"""Verify scripts/apply_kenari_pricing_patch.py: apply -> idempotent -> revert.

Runs against a temp copy of the UPSTREAM module (from git HEAD), never the
live core tree. Run: python tests/verify_pricing_patch.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Hermes home is $LOCALAPPDATA/hermes on Windows, ~/.hermes elsewhere.
HERMES = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes" / "hermes-agent"
if not (HERMES / "hermes_cli").is_dir():
    HERMES = Path.home() / ".hermes" / "hermes-agent"
SCRIPT = REPO / "scripts" / "apply_kenari_pricing_patch.py"


def run(*args: str) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True
    )
    return p.returncode, (p.stdout + p.stderr).strip()


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    core = tmp / "hermes-agent" / "hermes_cli"
    core.mkdir(parents=True)
    target = core / "models_pricing.py"

    upstream = subprocess.run(
        ["git", "-C", str(HERMES), "show", "HEAD:hermes_cli/models_pricing.py"],
        capture_output=True,
    )
    if upstream.returncode != 0:
        print("ERROR: could not read upstream module from git")
        return 1
    target.write_bytes(upstream.stdout)
    upstream_text = upstream.stdout.decode("utf-8")

    fails: list[str] = []

    # 1. apply
    rc, out = run("--hermes-home", str(tmp))
    print("apply:", rc, out)
    if rc != 0:
        fails.append("apply returned non-zero")
    patched = target.read_text(encoding="utf-8")
    if "def _fetch_kenari_pricing(" not in patched:
        fails.append("patch block missing after apply")
    if '"kenari": _fetch_kenari_pricing_for_provider,' not in patched:
        fails.append("fetcher entry missing after apply")
    if patched.count("_fetch_kenari_pricing_for_provider") != 2:
        # 1 def + 1 dict entry
        fails.append("unexpected fetcher reference count: %d" % patched.count("_fetch_kenari_pricing_for_provider"))
    try:
        compile(patched, str(target), "exec")
    except SyntaxError as e:
        fails.append(f"patched module has a syntax error: {e}")

    # 2. idempotent
    rc, out = run("--hermes-home", str(tmp))
    print("re-apply:", rc, out)
    if "already patched" not in out:
        fails.append("second apply was not reported as already patched")
    if target.read_text(encoding="utf-8") != patched:
        fails.append("second apply changed the file (not idempotent)")

    # 3. revert restores upstream byte-for-byte
    rc, out = run("--hermes-home", str(tmp), "--revert")
    print("revert:", rc, out)
    reverted = target.read_text(encoding="utf-8")
    if "kenari" in reverted.lower():
        fails.append("kenari references remain after revert")
    if reverted.replace("\r\n", "\n") != upstream_text.replace("\r\n", "\n"):
        fails.append("reverted file differs from upstream")
    try:
        compile(reverted, str(target), "exec")
    except SyntaxError as e:
        fails.append(f"reverted module has a syntax error: {e}")

    # 4. re-apply after revert works (upgrade path)
    rc, out = run("--hermes-home", str(tmp))
    print("re-apply after revert:", rc, out)
    if rc != 0 or "_fetch_kenari_pricing(" not in target.read_text(encoding="utf-8"):
        fails.append("could not re-apply after revert")

    print()
    if fails:
        for f in fails:
            print("FAIL:", f)
        return 1
    print("PASS: apply / idempotent / revert / re-apply all verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
