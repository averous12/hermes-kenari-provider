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
    return {
        "ok": True,
        "provider": snapshot.provider,
        "title": snapshot.title,
        "plan": plan,
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
