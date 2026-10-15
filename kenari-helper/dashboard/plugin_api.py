"""Kenari Helper backend — serves the desktop statusbar chip.

Mounted at ``/api/plugins/kenari-helper/``. ``GET /usage-summary``
calls the ``kenari`` provider profile's ``fetch_account_usage()`` (the
same snapshot ``hermes usage`` renders) and returns the 7d/30d/free-
today figures as JSON. Fails open: every error becomes a 200 with
``{\"ok\": false, ...}`` so the chip degrades to a quiet label instead
of an error toast.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

router = APIRouter()


_KENARI_PLANS_URL = "https://kenari.id/api/plans"
_KENARI_API_BASE = "https://kenari.id/v1"
_plans_cache: dict[str, Any] = {"at": 0.0, "plans": None}
_ws_cache: dict[str, Any] = {"at": 0.0, "key": "", "value": None}


def _fetch_plans() -> list[dict[str, Any]] | None:
    """Plan catalog from Kenari's public ``/api/plans`` (no key needed).

    Cached in-process for 10 minutes — the plan table changes rarely and the
    statusbar polls every 2 minutes.
    """
    import time

    now = time.monotonic()
    if _plans_cache["plans"] is not None and now - _plans_cache["at"] < 600:
        return _plans_cache["plans"]
    try:
        import json
        import urllib.request

        req = urllib.request.Request(
            _KENARI_PLANS_URL,
            headers={"Accept": "application/json", "User-Agent": "hermes-kenari-plugin"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:
        log.debug("kenari plans fetch failed: %s", exc)
        return _plans_cache["plans"]  # stale beats nothing
    plans = payload if isinstance(payload, list) else payload.get("plans")
    if not isinstance(plans, list):
        return _plans_cache["plans"]
    _plans_cache["plans"] = plans
    _plans_cache["at"] = now
    return plans


def _free_cache_for_plan(plan_name: str | None) -> dict[str, Any] | None:
    """``free_cache_models`` for the ACTIVE plan, resolved by name.

    Kenari marks a per-plan subset of models whose *cache reads* are free
    (``/api/plans`` → ``free_cache_models``); the active plan name comes from
    ``/v1/account/quota`` (``quota.plan.name``). Matching the two gives the
    live free-cache list without hard-coding plan knowledge.
    """
    if not plan_name:
        return None
    plans = _fetch_plans()
    if not plans:
        return None
    want = str(plan_name).strip().lower()
    plan = next((p for p in plans if str(p.get("name", "")).strip().lower() == want), None)
    if plan is None:
        return None
    models = plan.get("free_cache_models") or []
    return {
        "plan_id": plan.get("id"),
        "models": [str(m) for m in models],
        "count": len(models),
    }


def _resolve_key() -> str:
    """The Kenari API key, from the provider's credential resolution.

    Mirrors how the model provider resolves it (Hermes secret scope, then
    ``~/.hermes/.env``), with a plain env fallback so the backend still works
    in a stripped process.
    """
    try:
        from hermes_cli.auth import resolve_api_key_provider_credentials

        creds = resolve_api_key_provider_credentials("kenari") or {}
        if isinstance(creds, dict):
            key = str(creds.get("api_key") or "").strip()
            if key:
                return key
    except Exception as exc:  # noqa: BLE001
        log.debug("kenari-helper key resolution fell back to env: %s", exc)
    return (os.environ.get("KENARI_API_KEY") or "").strip()


def _get_json(url: str, api_key: str, timeout: float = 10.0) -> Any:
    """GET *url* as JSON with the Kenari UA, or ``None`` on any failure."""
    try:
        import json
        import urllib.request

        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                # Kenari's /api/* routes sit behind Cloudflare: a UA-less
                # client gets Error 1010.
                "User-Agent": "hermes-kenari-plugin",
                **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        log.debug("kenari-helper GET %s failed: %s", url, exc)
        return None


_WEB_CALL_MODELS = ("web_search", "web_fetch", "web_extract")


def _is_web_call_model(name: Any) -> bool:
    """Is *name* a Kenari web call consuming the shared daily allowance?

    Measured live: ``/v1/web/search`` and ``/v1/web/fetch`` both log under
    ``web_search``, and ``web_fetch``/``web_extract`` never appear. The other
    two names are accepted defensively so a future split can't under-count.
    """
    return str(name or "").strip().lower() in _WEB_CALL_MODELS


def _next_utc_midnight_iso() -> str:
    """When the daily web-search allowance resets — 00:00 UTC, per Kenari's docs."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    tomorrow = (now + datetime.timedelta(days=1)).date()
    return datetime.datetime.combine(tomorrow, datetime.time.min, datetime.timezone.utc).isoformat()


def _web_search_usage(plan_name: str | None, api_key: str) -> dict[str, Any] | None:
    """Today's web-search calls against the plan's daily allowance.

    Kenari bills web search/fetch against a per-plan DAILY CALL allowance
    counted per UTC day (``web_search_daily`` in ``/api/plans``). Usage comes
    from ``GET /v1/usage?range=today&model=web_search``.

    Note the counter is SHARED: ``/v1/web/search`` and ``/v1/web/fetch`` each
    consume one call from the same bucket, and Kenari logs both under the
    ``web_search`` model name — ``web_fetch``/``web_extract`` never appear.
    So this one number already covers page fetches; there is no second
    counter to add.

    Returns ``None`` when either half is unknown: no limit on the plan (the
    field is absent/0 → unlimited) still returns usage, and an unreadable
    counter must not render as "0 used".
    """
    import time

    if not api_key:
        return None
    limit: int | None = None
    if plan_name and (plans := _fetch_plans()):
        want = str(plan_name).strip().lower()
        plan = next((p for p in plans if str(p.get("name", "")).strip().lower() == want), None)
        if plan is not None:
            raw_limit = plan.get("web_search_daily")
            if isinstance(raw_limit, int) and raw_limit > 0:
                limit = raw_limit

    now = time.monotonic()
    if _ws_cache["value"] is not None and _ws_cache["key"] == api_key and now - _ws_cache["at"] < 60:
        cached = dict(_ws_cache["value"])
        if limit is not None:
            cached["limit"] = limit
            cached["remaining"] = max(0, limit - int(cached.get("used") or 0))
        return cached

    payload = _get_json(f"{_KENARI_API_BASE}/usage?range=today&model=web_search", api_key)
    if not isinstance(payload, dict):
        return None
    used = 0
    rows = payload.get("data")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and _is_web_call_model(row.get("model")):
                used += int(row.get("requests") or 0)
    value: dict[str, Any] = {"used": used, "resets_at": _next_utc_midnight_iso()}
    if limit is not None:
        value["limit"] = limit
        value["remaining"] = max(0, limit - used)
    _ws_cache.update({"at": now, "key": api_key, "value": dict(value)})
    return value


def _snapshot_to_payload(snapshot: Any) -> dict[str, Any]:
    windows = [
        {
            "label": w.label,
            "used_percent": w.used_percent,
            "remaining_percent": (
                None if w.used_percent is None else max(0.0, 100.0 - float(w.used_percent))
            ),
            "reset_at": w.reset_at.isoformat() if w.reset_at else None,
            "detail": w.detail,
        }
        for w in (snapshot.windows or [])
    ]

    # The chip shows PLAN QUOTA REMAINING (week / month), which lives in
    # the account-quota windows — not request counts. ``hermes usage``
    # labels them "Plan weekly" / "Plan monthly"; map them to stable keys.
    by_label = {w["label"]: w for w in windows}
    week = by_label.get("Plan weekly")
    month = by_label.get("Plan monthly")

    plan = None
    if snapshot.plan or week or month:
        plan = {
            "name": snapshot.plan,
            "week_remaining_percent": (week or {}).get("remaining_percent"),
            "week_used_percent": (week or {}).get("used_percent"),
            "week_resets_at": (week or {}).get("reset_at"),
            "week_detail": (week or {}).get("detail"),
            "month_remaining_percent": (month or {}).get("remaining_percent"),
            "month_used_percent": (month or {}).get("used_percent"),
            "month_resets_at": (month or {}).get("reset_at"),
            "month_detail": (month or {}).get("detail"),
        }

    raw = snapshot.raw if isinstance(snapshot.raw, dict) else {}
    free_cache = _free_cache_for_plan(snapshot.plan)
    web_search = _web_search_usage(snapshot.plan, _resolve_key())
    return {
        "ok": True,
        "provider": snapshot.provider,
        "title": snapshot.title,
        "plan": plan,
        "free_cache": free_cache,
        "web_search": web_search,
        "windows": windows,
        "lines": list(snapshot.details or []),
        "usage_7d": raw.get("usage_7d"),
        "usage_30d": raw.get("usage_30d"),
        "usage_today": raw.get("usage_today"),
        "balance": raw.get("balance"),
        "quota": raw.get("quota"),
    }


@router.get("/usage-summary")
def usage_summary() -> JSONResponse:
    """7d/30d/free-today Kenari usage for the statusbar chip."""
    try:
        from providers import get_provider_profile

        profile = get_provider_profile("kenari")
        if profile is None:
            return JSONResponse(
                {"ok": False, "reason": "kenari provider not registered"}, status_code=200
            )
        fetch = getattr(profile, "fetch_account_usage", None)
        if fetch is None:
            return JSONResponse(
                {"ok": False, "reason": "profile has no usage hook"}, status_code=200
            )
        snapshot = fetch()
    except Exception as exc:  # never 500 the statusbar
        log.debug("kenari-helper backend failed: %s", exc)
        return JSONResponse({"ok": False, "reason": "fetch failed"}, status_code=200)
    if snapshot is None:
        hint = (
            "no KENARI_API_KEY configured"
            if not (os.environ.get("KENARI_API_KEY") or "").strip()
            else "usage unreadable"
        )
        return JSONResponse({"ok": False, "reason": hint}, status_code=200)
    try:
        return JSONResponse(_snapshot_to_payload(snapshot), status_code=200)
    except Exception as exc:
        log.debug("kenari-helper payload shaping failed: %s", exc)
        return JSONResponse({"ok": False, "reason": "shaping failed"}, status_code=200)
