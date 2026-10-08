"""Kenari provider profile for Hermes Agent.

Kenari (https://kenari.id) is an Indonesian AI gateway with an
OpenAI-compatible ``/v1`` surface: ``POST /v1/chat/completions`` with
``Authorization: Bearer kn-...``. One key serves the whole catalog, PAYG
billing is in IDR (micro-Rupiah per 1M tokens, live in
``GET /v1/models``), and ``:free``-suffixed models cost Rp 0 (with
per-minute and daily request limits).

The model catalog (``GET /v1/models``) is public — no key needed. Each
entry carries ``endpoints`` (``chat`` / ``images`` / ``videos`` /
``audio_*``), a ``tool_call`` boolean, ``pricing`` (input / output /
cache_read / cache_write in micro-IDR per 1M tokens), and reasoning
metadata (``reasoning``, ``reasoning_options``, ``reasoning_toggle``).

Wire notes (https://kenari.id/docs):

- Chat completions accepts ``reasoning_effort`` (top-level string) or a
  ``reasoning`` object (``effort`` / ``enabled`` / ``max_tokens`` /
  ``exclude``). When both are present the object wins, so this profile
  sends exactly one shape: ``extra_body.reasoning``.
- Effort levels are ``none``, ``minimal``, ``low``, ``medium``,
  ``high``, ``xhigh``, ``max``. ``minimal`` is treated as ``low``
  server-side, and an unsupported level is clamped server-side to the
  nearest listed level at or below the request — but this profile
  clamps client-side first from the cached catalog so intent is
  explicit on the wire.
- ``{"enabled": false}`` disables thinking on models with
  ``reasoning_toggle: true``; other models may still think (server
  best-effort, never a 400).
- On models without reasoning support the reasoning field is ignored
  server-side, so sending it unconditionally is safe.
- ``prompt_cache_key`` is a documented chat-completions field with
  OpenAI-compatible semantics, so the profile opts in.

Key-scoped filtering: dashboard keys can carry a model scope. The
gateway reports it on any disallowed model — ``HTTP 400`` with
``"model 'X' is not allowed by this key. Allowed models: a, b, c"``.
This profile reuses that signal (a $0 probe: a 400 bills nothing,
since requests with zero output tokens cost Rp 0) to filter the
``/model`` picker to what the configured key can actually call.

Usage & pricing: ``GET /v1/usage`` (``range=today|24h|7d|30d``),
``GET /v1/usage/daily``, ``GET /v1/account/balance`` and
``GET /v1/account/quota`` back ``fetch_account_usage`` (``hermes usage``
— weekly/monthly plan windows with Rp remaining, 7d/30d spend, free
daily quota left). ``get_usage_cost`` prices each response from the
cached catalog rates, so per-turn cost labels read in Rupiah.

Hermes core moves fast and a plugin has no say in which build a user
runs, so the points of contact with ``ProviderProfile`` — the fields
we set and the inherited ``fetch_models`` we delegate to — are resolved
by introspection rather than assumed. See ``_supported_kwargs`` and
``_inherited_fetch``. Hermes-only imports (``agent.account_usage``,
``agent.usage_pricing``) stay inside method bodies so a bare checkout
can still import and test this module.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import logging
import re
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

PROVIDER_ID = "kenari"
API_KEY_ENV = "KENARI_API_KEY"
BASE_URL_ENV = "KENARI_BASE_URL"

OPENAI_BASE_URL = "https://kenari.id/v1"
MODELS_URL = "https://kenari.id/v1/models"
PUBLIC_PRICING_URL = "https://kenari.id/api/public/pricing"
SIGNUP_URL = "https://kenari.id"
DOCS_URL = "https://kenari.id/docs"

# Indicative USD conversion for cost labels (USD/IDR ~17,900, Oct 2026).
# amount_usd is approximate; the exact Rp figure is always in the label.
_USD_IDR_PER_USD = Decimal("17900")

# Hermes-internal low->high ladder. Mirrors
# ``agent.reasoning_effort.EFFORT_LADDER`` so this plugin clamps
# client-side without importing Hermes internals (which move between
# builds and are not importable from a bare checkout).
_EFFORT_LADDER = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
)

# Model id -> {"reasoning": bool, "options": [level, ...]}. Filled by
# fetch_models from the live catalog; the reasoning hooks below answer
# from it and treat a cold cache as unknown (pass through / None).
_REASONING_CAPS: dict[str, dict[str, Any]] = {}

# Model id -> {"input","output","cache_read","cache_write"} micro-IDR
# per 1M tokens (None when the catalog omits the rate) + "free" bool.
# Filled alongside _REASONING_CAPS; get_usage_cost answers from it and
# returns None while cold so the core falls back to its generic path.
_PRICING: dict[str, dict[str, Any]] = {}

# Model id -> {"vision": bool} (image in input modalities). Used by
# resolve_aux_model to keep vision aux calls on capable routes.
_MODEL_META: dict[str, dict[str, Any]] = {}

# Full public tool-capable chat list (key-independent). The per-key
# scope filter is applied on top per call, with its own cache.
_CATALOG_CACHE: list[str] | None = None

# (sha256(key)[:16], base_url) -> frozenset | None. None means the key
# is unrestricted (or the probe was inconclusive) — no filtering.
_KEY_ALLOW_CACHE: dict[tuple[str, str], frozenset[str] | None] = {}

# A probe model id that cannot exist, so the gateway always answers 400
# (scoped key, with the allowed list) or 404/model_not_found
# (unrestricted key) — never a billed completion.
_SCOPE_PROBE_MODEL = "__kenari_key_scope_probe__"


def _url_opener():
    """Return Hermes' credential-safe URL opener, or urllib's as a fallback.

    ``hermes_cli.urllib_security.open_credentialed_url`` guards a request
    that carries an Authorization header; builds predating that module
    fetch their own catalogs with a bare ``urllib.request.urlopen``.
    Falling back to the same call keeps the plugin working on those
    builds without ever being laxer than the host build is with its own
    credentialed catalog fetches. (The Kenari catalog is public, so no
    Authorization header is sent either way.)
    """
    try:
        from hermes_cli.urllib_security import open_credentialed_url

        return open_credentialed_url
    except ImportError:
        logger.debug(
            "kenari: this Hermes build has no urllib_security helper — "
            "using urllib.request.urlopen, as the build's own catalog fetch does"
        )
        return urllib.request.urlopen


def _supported_kwargs(cls: type, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop profile fields the installed Hermes build does not define.

    ``ProviderProfile`` gains fields over time (``supports_vision``,
    ``supports_prompt_cache_key`` and ``hostname`` are recent). Passing
    one to an older build raises TypeError at import, which the plugin
    loader swallows — the provider then silently fails to register and
    the user sees "unknown provider" with no cause. Declaring the full
    modern field set and filtering it to what this build accepts
    degrades to a slightly less capable profile instead.
    """
    try:
        known = {f.name for f in dataclasses.fields(cls)}
    except TypeError:  # pragma: no cover — non-dataclass profile base
        return dict(kwargs)
    dropped = sorted(set(kwargs) - known)
    if dropped:
        logger.debug(
            "kenari: this Hermes build has no profile field(s) %s — skipping",
            ", ".join(dropped),
        )
    return {k: v for k, v in kwargs.items() if k in known}


def _fmt_rp(micro_idr: Any) -> str:
    """Micro-Rupiah -> ``Rp12.345`` display string."""
    try:
        rp = int(Decimal(str(micro_idr)) // Decimal(1_000_000))
    except Exception:
        return "Rp?"
    return f"Rp{rp:,}".replace(",", ".")


def _parse_allowed_models(body: str) -> frozenset[str] | None:
    """Parse ``Allowed models: a, b, c`` from a 400 scope error.

    Returns the allowed set, or None when the body carries no such list
    (unrestricted key, unknown model, or unrecognised shape — never
    filter on a guess).
    """
    if not body:
        return None
    match = re.search(r"[Aa]llowed models:\s*(.+)", body, flags=re.S)
    if not match:
        return None
    tail = match.group(1).strip()
    ids: set[str] = set()
    for part in re.split(r"\s*,\s*", tail):
        token = part.strip().strip("\"'[]{}").rstrip(".")
        if token and re.fullmatch(r"[A-Za-z0-9_.:+\-/]+", token):
            ids.add(token)
    return frozenset(ids) or None


def _fetch_key_allowed_models(
    *, api_key: str, base_url: str | None, timeout: float
) -> frozenset[str] | None:
    """Models the key may call, or None when unrestricted/inconclusive.

    One cheap probe: a chat completion with a model id that cannot
    exist. A scoped key answers ``400 ... Allowed models: ...``; an
    unrestricted key answers ``model_not_found`` (or another 400 without
    a list); a bad key answers 401. The probe bills Rp 0 either way —
    errored requests produce no output tokens.
    """
    if not (api_key or "").strip():
        return None
    base = (base_url or OPENAI_BASE_URL).rstrip("/")
    cache_key = (hashlib.sha256(api_key.encode()).hexdigest()[:16], base)
    if cache_key in _KEY_ALLOW_CACHE:
        return _KEY_ALLOW_CACHE[cache_key]

    open_url = _url_opener()
    payload = json.dumps(
        {
            "model": _SCOPE_PROBE_MODEL,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }
    ).encode()
    req = urllib.request.Request(
        base + "/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    result: frozenset[str] | None = None
    try:
        with open_url(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode(errors="replace")
        except Exception:
            body = ""
        if exc.code == 400:
            result = _parse_allowed_models(body)
            logger.debug(
                "kenari: key-scope probe -> %s",
                f"{len(result)} allowed models" if result is not None else "unrestricted",
            )
        elif exc.code == 401:
            logger.debug("kenari: key-scope probe got 401 (bad key) — no filtering")
            result = None
        else:
            logger.debug("kenari: key-scope probe got HTTP %s — no filtering", exc.code)
            result = None
    except Exception as exc:
        logger.debug("kenari: key-scope probe failed: %s", exc)
        result = None
    else:
        # The impossible model id was accepted — treat as unrestricted.
        result = None
    _KEY_ALLOW_CACHE[cache_key] = result
    return result


def _clamp_effort(model: str | None, effort: str) -> str:
    """Clamp *effort* onto the model's catalog-advertised levels.

    Nearest WEAKER-or-equal supported level (never escalate cost); when
    nothing weaker exists, the weakest supported level. Unknown model,
    cold cache, or no published options: verbatim — the server clamps
    the same way ("level tertinggi yang tercantum di bawah atau sama
    dengan permintaan"), so this is a no-op, not a guess.
    """
    requested = str(effort or "").strip().lower()
    caps = _REASONING_CAPS.get((model or "").strip()) if model else None
    options = None
    if isinstance(caps, dict):
        raw = caps.get("options")
        if isinstance(raw, list):
            options = [str(o).strip().lower() for o in raw if isinstance(o, str)]
            options = [o for o in options if o in _EFFORT_LADDER]
    if not requested or not options or requested in options:
        return effort
    if requested not in _EFFORT_LADDER:
        return effort
    candidates = [level for level in options if level != "none"]
    if not candidates:
        return effort
    requested_idx = _EFFORT_LADDER.index(requested)
    below_or_equal = [
        level for level in candidates if _EFFORT_LADDER.index(level) <= requested_idx
    ]
    # "none" disables reasoning — never a degradation target for an
    # enabled ask (it is already excluded from candidates above).
    if below_or_equal:
        return max(below_or_equal, key=_EFFORT_LADDER.index)
    return min(candidates, key=_EFFORT_LADDER.index)


def _usage_tokens(usage: Any, *names: str) -> int:
    """Read a token bucket off CanonicalUsage or a plain mapping."""
    if isinstance(usage, dict):
        for name in names:
            value = usage.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        return 0
    for name in names:
        value = getattr(usage, name, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return 0


def _cost_result(
    *, amount_usd: Any, status: str, source: str, label: str,
    notes: tuple = (),
) -> Any:
    """Build the core CostResult, or a stand-in dict on bare checkouts."""
    try:
        from agent.usage_pricing import CostResult  # type: ignore

        return CostResult(
            amount_usd=amount_usd, status=status, source=source, label=label,
            notes=tuple(notes),
        )
    except Exception:
        return {
            "amount_usd": amount_usd, "status": status, "source": source,
            "label": label, "fetched_at": None,
            "pricing_version": "kenari-catalog", "notes": tuple(notes),
        }


class KenariProfile(ProviderProfile):
    """Kenari gateway — picker filtering, reasoning mapping, pricing/usage."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Return tool-capable chat model ids the key can actually call.

        The public ``/v1/models`` response carries ``endpoints`` and a
        ``tool_call`` flag per entry, so the picker is filtered to chat
        routes an agent can actually drive (image / video / audio-only
        entries and non-tool chat models are dropped). Pricing, vision,
        and reasoning metadata are recorded alongside for the cost,
        aux-model, and effort-clamp hooks. When an ``api_key`` is
        supplied, the list is further intersected with the key's model
        scope (probed once per key, then cached) — a scoped key never
        offers models the gateway would reject.

        Falls back to the inherited ``/v1/models`` handling whenever
        the catalog is unreachable, malformed, or filters down to
        nothing — a degraded picker beats an empty one.

        A caller-supplied ``base_url`` that differs from the default
        means the user pointed Hermes at a proxy or self-hosted relay.
        Its catalog is not ours to interpret, so defer to the generic
        path in that case.
        """
        global _CATALOG_CACHE  # noqa: PLW0603

        caller_base = (base_url or "").strip()
        custom_base = bool(caller_base) and (
            caller_base.rstrip("/") != self.base_url.rstrip("/")
        )
        if custom_base:
            return self._inherited_fetch(
                api_key=api_key, base_url=base_url, timeout=timeout
            )

        if _CATALOG_CACHE is None:
            models = self._fetch_agentic_catalog(timeout=timeout)
            if not models:
                return self._inherited_fetch(
                    api_key=api_key, base_url=base_url, timeout=timeout
                )
            _CATALOG_CACHE = models

        models = list(_CATALOG_CACHE)
        if (api_key or "").strip():
            allowed = _fetch_key_allowed_models(
                api_key=api_key.strip(), base_url=caller_base or None,
                timeout=timeout,
            )
            if allowed is not None:
                scoped = [m for m in models if m in allowed]
                if not scoped:
                    logger.debug(
                        "kenari: key scope allows no tool-capable chat models — "
                        "falling back to generic listing",
                    )
                    return self._inherited_fetch(
                        api_key=api_key, base_url=base_url, timeout=timeout
                    )
                return scoped
        return models

    def _inherited_fetch(
        self, *, api_key: str | None, base_url: str | None, timeout: float
    ) -> list[str] | None:
        """Call the base-class fetch, passing only arguments it accepts.

        ``base_url`` was added to the base signature after this plugin's
        contract was written. Passing it to an older build is a
        TypeError, and the fallback path is exactly where a crash is
        least affordable — we are already here because the primary
        catalog failed.
        """
        parent = super()
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        try:
            accepted = inspect.signature(parent.fetch_models).parameters
        except (TypeError, ValueError):  # pragma: no cover — C-level callable
            accepted = {}
        if "base_url" in accepted:
            kwargs["base_url"] = base_url
        return parent.fetch_models(**kwargs)

    def _fetch_agentic_catalog(self, *, timeout: float) -> list[str] | None:
        """Fetch and filter the public catalog, or None."""
        open_url = _url_opener()

        req = urllib.request.Request(MODELS_URL)
        req.add_header("Accept", "application/json")

        try:
            with open_url(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
        except Exception as exc:
            logger.debug("fetch_models(kenari): catalog fetch failed: %s", exc)
            return None

        entries = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            logger.debug("fetch_models(kenari): unexpected catalog shape")
            return None

        seen: set[str] = set()
        models: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id or model_id in seen:
                continue
            endpoints = entry.get("endpoints") or []
            if "chat" not in endpoints:
                continue
            if entry.get("tool_call") is not True:
                continue
            seen.add(model_id)
            models.append(model_id)
            raw_options = entry.get("reasoning_options")
            _REASONING_CAPS[model_id] = {
                "reasoning": entry.get("reasoning") is True,
                "options": list(raw_options)
                if isinstance(raw_options, list)
                else [],
            }
            pricing = entry.get("pricing") if isinstance(entry.get("pricing"), dict) else {}
            _PRICING[model_id] = {
                "input": pricing.get("input"),
                "output": pricing.get("output"),
                "cache_read": pricing.get("cache_read"),
                "cache_write": pricing.get("cache_write"),
                "free": pricing.get("free") is True,
            }
            modalities = entry.get("modalities") if isinstance(entry.get("modalities"), dict) else {}
            model_inputs = modalities.get("input") or []
            _MODEL_META[model_id] = {
                "vision": "image" in model_inputs,
            }

        if not models:
            logger.debug("fetch_models(kenari): catalog filtered to zero models")
            return None
        return models

    def resolve_aux_model(self, *, vision: bool = False) -> str:
        """Key-aware cheap model for auxiliary tasks, or "".

        For a model-scoped key the static ``default_aux_model``
        (``glm-5-3-flash``) may be outside the key's scope, which would
        fail every compression/title/vision call. When the catalog and
        the key scope are both cached, answer with the first allowed
        model (vision-capable when ``vision=True``); otherwise return
        "" so the caller falls through to ``default_aux_model``.
        Cache-only — the scope probe result is reused, never re-fetched
        here beyond its own cache.
        """
        import os

        if _CATALOG_CACHE is None:
            return ""
        key = (os.environ.get(API_KEY_ENV) or "").strip()
        if not key:
            return ""
        allowed = _fetch_key_allowed_models(
            api_key=key,
            base_url=(os.environ.get(BASE_URL_ENV) or "").strip() or None,
            timeout=8.0,
        )
        if allowed is None:
            return ""
        candidates = [m for m in _CATALOG_CACHE if m in allowed]
        if vision:
            vision_ok = [m for m in candidates if _MODEL_META.get(m, {}).get("vision")]
            return vision_ok[0] if vision_ok else ""
        return candidates[0] if candidates else ""

    def default_reasoning_config(self, model: str | None = None) -> dict | None:
        """Unset ``agent.reasoning_effort`` → ``medium``.

        Matches Kenari's own default (``{"enabled": true}`` without an
        effort means ``medium``) and keeps a route's stronger default
        from silently applying — the same reason the custom /
        OpenAI-compatible profile pins medium.
        """
        return {"enabled": True, "effort": "medium"}

    def supported_reasoning_efforts(
        self, model: str | None
    ) -> tuple[str, ...] | None:
        """Declared effort vocabulary for *model*, from the catalog cache.

        ``None`` while cold, for unknown ids, and for models without
        reasoning support — Kenari ignores (never 400s) the reasoning
        field on those, so the transport default is the honest answer.
        Must not block on network I/O: cache only.
        """
        caps = _REASONING_CAPS.get((model or "").strip()) if model else None
        if not isinstance(caps, dict) or caps.get("reasoning") is not True:
            return None
        raw = caps.get("options")
        options = tuple(
            o for o in raw if isinstance(o, str) and o in _EFFORT_LADDER
        ) if isinstance(raw, list) else ()
        return options or None

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Map Hermes reasoning controls onto Kenari's ``reasoning`` object.

        The transport passes ``supports_reasoning=False`` for hosts it
        does not recognise (Kenari included) — gating on it would make
        this method a permanent no-op, so intent is forwarded whenever a
        config is present (mirroring the custom-provider profile).
        Sending the field to models without reasoning support is safe:
        the server ignores it. ``supports_reasoning`` is accepted and
        ignored for signature compatibility across builds.
        """
        _ = context.get("supports_reasoning")  # documented, intentionally unused
        cfg = dict(reasoning_config) if isinstance(reasoning_config, dict) else None
        if cfg is None:
            cfg = {"enabled": True, "effort": "medium"}
        if cfg.get("enabled") is False or str(cfg.get("effort") or "").strip().lower() == "none":
            return {"reasoning": {"enabled": False}}, {}
        effort = str(cfg.get("effort") or "medium").strip().lower()
        return {"reasoning": {"enabled": True, "effort": _clamp_effort(model, effort)}}, {}

    def get_usage_cost(self, model: str, usage: Any) -> Any | None:
        """Price one response from the cached catalog rates (no I/O).

        Micro-IDR per 1M tokens -> Rupiah -> indicative USD. ``:free``
        models cost Rp 0. Unknown model or cold cache: None, so the
        core falls back to its generic pricing path. A cache-write rate
        of None bills at the input rate, per the Kenari docs.
        """
        entry = _PRICING.get((model or "").strip())
        if not isinstance(entry, dict):
            return None
        if entry.get("free") is True:
            return _cost_result(
                amount_usd=Decimal("0"), status="estimated",
                source="provider_models_api", label="Rp0 ($0.00)",
                notes=("kenari :free model — Rp 0 (free tier, rate-limited)",),
            )
        rates = {
            "input": entry.get("input"),
            "output": entry.get("output"),
            "cache_read": entry.get("cache_read"),
            "cache_write": entry.get("cache_write"),
        }
        if rates["input"] is None or rates["output"] is None:
            return None
        if rates["cache_write"] is None:
            rates["cache_write"] = rates["input"]
        if rates["cache_read"] is None:
            rates["cache_read"] = 0
        try:
            tokens = {
                "input": _usage_tokens(usage, "input_tokens"),
                "output": _usage_tokens(usage, "output_tokens"),
                "cache_read": _usage_tokens(usage, "cache_read_tokens"),
                "cache_write": _usage_tokens(usage, "cache_write_tokens"),
            }
            # tokens x micro-IDR/1M, summed, /1M -> micro-IDR, /1M -> Rp.
            micro_total = sum(
                Decimal(tokens[k]) * Decimal(str(rates[k]))
                for k in tokens
            )
            rp = micro_total / Decimal(1_000_000_000_000)
            usd = rp / _USD_IDR_PER_USD
        except Exception:
            return None
        rp_int = int(rp)
        if usd < Decimal("0.01"):
            usd_label = f"~${usd:.4f}"
            usd_label = usd_label if usd_label != "~$0.0000" else "~$<0.0001"
        else:
            usd_label = f"~${usd:.2f}"
        return _cost_result(
            amount_usd=usd, status="estimated", source="provider_models_api",
            label="{} ({})".format(f"Rp{rp_int:,}".replace(",", "."), usd_label),
            notes=(
                "kenari catalog rates for {} (in {}/out {} per 1M; "
                "USD indicative at ~Rp{}/$)".format(
                    model, _fmt_rp(rates["input"]), _fmt_rp(rates["output"]),
                    f"{int(_USD_IDR_PER_USD):,}".replace(",", "."),
                ),
            ),
        )

    def fetch_account_usage(
        self, *, base_url: str | None = None, api_key: str | None = None
    ) -> Any | None:
        """Kenari wallet + plan + spend snapshot for ``hermes usage``.

        - ``GET /v1/account/balance``: wallet balance / available.
        - ``GET /v1/account/quota``: plan name + weekly/monthly (and 5h)
          windows with Rp used, Rp remaining, and reset time.
        - ``GET /v1/usage?range=7d|30d|today``: key-scoped spend totals.
        - ``GET /api/public/pricing`` (public): free-tier daily
          allowance, matched against today's usage for quota-left.

        Restricted (model-scoped) keys get HTTP 403 on the account
        endpoints — those sections are skipped with a note, and the
        usage sections (which a scoped key may read for its own key)
        still render. Every endpoint failure fails open individually;
        None is returned only when nothing at all could be read.
        """
        import os
        from datetime import datetime, timezone

        try:
            from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
        except Exception:
            return None

        key = (api_key or os.environ.get(API_KEY_ENV) or "").strip()
        if not key:
            return None
        base = (
            (base_url or "").strip()
            or (os.environ.get(BASE_URL_ENV) or "").strip()
            or (self.base_url or "").strip()
            or OPENAI_BASE_URL
        ).rstrip("/")

        def _get(path: str, *, auth: bool = True, timeout: float = 8.0):
            open_url = _url_opener()
            headers = {"Accept": "application/json"}
            if auth:
                headers["Authorization"] = f"Bearer {key}"
            req = urllib.request.Request(base + path, headers=headers)
            try:
                with open_url(req, timeout=timeout) as resp:
                    return getattr(resp, "status", 200) or 200, json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                try:
                    raw = exc.read().decode(errors="replace")
                except Exception:
                    raw = ""
                try:
                    payload = json.loads(raw) if raw else {}
                except Exception:
                    payload = {"_raw": raw[:200]}
                return exc.code, payload
            except Exception as exc:
                logger.debug("kenari usage: %s failed: %s", path, exc)
                return None, None

        def _parse_reset(value: Any):
            if not value or not isinstance(value, str):
                return None
            try:
                text = value.strip().replace("Z", "+00:00")
                dt = datetime.fromisoformat(text)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                return None

        windows: list = []
        details: list[str] = []
        raw: dict[str, Any] = {}
        plan_name: str | None = None
        restricted = False

        # 1. Wallet balance (unrestricted keys only).
        status, balance = _get("/account/balance")
        if status == 200 and isinstance(balance, dict):
            raw["balance"] = balance
            details.append(
                "Balance: {} available ({} reserved)".format(
                    _fmt_rp(balance.get("available_micro_idr")),
                    _fmt_rp(balance.get("reserved_micro_idr")),
                )
            )
        elif status == 403:
            restricted = True
        elif status is not None:
            details.append("Balance: unreadable (HTTP {})".format(status))

        # 2. Plan quota windows — weekly & monthly usage left.
        # QuotaWindow figures are whole Rupiah (not micro).
        status, quota = _get("/account/quota")
        if status == 200 and isinstance(quota, dict):
            raw["quota"] = quota
            plan = quota.get("plan") if isinstance(quota.get("plan"), dict) else None
            if plan:
                plan_name = plan.get("name")
                plan_windows = plan.get("windows") if isinstance(plan.get("windows"), dict) else {}
                for window_key, label in (
                    ("week", "Plan weekly"),
                    ("month", "Plan monthly"),
                    ("five_hour", "Plan 5h"),
                ):
                    win = plan_windows.get(window_key)
                    if not isinstance(win, dict):
                        continue
                    try:
                        used_f = float(win.get("used_rp") or 0)
                        rem_f = float(win.get("remaining_rp"))
                    except (TypeError, ValueError):
                        continue
                    total = used_f + rem_f
                    pct = (used_f / total * 100.0) if total > 0 else 0.0
                    reset_at = _parse_reset(win.get("resets_at"))
                    detail = "{} left of {}".format(
                        _fmt_rp(int(rem_f * 1_000_000)),
                        _fmt_rp(int(total * 1_000_000)),
                    )
                    windows.append(
                        AccountUsageWindow(
                            label=label, used_percent=pct,
                            reset_at=reset_at, detail=detail,
                        )
                    )
            coupon = quota.get("coupon") if isinstance(quota.get("coupon"), dict) else None
            if coupon and coupon.get("remaining_rp") is not None:
                try:
                    coupon_left = _fmt_rp(int(float(coupon["remaining_rp"]) * 1_000_000))
                except (TypeError, ValueError):
                    coupon_left = "?"
                expires = coupon.get("expires_at") or ""
                details.append(
                    "Coupon {}: {} left{}".format(
                        coupon.get("name") or "active", coupon_left,
                        " (expires {})".format(expires[:10]) if expires else "",
                    )
                )
        elif status == 403:
            restricted = True
        elif status is not None:
            details.append("Plan quota: unreadable (HTTP {})".format(status))

        # 3. Spend totals — this key's own usage (works for scoped keys).
        for rng, label in (("7d", "Last 7d"), ("30d", "Last 30d")):
            status, summary = _get("/usage?range=" + rng)
            if status == 200 and isinstance(summary, dict):
                total = summary.get("total") if isinstance(summary.get("total"), dict) else {}
                raw["usage_" + rng] = total
                details.append(
                    "{}: {} requests • {} spent".format(
                        label,
                        total.get("requests", "?"),
                        _fmt_rp(total.get("cost_micro_idr")),
                    )
                )
            elif status is not None:
                details.append("{} usage: unreadable (HTTP {})".format(label, status))

        # 4. Free-daily quota left (public allowance vs today's usage).
        free_allowance: int | None = None
        if base.split("://", 1)[-1].split("/", 1)[0] == "kenari.id":
            open_url = _url_opener()
            try:
                req = urllib.request.Request(
                    PUBLIC_PRICING_URL, headers={"Accept": "application/json"}
                )
                with open_url(req, timeout=8.0) as resp:
                    public = json.loads(resp.read().decode())
                free_allowance = (public.get("free_tier") or {}).get("daily")
            except Exception as exc:
                logger.debug("kenari usage: public pricing failed: %s", exc)
        today_requests: int | None = None
        status, today = _get("/usage?range=today")
        if status == 200 and isinstance(today, dict):
            total = today.get("total") if isinstance(today.get("total"), dict) else {}
            raw["usage_today"] = total
            try:
                today_requests = int(total.get("requests", 0))
            except (TypeError, ValueError):
                today_requests = None
        if free_allowance is not None and today_requests is not None:
            try:
                left = max(0, int(free_allowance) - today_requests)
            except (TypeError, ValueError):
                left = None
            if left is not None:
                details.append(
                    "Free models today: {} requests • ~{} of {}/day left "
                    "(Solo tier; higher after top-up/subscription)".format(
                        today_requests, left, free_allowance
                    )
                )
        elif today_requests is not None:
            details.append("Free models today: {} requests".format(today_requests))

        if restricted:
            details.append(
                "Note: this key is model-scoped — balance/plan are hidden; "
                "usage above is this key only."
            )

        if not windows and not details:
            return None
        return AccountUsageSnapshot(
            provider=self.name, source="kenari_usage_api",
            fetched_at=datetime.now(timezone.utc),
            title="Kenari usage", plan=plan_name,
            windows=tuple(windows), details=tuple(details), raw=raw,
        )


PROFILE_FIELDS: dict[str, Any] = {
    "name": PROVIDER_ID,
    "aliases": ("kenari-id", "kenariid"),
    "display_name": "Kenari",
    "description": "Kenari.id — Indonesian AI gateway (OpenAI-compatible, IDR billing)",
    "signup_url": SIGNUP_URL,
    "env_vars": (API_KEY_ENV, BASE_URL_ENV),
    "base_url": OPENAI_BASE_URL,
    "models_url": MODELS_URL,
    "hostname": "kenari.id",
    "auth_type": "api_key",
    "api_mode": "chat_completions",
    # 51 chat routes accept image input; vision-capable by default.
    "supports_vision": True,
    # Documented chat-completions field with OpenAI-compatible semantics.
    "supports_prompt_cache_key": True,
    # Cheap, paid (no :free rate limits), vision-capable, 1M context —
    # suits compression, title generation, and vision auxiliary calls.
    "default_aux_model": "glm-5-3-flash",
    # Shown only when the live catalog is unreachable. Tool-capable chat
    # routes spanning vendors, verified against the live catalog —
    # including two :free entries for zero-balance setups.
    "fallback_models": (
        "step-3-7-flash",
        "deepseek-v4-flash",
        "glm-5-3-flash",
        "gpt-oss-20b",
        "kimi-k3",
        "claude-haiku-5-5",
        "step-3-7-flash:free",
        "muse-spark-1-3-contributor:free",
    ),
}

kenari = KenariProfile(**_supported_kwargs(KenariProfile, PROFILE_FIELDS))

register_provider(kenari)
