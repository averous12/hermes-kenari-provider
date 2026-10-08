"""Kenari usage backend — serves the desktop statusbar chip.

Mounted at ``/api/plugins/kenari-usage/``. ``GET /usage-summary``
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


def _snapshot_to_payload(snapshot: Any) -> dict[str, Any]:
    windows = [
        {
            "label": w.label,
            "used_percent": w.used_percent,
            "reset_at": w.reset_at.isoformat() if w.reset_at else None,
            "detail": w.detail,
        }
        for w in (snapshot.windows or [])
    ]
    raw = snapshot.raw if isinstance(snapshot.raw, dict) else {}
    return {
        "ok": True,
        "provider": snapshot.provider,
        "title": snapshot.title,
        "plan": snapshot.plan,
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
        log.debug("kenari-usage backend failed: %s", exc)
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
        log.debug("kenari-usage payload shaping failed: %s", exc)
        return JSONResponse({"ok": False, "reason": "shaping failed"}, status_code=200)
