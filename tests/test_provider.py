"""Offline tests for the Kenari provider profile."""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest_stub  # noqa: E402


def _catalog_payload(models):
    return json.dumps({"object": "list", "data": models}).encode()


def _entry(model_id, **kw):
    entry = {
        "id": model_id,
        "object": "model",
        "owned_by": "test",
        "endpoints": ["chat"],
        "tool_call": True,
    }
    entry.update(kw)
    return entry


class _FakeResponse(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(payload)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, body: bytes, code: int = 400):
        super().__init__(
            "https://kenari.id/v1/chat/completions", code, "Bad Request", {}, None
        )
        self._body = body

    def read(self, *args, **kwargs):
        return self._body


def _chat_models(*ids):
    return [_entry(i) for i in ids]


class TestRegistration(unittest.TestCase):
    def test_registers_as_kenari(self):
        _, profile = conftest_stub.load_plugin()
        self.assertEqual(profile.name, "kenari")

    def test_profile_fields(self):
        _, profile = conftest_stub.load_plugin()
        self.assertEqual(profile.base_url, "https://kenari.id/v1")
        self.assertEqual(profile.models_url, "https://kenari.id/v1/models")
        self.assertEqual(profile.env_vars, ("KENARI_API_KEY", "KENARI_BASE_URL"))
        self.assertEqual(profile.auth_type, "api_key")
        self.assertEqual(profile.api_mode, "chat_completions")
        self.assertEqual(profile.hostname, "kenari.id")
        self.assertTrue(profile.supports_vision)
        self.assertTrue(profile.supports_prompt_cache_key)
        # Aux default must be callable by scoped keys too: a :free route.
        self.assertTrue(profile.default_aux_model.endswith(":free"))

    def test_fallback_models_present(self):
        _, profile = conftest_stub.load_plugin()
        self.assertTrue(profile.fallback_models)
        self.assertIn("step-3-7-flash:free", profile.fallback_models)

    def test_importable_on_legacy_profile_base(self):
        # Older builds lack vision/cache/hostname/resolve_aux fields; the
        # plugin must still register rather than TypeError at import.
        _, profile = conftest_stub.load_plugin(legacy=True)
        self.assertEqual(profile.name, "kenari")
        self.assertEqual(profile.base_url, "https://kenari.id/v1")


class TestModelCapabilities(unittest.TestCase):
    def setUp(self):
        self.module, self.profile = conftest_stub.load_plugin()

    def test_curated_snapshot_covers_catalog(self):
        caps = self.module._MODEL_CAPABILITIES_CURATED
        self.assertGreaterEqual(len(caps), 90)
        entry = caps["step-3-7-flash"]
        self.assertEqual(entry["context_window"], 262144)
        self.assertTrue(entry["supports_tools"])
        self.assertTrue(entry["supports_vision"])
        self.assertTrue(entry["supports_reasoning"])
        self.assertEqual(entry["model_family"], "stepfun")

    def test_free_model_declared(self):
        entry = self.module._MODEL_CAPABILITIES_CURATED["step-3-7-flash:free"]
        self.assertTrue(entry["supports_tools"])

    def test_non_tool_model_declared(self):
        entry = self.module._MODEL_CAPABILITIES_CURATED.get("gpt-image-2")
        if entry is not None:
            self.assertFalse(entry["supports_tools"])

    def test_profile_carries_capabilities(self):
        self.assertIn("step-3-7-flash", self.profile.model_capabilities)
        self.assertEqual(
            self.profile.model_capabilities["step-3-7-flash"]["context_window"], 262144
        )

    def test_explicit_overrides_not_clobbered(self):
        # The plugin only supplies the declaration; user model_overrides
        # win in core — nothing here may force values past the profile.
        self.assertIsInstance(self.profile.model_capabilities, dict)

    def test_live_refresh_updates_profile(self):
        payload = _catalog_payload([
            _entry("brand-new-model",
                   context_length=777000, tool_call=True,
                   modalities={"input": ["text"], "output": ["text"]},
                   reasoning=False, owned_by="newvendor"),
        ])
        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(
            mod, "open_credentialed_url",
            lambda req, timeout=8.0: _FakeResponse(payload),
        ):
            self.module._CATALOG_CACHE = None
            self.profile.fetch_models()
        self.assertEqual(
            self.profile.model_capabilities["brand-new-model"]["context_window"],
            777000,
        )
        self.assertEqual(
            self.module._MODEL_CAPABILITIES_LIVE["brand-new-model"]["model_family"],
            "newvendor",
        )


class TestFetchModels(unittest.TestCase):
    def setUp(self):
        self.module, self.profile = conftest_stub.load_plugin()
        self.module._CATALOG_CACHE = None
        self.module._PRICING = {}
        self.module._REASONING_CAPS = {}
        self.module._MODEL_META = {}
        self.module._KEY_ALLOW_CACHE = {}

    def _patch_opener(self, payload):
        mod = sys.modules["hermes_cli.urllib_security"]
        return patch.object(
            mod, "open_credentialed_url", lambda req, timeout=8.0: _FakeResponse(payload)
        )

    def test_filters_to_tool_capable_chat(self):
        payload = _catalog_payload([
            _entry("step-3-7-flash"),
            _entry("gpt-image-2", endpoints=["images"], tool_call=False),
            _entry("kling-v3", endpoints=["videos"], tool_call=False),
            _entry("plain-chat", tool_call=False),
            _entry("step-3-7-flash:free"),
        ])
        with self._patch_opener(payload):
            models = self.profile.fetch_models()
        self.assertEqual(models, ["step-3-7-flash", "step-3-7-flash:free"])

    def test_records_pricing_and_reasoning(self):
        payload = _catalog_payload([
            _entry("step-3-7-flash",
                   reasoning=True, reasoning_options=["low", "medium", "high"],
                   modalities={"input": ["text", "image"], "output": ["text"]},
                   pricing={"input": 4200000000, "output": 24000000000,
                            "cache_read": 840000000, "cache_write": None,
                            "free": False, "currency": "IDR",
                            "unit": "micro_idr_per_1m_tokens"}),
            _entry("step-3-7-flash:free",
                   pricing={"free": True, "currency": "IDR",
                            "unit": "micro_idr_per_1m_tokens"}),
        ])
        with self._patch_opener(payload):
            self.profile.fetch_models()
        mod = sys.modules["kenari_plugin_under_test"]
        self.assertEqual(
            mod._REASONING_CAPS["step-3-7-flash"]["options"], ["low", "medium", "high"]
        )
        self.assertEqual(mod._PRICING["step-3-7-flash"]["input"], 4200000000)
        self.assertTrue(mod._PRICING["step-3-7-flash:free"]["free"])
        self.assertTrue(mod._MODEL_META["step-3-7-flash"]["vision"])
        self.assertEqual(self.profile.supported_reasoning_efforts("step-3-7-flash"),
                         ("low", "medium", "high"))

    def test_falls_back_when_catalog_empty(self):
        with self._patch_opener(_catalog_payload([])):
            models = self.profile.fetch_models()
        self.assertEqual(models, conftest_stub.SUPER_MARKER)

    def test_falls_back_when_all_filtered(self):
        payload = _catalog_payload([
            _entry("m/e", endpoints=["images"], tool_call=False),
            _entry("m/t", tool_call=False),
        ])
        with self._patch_opener(payload):
            models = self.profile.fetch_models()
        self.assertEqual(models, conftest_stub.SUPER_MARKER)

    def test_falls_back_on_bad_payload(self):
        with self._patch_opener(b"not json"):
            models = self.profile.fetch_models()
        self.assertEqual(models, conftest_stub.SUPER_MARKER)

    def test_falls_back_on_transport_error(self):
        def boom(req, timeout=8.0):
            raise OSError("no route")

        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", boom):
            models = self.profile.fetch_models()
        self.assertEqual(models, conftest_stub.SUPER_MARKER)

    def test_custom_base_url_skips_catalog(self):
        # A proxy/relay catalog is not ours to interpret — generic path only.
        with self._patch_opener(_catalog_payload(_chat_models("a/b"))):
            models = self.profile.fetch_models(base_url="https://relay.internal/v1")
        self.assertEqual(models, conftest_stub.SUPER_MARKER)

    def test_legacy_base_signature_still_falls_back(self):
        module, profile = conftest_stub.load_plugin(legacy=True)
        module._CATALOG_CACHE = None
        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(
            mod, "open_credentialed_url",
            lambda req, timeout=8.0: (_ for _ in ()).throw(OSError("down")),
        ):
            models = profile.fetch_models()
        self.assertEqual(models, conftest_stub.LEGACY_SUPER_MARKER)


class TestKeyScopeFiltering(unittest.TestCase):
    ALLOWED_BODY = (
        b'{"error": {"code": "bad_request", "message": '
        b'"model \'x\' is not allowed by this key. '
        b'Allowed models: step-3-7-flash:free, hy3:free", '
        b'"param": null, "type": "bad_request"}}'
    )

    def setUp(self):
        self.module, self.profile = conftest_stub.load_plugin()
        self.module._CATALOG_CACHE = [
            "step-3-7-flash", "step-3-7-flash:free", "hy3:free", "gpt-oss-20b",
        ]
        self.module._KEY_ALLOW_CACHE = {}
        self.module._MODEL_META = {
            "step-3-7-flash:free": {"vision": True},
            "hy3:free": {"vision": False},
        }

    def _probe_opener(self, calls):
        def opener(req, timeout=8.0):
            url = req.full_url
            if url.endswith("/chat/completions"):
                calls.append(url)
                raise _FakeHTTPError(self.ALLOWED_BODY, 400)
            return _FakeResponse(_catalog_payload([]))

        mod = sys.modules["hermes_cli.urllib_security"]
        return patch.object(mod, "open_credentialed_url", opener)

    def test_parse_allowed_models(self):
        parsed = self.module._parse_allowed_models(self.ALLOWED_BODY.decode())
        self.assertEqual(parsed, frozenset({"step-3-7-flash:free", "hy3:free"}))

    def test_parse_returns_none_without_list(self):
        self.assertIsNone(self.module._parse_allowed_models('{"error": {"code": "x"}}'))
        self.assertIsNone(self.module._parse_allowed_models(""))

    def test_scoped_key_filters_picker(self):
        with self._probe_opener([]):
            models = self.profile.fetch_models(api_key="kn-scoped")
        self.assertEqual(models, ["step-3-7-flash:free", "hy3:free"])

    def test_scope_probe_cached_per_key(self):
        calls = []
        with self._probe_opener(calls):
            self.profile.fetch_models(api_key="kn-scoped")
            self.profile.fetch_models(api_key="kn-scoped")
        self.assertEqual(len(calls), 1)

    def test_unrestricted_key_no_filtering(self):
        def opener(req, timeout=8.0):
            if req.full_url.endswith("/chat/completions"):
                raise _FakeHTTPError(b'{"error": {"code": "model_not_found"}}', 404)
            return _FakeResponse(_catalog_payload([]))

        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", opener):
            models = self.profile.fetch_models(api_key="kn-full")
        self.assertEqual(
            models, ["step-3-7-flash", "step-3-7-flash:free", "hy3:free", "gpt-oss-20b"]
        )

    def test_bad_key_no_filtering(self):
        def opener(req, timeout=8.0):
            if req.full_url.endswith("/chat/completions"):
                raise _FakeHTTPError(b"invalid api key", 401)
            return _FakeResponse(_catalog_payload([]))

        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", opener):
            models = self.profile.fetch_models(api_key="kn-bad")
        self.assertEqual(len(models), 4)

    def test_no_key_no_probe(self):
        def boom(req, timeout=8.0):
            raise AssertionError("no network without a key")

        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", boom):
            models = self.profile.fetch_models()
        self.assertEqual(len(models), 4)

    def test_resolve_aux_model_key_aware(self):
        with self._probe_opener([]):
            with patch.dict(os.environ, {"KENARI_API_KEY": "kn-scoped"}):
                self.assertEqual(self.profile.resolve_aux_model(), "step-3-7-flash:free")
                self.assertEqual(
                    self.profile.resolve_aux_model(vision=True), "step-3-7-flash:free"
                )

    def test_resolve_aux_model_vision_prefers_capable(self):
        module = self.module
        module._MODEL_META = {
            "step-3-7-flash:free": {"vision": False},
            "hy3:free": {"vision": True},
        }
        module._KEY_ALLOW_CACHE = {}
        with self._probe_opener([]):
            with patch.dict(os.environ, {"KENARI_API_KEY": "kn-scoped"}):
                self.assertEqual(
                    self.profile.resolve_aux_model(vision=True), "hy3:free"
                )

    def test_resolve_aux_model_empty_without_key(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KENARI_API_KEY", None)
            self.assertEqual(self.profile.resolve_aux_model(), "")


class TestReasoningMapping(unittest.TestCase):
    def setUp(self):
        self.module, self.profile = conftest_stub.load_plugin()
        self.module._REASONING_CAPS = {
            "step-3-7-flash": {"reasoning": True, "options": ["low", "medium", "high"]},
            "hy3:free": {"reasoning": True, "options": ["none", "low", "high"]},
            "plain": {"reasoning": False, "options": []},
        }

    def test_default_config_is_medium(self):
        self.assertEqual(
            self.profile.default_reasoning_config(),
            {"enabled": True, "effort": "medium"},
        )

    def test_effort_forwarded_verbatim_when_supported(self):
        extra, top = self.profile.build_api_kwargs_extras(
            reasoning_config={"effort": "high"}, model="step-3-7-flash"
        )
        self.assertEqual(extra, {"reasoning": {"enabled": True, "effort": "high"}})
        self.assertEqual(top, {})

    def test_effort_clamped_down_to_supported(self):
        extra, _ = self.profile.build_api_kwargs_extras(
            reasoning_config={"effort": "xhigh"}, model="step-3-7-flash"
        )
        self.assertEqual(extra["reasoning"]["effort"], "high")

    def test_none_disables(self):
        extra, _ = self.profile.build_api_kwargs_extras(
            reasoning_config={"effort": "none"}, model="step-3-7-flash"
        )
        self.assertEqual(extra, {"reasoning": {"enabled": False}})
        extra, _ = self.profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model="step-3-7-flash"
        )
        self.assertEqual(extra, {"reasoning": {"enabled": False}})

    def test_unset_config_defaults_medium(self):
        extra, _ = self.profile.build_api_kwargs_extras(
            reasoning_config=None, model="step-3-7-flash", supports_reasoning=False
        )
        self.assertEqual(extra, {"reasoning": {"enabled": True, "effort": "medium"}})

    def test_unknown_model_passes_through(self):
        extra, _ = self.profile.build_api_kwargs_extras(
            reasoning_config={"effort": "max"}, model="unknown-model"
        )
        self.assertEqual(extra["reasoning"]["effort"], "max")

    def test_supported_efforts_declared(self):
        self.assertEqual(
            self.profile.supported_reasoning_efforts("step-3-7-flash"),
            ("low", "medium", "high"),
        )
        self.assertIsNone(self.profile.supported_reasoning_efforts("plain"))
        self.assertIsNone(self.profile.supported_reasoning_efforts("unknown"))


class TestUsageCost(unittest.TestCase):
    def setUp(self):
        self.module, self.profile = conftest_stub.load_plugin()
        self.module._PRICING = {
            "step-3-7-flash": {
                "input": 4200000000, "output": 24000000000,
                "cache_read": 840000000, "cache_write": None, "free": False,
            },
            "step-3-7-flash:free": {
                "input": 4200000000, "output": 24000000000,
                "cache_read": None, "cache_write": None, "free": True,
            },
        }

    def test_free_model_costs_zero(self):
        res = self.profile.get_usage_cost(
            "step-3-7-flash:free", {"input_tokens": 1000, "output_tokens": 500}
        )
        self.assertEqual(res.status, "estimated")
        self.assertIn("Rp0", res.label)

    def test_paid_model_math(self):
        # 1M input @4.2e9/u (=Rp4.200) + 1M output @2.4e10/u (=Rp24.000)
        # -> Rp28.200 total.
        res = self.profile.get_usage_cost(
            "step-3-7-flash",
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
             "cache_read_tokens": 0, "cache_write_tokens": 0},
        )
        self.assertTrue(res.label.startswith("Rp28.200 "))

    def test_cache_write_falls_back_to_input_rate(self):
        a = self.profile.get_usage_cost(
            "step-3-7-flash",
            {"input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_write_tokens": 1_000_000},
        )
        # 1M cache-write @ input rate 4.2e9/u = Rp4.200.
        self.assertTrue(a.label.startswith("Rp4.200 "))

    def test_unknown_model_returns_none(self):
        self.assertIsNone(self.profile.get_usage_cost("nope", {}))

    def test_works_with_object_usage(self):
        class U:
            input_tokens = 100
            output_tokens = 50
            cache_read_tokens = 0
            cache_write_tokens = 0

        res = self.profile.get_usage_cost("step-3-7-flash", U())
        self.assertEqual(res.source, "provider_models_api")


class TestFmtRp(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(self.module_fmt(21_500_000_000), "Rp21.500")

    def test_none_is_unknown(self):
        self.assertEqual(self.module_fmt(None), "Rp?")

    def module_fmt(self, v):
        _, profile = conftest_stub.load_plugin()
        import sys as _sys

        mod = _sys.modules["kenari_plugin_under_test"]
        return mod._fmt_rp(v)


class TestFetchAccountUsage(unittest.TestCase):
    def setUp(self):
        self.module, self.profile = conftest_stub.load_plugin()
        self.calls = []

    def _opener(self, routes):
        def opener(req, timeout=8.0):
            url = req.full_url
            self.calls.append(url)
            for suffix, (status, payload) in routes.items():
                if url.endswith(suffix):
                    if status == 200:
                        return _FakeResponse(json.dumps(payload).encode())
                    raise _FakeHTTPError(json.dumps(payload).encode(), status)
            raise _FakeHTTPError(b"{}", 404)

        mod = sys.modules["hermes_cli.urllib_security"]
        return patch.object(mod, "open_credentialed_url", opener)

    def test_scoped_key_renders_usage_without_account(self):
        routes = {
            "/account/balance": (403, {"error": {"code": "restricted_key_not_allowed"}}),
            "/account/quota": (403, {"error": {"code": "restricted_key_not_allowed"}}),
            "/usage?range=7d": (200, {"total": {"requests": 10, "cost_micro_idr": 0}}),
            "/usage?range=30d": (200, {"total": {"requests": 312, "cost_micro_idr": 0}}),
            "/api/public/pricing": (200, {"free_tier": {"daily": 50}}),
            "/usage?range=today": (200, {"total": {"requests": 5, "cost_micro_idr": 0}}),
        }
        with self._opener(routes):
            snap = self.profile.fetch_account_usage(api_key="kn-scoped")
        self.assertEqual(snap.provider, "kenari")
        self.assertFalse(snap.windows)
        joined = "\n".join(snap.details)
        self.assertIn("Last 7d: 10 requests", joined)
        self.assertIn("Last 30d: 312 requests", joined)
        self.assertIn("~45 of 50/day left", joined)
        self.assertIn("model-scoped", joined)

    def test_full_key_renders_balance_and_plan_windows(self):
        routes = {
            "/account/balance": (200, {
                "object": "account.balance", "balance_micro_idr": 19589000000,
                "reserved_micro_idr": 0, "available_micro_idr": 19589000000,
                "currency": "IDR",
            }),
            "/account/quota": (200, {
                "plan": {"name": "Indie", "windows": {
                    "week": {"used_rp": 10000, "remaining_rp": 65000,
                             "resets_at": "2026-10-15T00:00:00Z"},
                    "month": {"used_rp": 20000, "remaining_rp": 280000,
                              "resets_at": "2026-11-01T00:00:00Z"},
                }},
                "coupon": None,
            }),
            "/usage?range=7d": (200, {"total": {"requests": 3, "cost_micro_idr": 5000000}}),
            "/usage?range=30d": (200, {"total": {"requests": 9, "cost_micro_idr": 9000000}}),
            "/api/public/pricing": (200, {"free_tier": {"daily": 50}}),
            "/usage?range=today": (200, {"total": {"requests": 1, "cost_micro_idr": 1000000}}),
        }
        with self._opener(routes):
            snap = self.profile.fetch_account_usage(api_key="kn-full")
        self.assertEqual(snap.plan, "Indie")
        labels = [w.label for w in snap.windows]
        self.assertEqual(labels, ["Plan weekly", "Plan monthly"])
        weekly = snap.windows[0]
        self.assertAlmostEqual(weekly.used_percent, 10000 / 75000 * 100)
        self.assertIn("Rp65.000 left", weekly.detail)
        joined = "\n".join(snap.details)
        self.assertIn("Rp19.589 available", joined)
        self.assertIn("Last 7d: 3 requests", joined)

    def test_no_key_returns_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KENARI_API_KEY", None)
            self.assertIsNone(self.profile.fetch_account_usage())

    def test_public_pricing_uses_browser_ua(self):
        # kenari.id's WAF 403s Python-urllib/* — the public-pricing fetch
        # must identify with an honest non-urllib UA.
        seen = {}

        def opener(req, timeout=8.0):
            url = req.full_url
            if url.endswith("/account/balance") or url.endswith("/account/quota"):
                raise _FakeHTTPError(b"{}", 403)
            if url.endswith("/api/public/pricing"):
                seen["ua"] = req.get_header("User-agent")
                return _FakeResponse(json.dumps({"free_tier": {"daily": 50}}).encode())
            return _FakeResponse(
                json.dumps({"total": {"requests": 1, "cost_micro_idr": 0}}).encode()
            )

        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", opener):
            self.profile.fetch_account_usage(api_key="kn-scoped")
        self.assertIn("hermes-kenari-plugin", seen.get("ua", ""))
        self.assertNotIn("Python-urllib", seen.get("ua", ""))

    def test_all_failing_returns_none(self):
        def boom(req, timeout=8.0):
            raise OSError("down")

        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", boom):
            self.assertIsNone(self.profile.fetch_account_usage(api_key="k"))


if __name__ == "__main__":
    unittest.main()
