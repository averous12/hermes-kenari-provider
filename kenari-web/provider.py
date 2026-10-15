"""Kenari web search + page fetch backend.

Backed by Kenari's standalone endpoints (no model needed):

* ``POST /v1/web/search``  ``{"query", "limit"}`` -> ``{"results": [{title, url, content}]}``
* ``POST /v1/web/fetch``   ``{"url"}``            -> ``{title, content, links, id, cost_micro_idr}``

Auth reuses ``KENARI_API_KEY`` — the same credential the model provider uses —
so this backend needs no web-search-specific key. Calls are billed per call
against the plan's web-search allowance (``web_search_daily``), separately from
tokens.

Positioning: ``is_available()`` is True whenever a Kenari key is present, which
makes this a *fallback* rather than a hijack. Hermes resolves the active backend
as: explicit ``web.search_backend``/``web.backend`` config, then the single
available capable provider, then the legacy preference walk, then the anonymous
keyless ring. So Kenari serves search/extract when it is the only keyed backend,
and steps aside as soon as a dedicated web backend (Firecrawl, Tavily, Exa, …)
is configured or keyed. Pin it outright with::

    web:
      search_backend: kenari
      extract_backend: kenari

Self-contained on purpose: only ``agent.web_search_provider`` (the ABC and its
env helper) is imported, so a bundled-module rename can't break this plugin.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

from agent.web_search_provider import WebSearchProvider, get_provider_env

logger = logging.getLogger(__name__)

KEY_ENV = "KENARI_API_KEY"
BASE_URL_ENV = "KENARI_BASE_URL"
DEFAULT_BASE_URL = "https://kenari.id/v1"
_TIMEOUT = 20


def _base_url() -> str:
    """Kenari API root, honouring ``KENARI_BASE_URL`` (proxy / self-hosted relay)."""
    return (get_provider_env(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")


def _api_key() -> str:
    """The Kenari key, however it was configured.

    Layered, cheapest-first, because there are two distinct write paths and
    they do NOT land in the same place:

    1. ``get_provider_env(KEY_ENV)`` — the normal path. The provider plugin
       declares ``KENARI_API_KEY`` in its profile's ``env_vars``, and Hermes
       (plus this web backend) read the same name out of ``os.environ`` /
       ``~/.hermes/.env``. One key, one file, both sides.
    2. The Provider page's desktop save path, which persists a *credential
       pointer* as ``model.key_env`` → ``$HERMES_HOME/.env`` instead of the
       registry's canonical var (#106336). If the card wrote a custom name,
       ``get_provider_env(KEY_ENV)`` finds nothing and web search would
       silently report unavailable while chat worked fine — so honor the
       pointer.
    3. ``resolve_api_key_provider_credentials`` — the model provider's own
       resolution, which covers the credential pool and every other path the
       model side already accepts.
    """
    val = get_provider_env(KEY_ENV)
    if val:
        return val

    try:
        from hermes_cli.config import get_env_value_prefer_dotenv, load_config

        model_cfg = (load_config() or {}).get("model")
        if isinstance(model_cfg, dict) and \
                str(model_cfg.get("provider") or "").strip().lower() == "kenari":
            pointer = model_cfg.get("key_env") or model_cfg.get("api_key_env")
            if pointer:
                pointed = (get_env_value_prefer_dotenv(str(pointer)) or "").strip()
                if pointed:
                    logger.debug("kenari-web: key via model.key_env=%s", pointer)
                    return pointed
    except Exception as exc:  # noqa: BLE001 — pointer is an optional fallback
        logger.debug("kenari-web: model.key_env lookup unavailable: %s", exc)

    try:
        from hermes_cli.auth import resolve_api_key_provider_credentials

        creds = resolve_api_key_provider_credentials("kenari") or {}
        if isinstance(creds, dict):
            resolved = str(creds.get("api_key") or "").strip()
            if resolved:
                logger.debug("kenari-web: key via the provider credential resolver")
                return resolved
    except Exception as exc:  # noqa: BLE001 — same: optional fallback
        logger.debug("kenari-web: provider credential resolution unavailable: %s", exc)

    return ""


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Kenari sits behind Cloudflare; a UA-less client gets Error 1010.
        "User-Agent": "hermes-kenari-plugin",
    }


MAX_LIMIT = 20


def _search_fail_for_status(status: int) -> str:
    """Human error for a failed Kenari call.

    ``402`` is the exhausted-allowance path: every plan carries a daily
    web-search/fetch call allowance counted per UTC day, and above it a call is
    only served by billing balance — with "continue with balance" off, it is
    refused. ``429`` with ``plan_limit_reached`` is the quota-window equivalent.
    """
    if status == 401:
        return f"{KEY_ENV} is invalid (Kenari returned 401)"
    if status == 402:
        return (
            "Kenari refused the call: the plan's daily web-search allowance is used up "
            "(it resets at 00:00 UTC). Enable \"continue with balance\" in the Kenari "
            "dashboard to bill further calls to your balance, or top up."
        )
    if status == 429:
        return (
            "Kenari rate-limited or rejected the call (HTTP 429) — the plan's daily "
            "allowance may be spent, or a quota window is exhausted."
        )
    return f"Kenari returned HTTP {status}"


def _post(path: str, payload: Dict[str, Any]) -> tuple[Any, str]:
    """POST ``path`` -> ``(json, "")`` on success or ``(None, error)``."""
    try:
        resp = httpx.post(_base_url() + path, json=payload, headers=_headers(), timeout=_TIMEOUT)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        detail = ""
        try:
            body = exc.response.json()
            err = (body or {}).get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("code") or "")
            elif err:
                detail = str(err)
        except Exception:
            detail = ""
        message = _search_fail_for_status(status)
        if detail:
            message = f"{message} — {detail}"
        logger.warning("Kenari %s HTTP %s: %s", path, status, detail or "(no detail)")
        return None, message
    except httpx.RequestError as exc:
        logger.warning("Kenari %s unreachable: %s", path, exc)
        return None, f"Could not reach Kenari: {exc}"
    try:
        return resp.json(), ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("Kenari %s response not JSON: %s", path, exc)
        return None, "Could not parse Kenari response as JSON"


class KenariWebSearchProvider(WebSearchProvider):
    """Search + fetch via Kenari's standalone web endpoints."""

    @property
    def name(self) -> str:
        # Stable id for web.search_backend / web.extract_backend.
        return "kenari"

    @property
    def display_name(self) -> str:
        return "Kenari"

    def is_available(self) -> bool:
        """True when the Kenari key is present. Cheap, no network.

        Deliberately keyed: a Kenari key is a general API credential the user
        already holds for models, so treating it as a web backend needs no new
        setup. ``is_keyless_available`` stays False — Kenari has no anonymous
        tier, and claiming one would let the legacy walk route users who hold a
        real web key onto Kenari.
        """
        # Same resolver the request path uses, so availability and auth can
        # never disagree: a key found only through the Provider page's
        # model.key_env pointer still counts as available.
        return bool(_api_key())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        want = max(1, min(int(limit or 5), MAX_LIMIT))
        data, error = _post("/web/search", {"query": query, "limit": want})
        if error:
            return {"success": False, "error": error}

        raw = data.get("results") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return {"success": False, "error": "Unexpected Kenari search response shape"}

        # The endpoint may return more than asked for; trim to the caller's limit.
        rows: List[Dict[str, Any]] = []
        for index, item in enumerate(raw[:want]):
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "title": str(item.get("title", "")),
                    "url": str(item.get("url", "")),
                    "description": str(item.get("content", "")),
                    "position": index + 1,
                }
            )
        logger.info("Kenari search '%s': %d results (limit %d)", query, len(rows), want)
        return {"success": True, "data": {"web": rows}}

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Fetch each URL. Per-URL failures become entries with ``error``."""
        results: List[Dict[str, Any]] = []
        for url in urls:
            data, error = _post("/web/fetch", {"url": url})
            if error:
                results.append({"url": url, "title": "", "content": "", "error": error})
                continue
            if not isinstance(data, dict):
                results.append(
                    {"url": url, "title": "", "content": "", "error": "Unexpected Kenari fetch response shape"}
                )
                continue
            title = str(data.get("title", "") or "")
            content = str(data.get("content", "") or "")
            results.append(
                {
                    "url": url,
                    "title": title,
                    "content": content,
                    "raw_content": content,
                    "metadata": {"sourceURL": url, "title": title},
                }
            )
        logger.info("Kenari extract: %d/%d URLs fetched", sum(1 for r in results if not r.get("error")), len(urls))
        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        """Picker entry for ``hermes tools``."""
        return {
            "name": "Kenari",
            "badge": "keyed",
            "tag": "Reuses KENARI_API_KEY — search + fetch, billed per call against the plan's web allowance.",
            "env_vars": [
                {
                    "key": KEY_ENV,
                    "prompt": "Kenari API key (shared with the model provider)",
                    "url": "https://kenari.id",
                }
            ],
        }
