"""Minimal stand-ins for the Hermes runtime, so the plugin is testable alone.

The plugin imports ``providers`` / ``providers.base`` from the Hermes tree and
``hermes_cli.urllib_security`` plus ``agent.account_usage`` /
``agent.usage_pricing`` at fetch time. None of those exist in a bare
checkout, so the tests install stubs that mirror the real contracts closely
enough to exercise the plugin's own logic.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_INIT = REPO_ROOT / "kenari" / "__init__.py"

# Sentinel returned by the stub base-class ``fetch_models`` so tests can assert
# "this call fell through to the inherited /v1/models path".
SUPER_MARKER = ["__inherited_fetch_models__"]


@dataclass
class StubProviderProfile:
    """Mirrors the field set of ``providers.base.ProviderProfile``."""

    name: str
    api_mode: str = "chat_completions"
    aliases: tuple = ()
    display_name: str = ""
    description: str = ""
    signup_url: str = ""
    env_vars: tuple = ()
    base_url: str = ""
    models_url: str = ""
    auth_type: str = "api_key"
    supports_health_check: bool = True
    supports_vision: bool = False
    supports_vision_tool_messages: bool = True
    supports_prompt_cache_key: bool = False
    fallback_models: tuple = ()
    hostname: str = ""
    default_headers: dict = field(default_factory=dict)
    fixed_temperature: Any = None
    default_max_tokens: int | None = None
    default_aux_model: str = ""
    model_aliases: dict = field(default_factory=dict)

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        return list(SUPER_MARKER)

    def default_reasoning_config(self, model: str | None = None) -> dict | None:
        return None

    def supported_reasoning_efforts(self, model: str | None) -> tuple | None:
        return None

    def build_api_kwargs_extras(self, **kwargs: Any):
        return {}, {}

    def get_usage_cost(self, model: str, usage: Any) -> Any | None:
        return None

    def fetch_account_usage(self, **kwargs: Any) -> Any | None:
        return None

    def get_hostname(self) -> str:
        return self.hostname or ""

    def resolve_aux_model(self, *, vision: bool = False) -> str:
        return ""


LEGACY_SUPER_MARKER = ["__legacy_inherited_fetch_models__"]


@dataclass
class LegacyStubProviderProfile:
    """An older ``ProviderProfile``: no vision/cache/hostname fields, and a
    ``fetch_models`` that predates the ``base_url`` parameter.

    Third-party plugins cannot pin the Hermes build they run against, so
    the plugin must stay importable and functional on one of these.
    """

    name: str
    api_mode: str = "chat_completions"
    aliases: tuple = ()
    display_name: str = ""
    description: str = ""
    signup_url: str = ""
    env_vars: tuple = ()
    base_url: str = ""
    models_url: str = ""
    auth_type: str = "api_key"
    fallback_models: tuple = ()
    default_headers: dict = field(default_factory=dict)
    default_aux_model: str = ""

    def fetch_models(
        self, *, api_key: str | None = None, timeout: float = 8.0
    ) -> list[str] | None:
        return list(LEGACY_SUPER_MARKER)

    def default_reasoning_config(self, model: str | None = None) -> dict | None:
        return None


@dataclass(frozen=True)
class StubAccountUsageWindow:
    label: str
    used_percent: Any = None
    reset_at: Any = None
    detail: Any = None


@dataclass(frozen=True)
class StubAccountUsageSnapshot:
    provider: str
    source: str
    fetched_at: Any
    title: str = "Account limits"
    plan: Any = None
    windows: tuple = ()
    details: tuple = ()
    unavailable_reason: Any = None
    raw: Any = None


@dataclass(frozen=True)
class StubCostResult:
    amount_usd: Any = None
    status: str = "unknown"
    source: str = "none"
    label: str = "n/a"
    fetched_at: Any = None
    pricing_version: Any = None
    notes: tuple = ()


def _utc_now():
    return datetime.now(timezone.utc)


def install_stubs(*, legacy: bool = False) -> dict[str, Any]:
    """Install stub modules and return the provider registry they write into."""
    registry: dict[str, Any] = {}

    profile_cls = LegacyStubProviderProfile if legacy else StubProviderProfile

    base_mod = types.ModuleType("providers.base")
    base_mod.ProviderProfile = profile_cls
    base_mod.OMIT_TEMPERATURE = object()

    providers_mod = types.ModuleType("providers")
    providers_mod.ProviderProfile = profile_cls
    providers_mod.register_provider = lambda profile: registry.__setitem__(
        profile.name, profile
    )
    providers_mod.base = base_mod

    security_mod = types.ModuleType("hermes_cli.urllib_security")
    security_mod.open_credentialed_url = lambda req, timeout=8.0: (_ for _ in ()).throw(
        AssertionError("test did not stub open_credentialed_url")
    )
    hermes_cli_mod = types.ModuleType("hermes_cli")
    hermes_cli_mod.__version__ = "0.0.0-test"
    hermes_cli_mod.urllib_security = security_mod

    account_mod = types.ModuleType("agent.account_usage")
    account_mod.AccountUsageSnapshot = StubAccountUsageSnapshot
    account_mod.AccountUsageWindow = StubAccountUsageWindow
    agent_mod = types.ModuleType("agent")
    agent_mod.account_usage = account_mod

    pricing_mod = types.ModuleType("agent.usage_pricing")
    pricing_mod.CostResult = StubCostResult

    sys.modules["providers"] = providers_mod
    sys.modules["providers.base"] = base_mod
    sys.modules["hermes_cli"] = hermes_cli_mod
    sys.modules["hermes_cli.urllib_security"] = security_mod
    sys.modules["agent"] = agent_mod
    sys.modules["agent.account_usage"] = account_mod
    sys.modules["agent.usage_pricing"] = pricing_mod
    return registry


def load_plugin(*, legacy: bool = False):
    """Import the plugin module fresh and return (module, registered_profile)."""
    registry = install_stubs(legacy=legacy)
    sys.modules.pop("kenari_plugin_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "kenari_plugin_under_test",
        PLUGIN_INIT,
        submodule_search_locations=[str(PLUGIN_INIT.parent)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["kenari_plugin_under_test"] = module
    spec.loader.exec_module(module)
    return module, registry["kenari"]
