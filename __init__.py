"""Kenari provider — repo-root entry point.

Installing this repository without a subdirectory
(``hermes plugins install averous12/hermes-kenari-provider``) drops the
whole repo into ``~/.hermes/plugins/<name>/``, and the provider loader
imports ``__init__.py`` from the plugin directory root. The real profile
lives in ``kenari/``; this shim loads it so the bare-repo install
registers exactly the same provider.

Prefer the subdir install when you can
(``.../hermes-kenari-provider/kenari``) — it keeps the plugin directory
to just the provider.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_DIR = Path(__file__).resolve().parent / "kenari"
_INIT = _MODULE_DIR / "__init__.py"

if _INIT.is_file():
    _name = f"{__name__}._kenari_profile"
    if _name not in sys.modules:
        _spec = importlib.util.spec_from_file_location(
            _name, _INIT, submodule_search_locations=[str(_MODULE_DIR)]
        )
        if _spec is not None and _spec.loader is not None:
            _module = importlib.util.module_from_spec(_spec)
            sys.modules[_name] = _module
            _spec.loader.exec_module(_module)
