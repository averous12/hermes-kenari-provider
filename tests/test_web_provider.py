"""Offline tests for the kenari-web backend plugin.

Loads ``kenari-web/provider.py`` under a faithful stub of the web-search ABC
(``agent.web_search_provider``) so the response mapping and error handling are
exercised without a Hermes tree or network. The live contract was verified
separately against the real registry and the real ``web_search_tool``.

Run: python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

REPO = Path(__file__).resolve().parent.parent
WEB_DIR = REPO / "kenari-web"


# ── stub the ABC exactly as the plugin uses it ──────────────────────────────
def _install_abc_stub() -> types.ModuleType:
    agent = sys.modules.get("agent") or types.ModuleType("agent")
    provider_base = types.ModuleType("agent.provider_base")

    class ProviderBase:
        """Marker base; the real one is a detail-free mixin."""

    provider_base.ProviderBase = ProviderBase

    abc_mod = types.ModuleType("agent.web_search_provider")
    env_store: dict[str, str] = {}

    class WebSearchProvider(ProviderBase):
        def is_available(self) -> bool:  # pragma: no cover - abstract in real ABC
            raise NotImplementedError

        def is_keyless_available(self) -> bool:
            return False

        def supports_search(self) -> bool:
            return True

        def supports_extract(self) -> bool:
            return False

        def search(self, query, limit=5):
            raise NotImplementedError

        def extract(self, urls, **kwargs):
            raise NotImplementedError

    def get_provider_env(name: str) -> str:
        return (env_store.get(name) or "").strip()

    abc_mod.WebSearchProvider = WebSearchProvider
    abc_mod.get_provider_env = get_provider_env
    abc_mod.__env_store__ = env_store

    agent.provider_base = provider_base
    agent.web_search_provider = abc_mod
    sys.modules["agent"] = agent
    sys.modules["agent.provider_base"] = provider_base
    sys.modules["agent.web_search_provider"] = abc_mod
    return abc_mod


ABC = _install_abc_stub()


def load_provider():
    """Import the real provider.py fresh, with stubbed env values."""
    import importlib.util

    for mod in list(sys.modules):
        if mod == "kenari_web_provider":
            del sys.modules[mod]
    spec = importlib.util.spec_from_file_location("kenari_web_provider", WEB_DIR / "provider.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["kenari_web_provider"] = module
    spec.loader.exec_module(module)
    return module


def set_env(**values: str) -> None:
    ABC.__env_store__.clear()
    ABC.__env_store__.update(values)


class TestKeyResolutionLayers(unittest.TestCase):
    """The key must be found whichever way the Provider page saved it.

    Two distinct write paths exist and they do NOT land in the same place: the
    canonical ``KENARI_API_KEY`` (which is what ``get_provider_env`` reads) and
    the desktop card's ``model.key_env`` pointer to ``$HERMES_HOME/.env``. When
    only the pointer exists, web search used to report itself unavailable while
    chat worked — silently, because nothing errored.
    """

    def setUp(self):
        set_env()
        self.mod = load_provider()

    def tearDown(self):
        set_env()

    def test_canonical_env_var_wins(self):
        set_env(KENARI_API_KEY="kn-canonical")
        self.assertEqual(self.mod._api_key(), "kn-canonical")

    def test_falls_back_to_model_key_env_pointer(self):
        """The bug: key present only via the desktop pointer."""
        cfg = {"model": {"provider": "kenari", "key_env": "HERMES_CUSTOM_KENARI_API_KEY"}}
        with patch.dict(sys.modules, {"hermes_cli.config": _fake_config_mod(
                load_config=lambda: cfg,
                get_env_value_prefer_dotenv=lambda n: "kn-from-pointer"
                if n == "HERMES_CUSTOM_KENARI_API_KEY" else "")}):
            self.assertEqual(self.mod._api_key(), "kn-from-pointer")

    def test_pointer_ignored_when_model_provider_is_something_else(self):
        cfg = {"model": {"provider": "openrouter", "key_env": "SOME_OTHER_KEY"}}
        with patch.dict(sys.modules, {"hermes_cli.config": _fake_config_mod(
                load_config=lambda: cfg,
                get_env_value_prefer_dotenv=lambda n: "not-kenaris-key")}):
            self.assertEqual(
                self.mod._api_key(), "",
                "another provider's pointer must not be read as a Kenari key")

    def test_falls_back_to_provider_credential_resolver(self):
        """Credential-pool setups resolve only through the provider's own path."""
        resolver = types.ModuleType("hermes_cli.auth")
        resolver.resolve_api_key_provider_credentials = (
            lambda pid: {"api_key": "kn-pooled", "source": "credential_pool"}
        )
        with patch.dict(sys.modules, {
            "hermes_cli.config": _fake_config_mod(load_config=lambda: {}, get_env_value_prefer_dotenv=lambda n: ""),
            "hermes_cli.auth": resolver,
        }):
            self.assertEqual(self.mod._api_key(), "kn-pooled")

    def test_no_key_anywhere_is_empty_not_an_exception(self):
        with patch.dict(sys.modules, {
            "hermes_cli.config": _fake_config_mod(load_config=lambda: {}, get_env_value_prefer_dotenv=lambda n: ""),
        }):
            self.assertEqual(self.mod._api_key(), "")

    def test_availability_matches_key_resolution(self):
        """is_available() must never disagree with what the request path uses."""
        set_env(KENARI_API_KEY="kn-canonical")
        self.assertTrue(self.mod.KenariWebSearchProvider().is_available())
        set_env()
        self.assertFalse(self.mod.KenariWebSearchProvider().is_available())

    def test_availability_true_via_pointer_only(self):
        set_env()
        cfg = {"model": {"provider": "kenari", "key_env": "HERMES_CUSTOM_KENARI_API_KEY"}}
        with patch.dict(sys.modules, {"hermes_cli.config": _fake_config_mod(
                load_config=lambda: cfg,
                get_env_value_prefer_dotenv=lambda n: "kn-from-pointer")}):
            self.assertTrue(
                self.mod.KenariWebSearchProvider().is_available(),
                "a pointer-only key must still count as available")

    def test_headers_use_the_resolved_key(self):
        set_env()
        cfg = {"model": {"provider": "kenari", "key_env": "HERMES_CUSTOM_KENARI_API_KEY"}}
        with patch.dict(sys.modules, {"hermes_cli.config": _fake_config_mod(
                load_config=lambda: cfg,
                get_env_value_prefer_dotenv=lambda n: "kn-from-pointer")}):
            self.assertEqual(self.mod._headers()["Authorization"], "Bearer kn-from-pointer")

    def test_config_layer_failure_does_not_break_resolution(self):
        broken = types.ModuleType("hermes_cli.config")
        broken.load_config = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        broken.get_env_value_prefer_dotenv = lambda n: ""
        with patch.dict(sys.modules, {"hermes_cli.config": broken}):
            self.assertEqual(self.mod._api_key(), "", "a broken config layer must degrade, not raise")


def _fake_config_mod(**fns) -> types.ModuleType:
    mod = types.ModuleType("hermes_cli.config")
    for name, fn in fns.items():
        setattr(mod, name, fn)
    return mod


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "https://kenari.id/v1/web/search"),
                response=self,
            )


class TestKenariWebProvider(unittest.TestCase):
    def setUp(self):
        set_env(KENARI_API_KEY="kn-test")
        self.mod = load_provider()
        self.provider = self.mod.KenariWebSearchProvider()

    # ── identity / capability ───────────────────────────────────────────

    def test_name_and_display_name(self):
        self.assertEqual(self.provider.name, "kenari")
        self.assertEqual(self.provider.display_name, "Kenari")

    def test_supports_both_capabilities(self):
        self.assertTrue(self.provider.supports_search())
        self.assertTrue(self.provider.supports_extract())

    def test_is_available_true_with_key(self):
        self.assertTrue(self.provider.is_available())

    def test_is_available_false_without_key(self):
        set_env()
        self.assertFalse(self.provider.is_available())

    def test_never_claims_keyless(self):
        """Kenari has no anonymous tier — claiming one would hijack keyed users."""
        self.assertFalse(self.provider.is_keyless_available())

    # ── search ──────────────────────────────────────────────────────────

    def test_search_maps_response(self):
        payload = {"results": [
            {"title": "A", "url": "https://a.test", "content": "snippet a"},
            {"title": "B", "url": "https://b.test", "content": "snippet b"},
        ]}
        with patch.object(httpx, "post", return_value=_FakeResponse(payload)):
            out = self.provider.search("q", limit=5)
        self.assertTrue(out["success"])
        rows = out["data"]["web"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {"title": "A", "url": "https://a.test", "description": "snippet a", "position": 1})
        self.assertEqual(rows[1]["position"], 2)

    def test_search_trims_to_limit(self):
        """The endpoint may return more rows than asked for."""
        payload = {"results": [{"title": str(i), "url": f"https://{i}.test", "content": ""} for i in range(10)]}
        with patch.object(httpx, "post", return_value=_FakeResponse(payload)):
            out = self.provider.search("q", limit=3)
        self.assertEqual(len(out["data"]["web"]), 3)

    def test_search_sends_query_and_limit(self):
        seen = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            seen.update({"url": url, "json": json, "headers": headers})
            return _FakeResponse({"results": []})

        with patch.object(httpx, "post", fake_post):
            self.provider.search("hello world", limit=4)
        self.assertTrue(seen["url"].endswith("/web/search"))
        self.assertEqual(seen["json"], {"query": "hello world", "limit": 4})

    def test_search_sends_user_agent(self):
        """Kenari is behind Cloudflare: a UA-less client gets Error 1010."""
        seen = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            seen.update(headers or {})
            return _FakeResponse({"results": []})

        with patch.object(httpx, "post", fake_post):
            self.provider.search("q")
        self.assertIn("hermes-kenari-plugin", seen.get("User-Agent", ""))

    def test_base_url_override_respected(self):
        set_env(KENARI_API_KEY="kn-test", KENARI_BASE_URL="https://relay.internal/v1")
        mod = load_provider()
        seen = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            seen["url"] = url
            return _FakeResponse({"results": []})

        with patch.object(httpx, "post", fake_post):
            mod.KenariWebSearchProvider().search("q")
        self.assertTrue(seen["url"].startswith("https://relay.internal/v1"))

    # ── search failures ─────────────────────────────────────────────────

    def test_search_401_mentions_key(self):
        with patch.object(httpx, "post", return_value=_FakeResponse({"error": "bad key"}, 401)):
            out = self.provider.search("q")
        self.assertFalse(out["success"])
        self.assertIn("401", out["error"])
        self.assertIn("KENARI_API_KEY", out["error"])

    def test_search_402_mentions_allowance(self):
        with patch.object(httpx, "post", return_value=_FakeResponse({}, 402)):
            out = self.provider.search("q")
        self.assertFalse(out["success"])
        self.assertIn("web-search allowance", out["error"])

    def test_search_unreachable_is_an_error_not_a_raise(self):
        with patch.object(httpx, "post", side_effect=httpx.ConnectError("boom")):
            out = self.provider.search("q")
        self.assertFalse(out["success"])
        self.assertIn("Could not reach Kenari", out["error"])

    def test_search_bad_shape_is_an_error(self):
        with patch.object(httpx, "post", return_value=_FakeResponse({"unexpected": 1})):
            out = self.provider.search("q")
        self.assertFalse(out["success"])

    def test_search_non_json_is_an_error(self):
        with patch.object(httpx, "post", return_value=_FakeResponse("<html>", 200)):
            out = self.provider.search("q")
        self.assertFalse(out["success"])

    def test_search_skips_non_dict_rows(self):
        payload = {"results": ["junk", {"title": "ok", "url": "https://ok.test", "content": ""}]}
        with patch.object(httpx, "post", return_value=_FakeResponse(payload)):
            out = self.provider.search("q")
        self.assertEqual(len(out["data"]["web"]), 1)
        self.assertEqual(out["data"]["web"][0]["title"], "ok")

    # ── extract ─────────────────────────────────────────────────────────

    def test_extract_returns_document_shape(self):
        payload = {"title": "Example", "content": "body text", "links": [], "id": "x", "cost_micro_idr": 0}
        with patch.object(httpx, "post", return_value=_FakeResponse(payload)):
            rows = self.provider.extract(["https://example.com"])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["url"], "https://example.com")
        self.assertEqual(row["title"], "Example")
        self.assertEqual(row["content"], "body text")
        # raw_content mirrors content for the legacy pipeline.
        self.assertEqual(row["raw_content"], "body text")

    def test_extract_failure_is_per_url(self):
        """One bad URL must not fail the whole batch."""
        calls = {"n": 0}

        def fake_post(url, json=None, headers=None, timeout=None):
            calls["n"] += 1
            if json["url"].endswith("bad"):
                return _FakeResponse({}, 500)
            return _FakeResponse({"title": "T", "content": "C"})

        with patch.object(httpx, "post", fake_post):
            rows = self.provider.extract(["https://good.test", "https://bad"])
        self.assertEqual(len(rows), 2)
        self.assertNotIn("error", rows[0])
        self.assertIn("error", rows[1])
        self.assertEqual(rows[1]["url"], "https://bad")

    def test_extract_unreachable_marks_each_url(self):
        with patch.object(httpx, "post", side_effect=httpx.ConnectError("boom")):
            rows = self.provider.extract(["https://a.test", "https://b.test"])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.get("error") for r in rows))

    def test_extract_empty_list(self):
        self.assertEqual(self.provider.extract([]), [])

    # ── setup schema ────────────────────────────────────────────────────

    def test_setup_schema_shape(self):
        schema = self.provider.get_setup_schema()
        self.assertEqual(schema["name"], "Kenari")
        self.assertEqual(schema["env_vars"][0]["key"], "KENARI_API_KEY")

    # ── packaging ───────────────────────────────────────────────────────

    def test_plugin_yaml_is_a_backend_providing_kenari(self):
        text = (WEB_DIR / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("kind: backend", text)
        self.assertIn("provides_web_providers:", text)
        self.assertIn("- kenari", text)

    def test_entry_point_registers_the_provider(self):
        text = (WEB_DIR / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("register_web_search_provider", text)
        # Relative import: a plugin's module name is install-specific, so an
        # absolute `plugins.web.*` import would only work for a bundled copy.
        self.assertIn("from .provider import", text)

    def test_provider_avoids_bundled_internals(self):
        """Must not depend on plugins.web._common (bundled-only import path)."""
        text = (WEB_DIR / "provider.py").read_text(encoding="utf-8")
        self.assertNotIn("plugins.web._common", text)
        self.assertNotIn("from plugins.web", text)


if __name__ == "__main__":
    unittest.main()
