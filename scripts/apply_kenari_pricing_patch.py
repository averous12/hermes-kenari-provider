"""Kenari picker-pricing patch for Hermes core — `hermes_cli/models_pricing.py`.

WHY THIS EXISTS
---------------
The model picker renders a per-row price/free chip from
``row["pricing"]``, which is built by ``inventory._apply_pricing`` from
``models_pricing._PRICING_FETCHERS``. That dict is **core-only** and has
no plugin hook, so a provider plugin cannot supply pricing — every Kenari
model shows no badge.

This patch registers a ``kenari`` entry in ``_PRICING_FETCHERS``. The
fetch itself is still done by the installed plugin, so this file only
needs to be re-applied when Hermes updates that core module.

RESULT
------
- ``:free`` routes render the green "free" badge.
- Paid routes render ``$in/$out`` per Mtok (plus the cached-read rate),
  converted from Kenari's micro-Rupiah with an indicative USD/IDR rate.

APPLY
-----
    python scripts/apply_kenari_pricing_patch.py [--hermes-home PATH]

Idempotent: re-running on an already-patched tree is a no-op, and
``--revert`` removes exactly what this added.

NOTE ON THE DESKTOP HALF
------------------------
The picker's per-row type, ``ModelPricing``, is defined in a *generated*
file: ``apps/shared/src/gateway-contract.generated.ts`` (emitted from the
Python gateway contract). Do NOT hand-edit it. This patch sidesteps the
question entirely by emitting ``free_cache`` as an extra dict key from
``inventory._apply_pricing`` — the generated interface carries
``[key: string]: unknown``, so an unknown key type-checks without a
regeneration step. If a future change ever *requires* a typed field,
regenerate the contract rather than editing the generated file.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

MARKER = "def _fetch_kenari_pricing("

PATCH_BLOCK = '''

def _fetch_kenari_pricing(timeout: float = 5.0, *, force_refresh: bool = False) -> dict[str, dict[str, str]]:
    """Kenari.id picker pricing from its public catalog.

    ``GET /v1/models`` needs no key and carries ``pricing`` in micro-Rupiah
    per 1M tokens (``input`` / ``output`` / ``cache_read``; ``null`` cache
    rates mean "bill at the input rate"). Prices are converted to the
    picker's per-token USD shape so the model list renders live chips —
    a ``:free`` model reports the ``free`` flag and shows the green badge.

    USD figures are indicative (see ``_KENARI_USD_IDR``); Kenari bills in
    Rupiah, so the badge is a comparison aid, not an invoice.
    """
    cache_key = "kenari/models"
    if not force_refresh:
        cached = _cached_catalog(cache_key)
        if cached is not None:
            return cached

    payload = _get_json("https://kenari.id/v1/models", {"Accept": "application/json"}, timeout)
    if payload is None:
        return _cache_catalog(cache_key, {})

    result: dict[str, dict[str, str]] = {}
    for item in _catalog_items(payload):
        mid = item.get("id")
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else None
        if not mid or not pricing:
            continue
        free = pricing.get("free") is True
        inp = pricing.get("input")
        out = pricing.get("output")
        if free:
            # `:free` models still PUBLISH paid list rates; only the flag
            # marks them. Zero them so the picker shows "free", not a price.
            inp = out = 0
        if inp is None or out is None:
            continue
        entry: dict[str, str] = {
            "prompt": _kenari_micro_idr_to_usd_per_token(inp),
            "completion": _kenari_micro_idr_to_usd_per_token(out),
        }
        cache_read = pricing.get("cache_read")
        if cache_read is None:
            cache_read = inp
        if free:
            cache_read = 0
        if cache_read:
            entry["input_cache_read"] = _kenari_micro_idr_to_usd_per_token(cache_read)
        result[str(mid)] = entry

    return _cache_catalog(cache_key, result)


def _kenari_micro_idr_to_usd_per_token(micro_idr_per_1m) -> str:
    """Kenari micro-Rupiah per 1M tokens -> the picker's per-token USD string."""
    try:
        rp_per_mtok = float(micro_idr_per_1m) / 1_000_000
        return str(rp_per_mtok / _KENARI_USD_IDR / 1_000_000)
    except (TypeError, ValueError):
        return "0"


def _fetch_kenari_pricing_for_provider(*, force_refresh: bool = False) -> dict[str, dict[str, str]]:
    _remember_provider_cache_key("kenari", "kenari/models")
    return _fetch_kenari_pricing(force_refresh=force_refresh)


# Indicative USD/IDR (Kenari bills in Rupiah, the picker shows USD).
_KENARI_USD_IDR = 17900.0

'''

FETCHER_ENTRY = '    "kenari": _fetch_kenari_pricing_for_provider,\n'


def find_core_module(hermes_home: Path) -> Path | None:
    candidates = [
        hermes_home / "hermes-agent" / "hermes_cli" / "models_pricing.py",
        Path(__file__).resolve().parent.parent / "hermes_cli" / "models_pricing.py",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def apply(path: Path, *, revert: bool = False) -> int:
    src = path.read_text(encoding="utf-8")

    if revert:
        if MARKER not in src:
            print(f"nothing to revert in {path}")
            return 0
        backup = path.with_suffix(path.suffix + ".kenari-orig")
        if backup.exists():
            # The backup is the exact pre-patch module — restoring it is the
            # only byte-clean revert (string splicing kept drifting on the
            # blank-line separator).
            shutil.copy2(backup, path)
            print(f"reverted {path} from {backup}")
            return 0
        # No backup (e.g. patch applied by hand): fall back to splicing.
        patched = src
        start = patched.index("\n\ndef _fetch_kenari_pricing(")
        end_anchor = "_KENARI_USD_IDR = 17900.0"
        end = patched.index(end_anchor, start) + len(end_anchor)
        while end < len(patched) and patched[end] == "\n":
            end += 1
        patched = patched[:start] + "\n\n" + patched[end:].lstrip("\n")
        patched = patched.replace(FETCHER_ENTRY, "")
        path.write_text(patched, encoding="utf-8")
        print(f"reverted {path} (spliced; no backup found)")
        return 0

    if MARKER in src:
        print(f"already patched: {path}")
        return 0

    anchor = "_PRICING_FETCHERS = {"
    if anchor not in src:
        print(f"ERROR: {anchor!r} not found in {path} — Hermes layout changed", file=sys.stderr)
        return 1

    backup = path.with_suffix(path.suffix + ".kenari-orig")
    if not backup.exists():
        shutil.copy2(path, backup)

    patched = src.replace(anchor, PATCH_BLOCK + anchor, 1)
    # Register the fetcher; tolerate a closing brace on the same line.
    idx = patched.index(anchor)
    close = patched.index("}", idx)
    patched = patched[:close] + FETCHER_ENTRY + patched[close:]

    path.write_text(patched, encoding="utf-8")
    print(f"patched {path} (backup at {backup})")
    print("restart Hermes (or the desktop app) for the picker to pick it up")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hermes-home", default=str(Path.home() / ".hermes"))
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()

    home = Path(args.hermes_home)
    module = find_core_module(home)
    if module is None:
        print(f"ERROR: models_pricing.py not found under {home}", file=sys.stderr)
        return 1
    return apply(module, revert=args.revert)


if __name__ == "__main__":
    raise SystemExit(main())
