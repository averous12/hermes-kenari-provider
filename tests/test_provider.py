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

    def test_fallback_models_empty_by_design(self):
        # A curated list would be unioned into every picker result and, for a
        # model-scoped key, would offer models the gateway rejects. The live
        # catalog (intersected with the key scope) is the only source.
        _, profile = conftest_stub.load_plugin()
        self.assertEqual(profile.fallback_models, ())

    def test_fallback_models_empty_legacy_too(self):
        _, profile = conftest_stub.load_plugin(legacy=True)
        self.assertEqual(profile.fallback_models, ())

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
        self.assertEqual(models, ["step-3-7-flash:free", "step-3-7-flash"])

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
        self.assertEqual(models, ["hy3:free", "step-3-7-flash:free"])

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
            models, ["hy3:free", "step-3-7-flash:free", "gpt-oss-20b", "step-3-7-flash"]
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

    def test_plan_percentages_map_from_quota_windows(self):
        # The statusbar chip renders PLAN REMAINING percentage, not request
        # counts: week/month windows -> remaining_percent = 100 - used.
        from datetime import datetime, timezone

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kenari-helper" / "dashboard"))
        import importlib

        import plugin_api  # noqa: E402

        importlib.reload(plugin_api)
        from conftest_stub import StubAccountUsageSnapshot, StubAccountUsageWindow

        snapshot = StubAccountUsageSnapshot(
            provider="kenari", source="test", fetched_at=datetime.now(timezone.utc),
            title="Kenari usage", plan="Indie",
            windows=(
                StubAccountUsageWindow(
                    label="Plan weekly", used_percent=16.0,
                    reset_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
                    detail="Rp63.000 left of Rp75.000",
                ),
                StubAccountUsageWindow(
                    label="Plan monthly", used_percent=4.0,
                    reset_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
                    detail="Rp288.000 left of Rp300.000",
                ),
            ),
            details=("Balance: Rp19.589 available",), raw={},
        )
        payload = plugin_api._snapshot_to_payload(snapshot)
        self.assertEqual(payload["plan"]["week_remaining_percent"], 84.0)
        self.assertEqual(payload["plan"]["month_remaining_percent"], 96.0)
        self.assertEqual(payload["plan"]["name"], "Indie")
        self.assertIn("Rp63.000 left", payload["plan"]["week_detail"])

    def test_plan_none_without_quota_windows(self):
        from datetime import datetime, timezone

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kenari-helper" / "dashboard"))
        import importlib

        import plugin_api  # noqa: E402

        importlib.reload(plugin_api)
        from conftest_stub import StubAccountUsageSnapshot

        snapshot = StubAccountUsageSnapshot(
            provider="kenari", source="test", fetched_at=datetime.now(timezone.utc),
            title="Kenari usage", plan=None, windows=(),
            details=("Last 7d: 565 requests",), raw={},
        )
        payload = plugin_api._snapshot_to_payload(snapshot)
        self.assertIsNone(payload["plan"])
        self.assertTrue(payload["ok"])

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


class TestPlanOnlyFiltering(unittest.TestCase):
    """`KENARI_PLAN_ONLY` / config `plan_only` — show only plan-covered models.

    The load-bearing case is the one with NO active plan: the filter must
    fail OPEN (show everything the key can call). Hiding models there would
    empty the picker, so that behaviour is asserted explicitly.
    """

    PLANS = [
        {"id": "plan-ringan-v3", "name": "Indie",
         "scope_models": ["step-3-7-flash:free", "hy3:free"]},
        {"id": "plan-agensi-v3", "name": "Agensi",
         "scope_models": ["step-3-7-flash:free", "hy3:free", "gpt-oss-20b"]},
    ]

    def setUp(self):
        self.module, self.profile = conftest_stub.load_plugin()
        self.module._CATALOG_CACHE = [
            "step-3-7-flash", "step-3-7-flash:free", "hy3:free", "gpt-oss-20b",
        ]
        self.module._KEY_ALLOW_CACHE = {}
        self.module._PLAN_SCOPE_CACHE.update({"at": 0.0, "models": None, "plan": "", "resolved": False})
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("KENARI_PLAN_ONLY", None)
        # Isolate from the developer's real config.yaml — it may well have
        # plan_only turned on, which would silently flip every expectation.
        self._config = patch.object(self.module, "_load_config_file", lambda: {})
        self._config.start()

    def tearDown(self):
        self._config.stop()
        self._env.stop()
        self.module._PLAN_SCOPE_CACHE.update({"at": 0.0, "models": None, "plan": "", "resolved": False})

    def _opener(self, plan_name, *, quota_status=200, plans=None):
        """Fake transport: catalog, quota, and plans endpoints."""
        def opener(req, timeout=8.0):
            url = req.full_url
            if url.endswith("/chat/completions"):
                raise _FakeHTTPError(
                    b'{"error": {"code": "bad_request", "message": '
                    b'"Allowed models: step-3-7-flash:free, hy3:free, gpt-oss-20b"}}',
                    400,
                )
            if url.endswith("/account/quota"):
                if quota_status != 200:
                    raise _FakeHTTPError(b"forbidden", quota_status)
                return _FakeResponse(json.dumps(
                    {"plan": {"name": plan_name}} if plan_name else {}
                ).encode())
            if url.endswith("/api/plans"):
                return _FakeResponse(json.dumps(plans if plans is not None else self.PLANS).encode())
            return _FakeResponse(_catalog_payload([]))
        return opener

    # ── toggle resolution ───────────────────────────────────────────────

    def test_toggle_defaults_off(self):
        self.assertFalse(self.module._plan_only_enabled())

    def test_toggle_env_truthy(self):
        for raw in ("1", "true", "TRUE", "yes", "on"):
            with patch.dict(os.environ, {"KENARI_PLAN_ONLY": raw}):
                self.assertTrue(self.module._plan_only_enabled(), raw)

    def test_toggle_env_falsy(self):
        for raw in ("0", "false", "no", "off"):
            with patch.dict(os.environ, {"KENARI_PLAN_ONLY": raw}):
                self.assertFalse(self.module._plan_only_enabled(), raw)

    def test_toggle_env_garbage_is_unset_not_guessed(self):
        # A typo must not silently hide models.
        with patch.dict(os.environ, {"KENARI_PLAN_ONLY": "maybe"}):
            self.assertFalse(self.module._plan_only_enabled())

    def test_toggle_reads_config_when_env_unset(self):
        fake_cfg = {"kenari": {"plan_only": True}}
        with patch.object(self.module, "_load_config_file", lambda: fake_cfg):
            self.assertTrue(self.module._plan_only_enabled())

    def test_toggle_reads_desktop_settings_form(self):
        """The switch written by the desktop form wins over the legacy key."""
        fake_cfg = {
            "plugins": {"entries": {"kenari-helper": {"settings": {"plan_only": True}}}}
        }
        with patch.object(self.module, "_load_config_file", lambda: fake_cfg):
            self.assertTrue(self.module._plan_only_enabled())

    def test_desktop_form_beats_legacy_key(self):
        fake_cfg = {
            "plugins": {"entries": {"kenari-helper": {"settings": {"plan_only": False}}}},
            "kenari": {"plan_only": True},
        }
        with patch.object(self.module, "_load_config_file", lambda: fake_cfg):
            self.assertFalse(self.module._plan_only_enabled())

    def test_returns_real_bool_never_none(self):
        """A falsy setting must return False, not None (an `or` chain leak)."""
        with patch.object(self.module, "_load_config_file", lambda: {"kenari": {"plan_only": False}}):
            self.assertIs(self.module._plan_only_enabled(), False)
        with patch.object(self.module, "_load_config_file", lambda: {}):
            self.assertIs(self.module._plan_only_enabled(), False)

    def test_reads_toggle_from_helper_plugin(self):
        """The switch lives on kenari-helper; the provider must read that key."""
        fake_cfg = {
            "plugins": {"entries": {"kenari-helper": {"settings": {"plan_only": True}}}}
        }
        with patch.object(self.module, "_load_config_file", lambda: fake_cfg):
            self.assertTrue(self.module._plan_only_enabled())

    def test_helper_key_outranks_legacy_provider_key(self):
        fake_cfg = {
            "plugins": {
                "entries": {
                    "kenari-helper": {"settings": {"plan_only": False}},
                    "model-providers/kenari": {"settings": {"plan_only": True}},
                }
            }
        }
        with patch.object(self.module, "_load_config_file", lambda: fake_cfg):
            self.assertFalse(self.module._plan_only_enabled())

    def test_legacy_provider_key_still_read(self):
        """An install from before the move keeps working."""
        fake_cfg = {
            "plugins": {"entries": {"model-providers/kenari": {"settings": {"plan_only": True}}}}
        }
        with patch.object(self.module, "_load_config_file", lambda: fake_cfg):
            self.assertTrue(self.module._plan_only_enabled())

    def test_toggle_paths_all_target_the_plan_only_field(self):
        """Every candidate path must end in the field the writer saves."""
        for path in self.module._PLAN_ONLY_SETTINGS_PATHS:
            self.assertEqual(path[-1], "plan_only")
            self.assertEqual(path[:2], ("plugins", "entries"))
            self.assertIn("kenari", path[2])

    def test_config_read_does_not_depend_on_hermes_cli_config(self):
        """The toggle must not need `hermes_cli.config` to be importable.

        That import pulls in `hermes_yaml` (a repo-root module) which is not
        always on sys.path when the plugin loader imports a provider; the
        ImportError was swallowed and the toggle silently read as OFF, so the
        plan filter never ran. Read config.yaml directly instead.
        """
        source = Path(self.module.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            "hermes_cli.config", source,
            "provider must not import hermes_cli.config for the toggle",
        )

    def test_minimal_yaml_fallback_reads_the_toggle(self):
        """With no YAML library, the narrow fallback still finds the boolean."""
        text = (
            "plugins:\n"
            "  entries:\n"
            "    kenari-helper:\n"
            "      settings:\n"
            "        plan_only: true\n"
            "model:\n"
            "  default: step-3-7-flash:free\n"
        )
        tree = self.module._minimal_yaml_bool_tree(text)
        self.assertIs(tree["plugins"]["entries"]["kenari-helper"]["settings"]["plan_only"], True)
        path = ("plugins", "entries", "kenari-helper", "settings", "plan_only")
        with patch.object(self.module, "_load_config_file", lambda: tree):
            self.assertTrue(self.module._plan_only_enabled())

    def test_minimal_yaml_fallback_ignores_comments_and_quotes(self):
        text = 'plugins:  # root\n  entries:\n    "kenari-helper":\n      settings:\n        plan_only: false\n'
        tree = self.module._minimal_yaml_bool_tree(text)
        self.assertIs(tree["plugins"]["entries"]["kenari-helper"]["settings"]["plan_only"], False)

    def test_unreadable_config_reads_as_unset_not_off(self):
        """A missing file must return None (unset), so the caller can tell."""
        with patch.object(self.module, "_load_config_file", lambda: None):
            self.assertIsNone(
                self.module._config_bool(("plugins", "entries", "kenari-helper", "settings", "plan_only"))
            )

    def test_non_bool_setting_is_ignored(self):
        fake_cfg = {"plugins": {"entries": {"kenari": {"settings": {"plan_only": "yes"}}}}}
        with patch.object(self.module, "_load_config_file", lambda: fake_cfg):
            self.assertFalse(self.module._plan_only_enabled())

    def test_toggle_env_beats_config(self):
        fake_cfg = {"kenari": {"plan_only": True}}
        with patch.dict(os.environ, {"KENARI_PLAN_ONLY": "0"}):
            with patch.object(self.module, "_load_config_file", lambda: fake_cfg):
                self.assertFalse(self.module._plan_only_enabled())

    # ── filtering ───────────────────────────────────────────────────────

    def test_on_filters_to_plan_scope(self):
        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", self._opener("Indie")):
            with patch.dict(os.environ, {"KENARI_PLAN_ONLY": "1"}):
                models = self.profile.fetch_models(api_key="kn-full")
        # key allows 3, plan Indie covers 2
        self.assertEqual(models, ["hy3:free", "step-3-7-flash:free"])

    def test_off_shows_key_scope(self):
        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", self._opener("Indie")):
            models = self.profile.fetch_models(api_key="kn-full")
        self.assertEqual(
            models, ["hy3:free", "step-3-7-flash:free", "gpt-oss-20b"]
        )

    def test_higher_plan_covers_more(self):
        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", self._opener("Agensi")):
            with patch.dict(os.environ, {"KENARI_PLAN_ONLY": "1"}):
                models = self.profile.fetch_models(api_key="kn-full")
        self.assertIn("gpt-oss-20b", models)

    # ── the no-active-plan case (must fail OPEN) ────────────────────────

    def test_no_plan_in_quota_shows_everything(self):
        """Quota readable but carries no plan (no active subscription)."""
        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", self._opener("")):
            with patch.dict(os.environ, {"KENARI_PLAN_ONLY": "1"}):
                models = self.profile.fetch_models(api_key="kn-full")
        self.assertEqual(
            models, ["hy3:free", "step-3-7-flash:free", "gpt-oss-20b"],
            "no active plan must show all key-allowed models, never none",
        )

    def test_scoped_key_403_on_quota_shows_everything(self):
        """The model-scoped-key case: /account/quota 403s -> no filter."""
        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", self._opener("Indie", quota_status=403)):
            with patch.dict(os.environ, {"KENARI_PLAN_ONLY": "1"}):
                models = self.profile.fetch_models(api_key="kn-scoped")
        self.assertEqual(models, ["hy3:free", "step-3-7-flash:free", "gpt-oss-20b"])

    def test_unknown_plan_name_shows_everything(self):
        """Quota names a plan /api/plans does not list."""
        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", self._opener("Mystery")):
            with patch.dict(os.environ, {"KENARI_PLAN_ONLY": "1"}):
                models = self.profile.fetch_models(api_key="kn-full")
        self.assertEqual(len(models), 3)

    def test_plan_with_empty_scope_shows_everything(self):
        mod = sys.modules["hermes_cli.urllib_security"]
        plans = [{"id": "x", "name": "Indie", "scope_models": []}]
        with patch.object(mod, "open_credentialed_url", self._opener("Indie", plans=plans)):
            with patch.dict(os.environ, {"KENARI_PLAN_ONLY": "1"}):
                models = self.profile.fetch_models(api_key="kn-full")
        self.assertEqual(len(models), 3)

    def test_plan_endpoint_unreachable_shows_everything(self):
        def opener(req, timeout=8.0):
            url = req.full_url
            if url.endswith("/chat/completions"):
                raise _FakeHTTPError(
                    b'{"error": {"code": "bad_request", "message": '
                    b'"Allowed models: step-3-7-flash:free, hy3:free, gpt-oss-20b"}}',
                    400,
                )
            if url.endswith("/account/quota"):
                return _FakeResponse(json.dumps({"plan": {"name": "Indie"}}).encode())
            raise OSError("plans endpoint down")

        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", opener):
            with patch.dict(os.environ, {"KENARI_PLAN_ONLY": "1"}):
                models = self.profile.fetch_models(api_key="kn-full")
        self.assertEqual(len(models), 3)

    def test_no_api_key_never_calls_plan_endpoints(self):
        """Without a key there is nothing to scope — and no network at all."""
        seen = []

        def opener(req, timeout=8.0):
            seen.append(req.full_url)
            raise AssertionError("no network without a key")

        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", opener):
            with patch.dict(os.environ, {"KENARI_PLAN_ONLY": "1"}):
                models = self.profile.fetch_models()
        self.assertEqual(len(models), 4)
        self.assertEqual(seen, [])

    def test_plan_request_sends_user_agent(self):
        """`/api/plans` is behind Cloudflare: a UA-less GET gets Error 1010."""
        seen = {}

        def opener(req, timeout=8.0):
            url = req.full_url
            if url.endswith("/chat/completions"):
                raise _FakeHTTPError(
                    b'{"error": {"code": "bad_request", "message": '
                    b'"Allowed models: step-3-7-flash:free, hy3:free, gpt-oss-20b"}}',
                    400,
                )
            if url.endswith("/account/quota"):
                return _FakeResponse(json.dumps({"plan": {"name": "Indie"}}).encode())
            if url.endswith("/api/plans"):
                seen["ua"] = req.get_header("User-agent") or req.headers.get("User-agent")
                return _FakeResponse(json.dumps(self.PLANS).encode())
            return _FakeResponse(_catalog_payload([]))

        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", opener):
            with patch.dict(os.environ, {"KENARI_PLAN_ONLY": "1"}):
                models = self.profile.fetch_models(api_key="kn-full")
        self.assertIn("hermes-kenari-plugin", seen.get("ua", ""))
        self.assertEqual(models, ["hy3:free", "step-3-7-flash:free"])

    def test_scope_cache_avoids_refetch(self):
        quota_calls = []

        def opener(req, timeout=8.0):
            url = req.full_url
            if url.endswith("/chat/completions"):
                raise _FakeHTTPError(
                    b'{"error": {"code": "bad_request", "message": '
                    b'"Allowed models: step-3-7-flash:free, hy3:free, gpt-oss-20b"}}',
                    400,
                )
            if url.endswith("/account/quota"):
                quota_calls.append(url)
                return _FakeResponse(json.dumps({"plan": {"name": "Indie"}}).encode())
            if url.endswith("/api/plans"):
                return _FakeResponse(json.dumps(self.PLANS).encode())
            return _FakeResponse(_catalog_payload([]))

        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", opener):
            with patch.dict(os.environ, {"KENARI_PLAN_ONLY": "1"}):
                self.profile.fetch_models(api_key="kn-full")
                self.profile.fetch_models(api_key="kn-full")
        self.assertEqual(len(quota_calls), 1, "plan scope should be cached")

    def test_failure_cached_does_not_retry_storm(self):
        """A 403 must not be re-probed on every picker open."""
        quota_calls = []

        def opener(req, timeout=8.0):
            url = req.full_url
            if url.endswith("/chat/completions"):
                raise _FakeHTTPError(
                    b'{"error": {"code": "bad_request", "message": '
                    b'"Allowed models: step-3-7-flash:free, hy3:free, gpt-oss-20b"}}',
                    400,
                )
            if url.endswith("/account/quota"):
                quota_calls.append(url)
                raise _FakeHTTPError(b"forbidden", 403)
            return _FakeResponse(_catalog_payload([]))

        mod = sys.modules["hermes_cli.urllib_security"]
        with patch.object(mod, "open_credentialed_url", opener):
            with patch.dict(os.environ, {"KENARI_PLAN_ONLY": "1"}):
                self.profile.fetch_models(api_key="kn-scoped")
                self.profile.fetch_models(api_key="kn-scoped")
        self.assertEqual(len(quota_calls), 1)


class TestFreeCacheOrdering(unittest.TestCase):
    """Three tiers — cache-free, then `:free` routes, then the rest — each
    alphabetical. The head of the list is what the picker shows first."""

    def setUp(self):
        self.module, self.profile = conftest_stub.load_plugin()

    def test_three_tiers_each_alphabetical(self):
        models = [
            "zzz-paid", "hy3:free", "deepseek-v4-1-flash", "aaa-paid",
            "agnes-3-0-flash:free", "mimo-v2-6-flash",
        ]
        out = self.module._sort_models_by_tier(
            models, {"deepseek-v4-1-flash", "mimo-v2-6-flash"}
        )
        self.assertEqual(
            out,
            [
                # 1. cache-free, alphabetical
                "deepseek-v4-1-flash", "mimo-v2-6-flash",
                # 2. free routes, alphabetical
                "agnes-3-0-flash:free", "hy3:free",
                # 3. the rest, alphabetical
                "aaa-paid", "zzz-paid",
            ],
        )

    def test_free_route_outranks_paid_even_without_cache_free(self):
        models = ["bbb-paid", "zzz:free", "aaa-paid"]
        out = self.module._sort_models_by_tier(models, set())
        self.assertEqual(out, ["zzz:free", "aaa-paid", "bbb-paid"])

    def test_free_route_with_cache_free_base_is_tier_two(self):
        """Already free outright — the cheaper cache rate is moot."""
        models = ["deepseek-v4-flash", "deepseek-v4-flash:free"]
        out = self.module._sort_models_by_tier(models, {"deepseek-v4-flash"})
        self.assertEqual(out, ["deepseek-v4-flash", "deepseek-v4-flash:free"])

    def test_no_free_cache_still_splits_free_from_paid(self):
        models = ["c", "b:free", "a"]
        out = self.module._sort_models_by_tier(models, set())
        self.assertEqual(out, ["b:free", "a", "c"])

    def test_all_paid_is_plain_alphabetical(self):
        models = ["c", "a", "b"]
        self.assertEqual(self.module._sort_models_by_tier(models, set()), ["a", "b", "c"])

    def test_every_input_survives_exactly_once(self):
        models = ["b", "a:free", "c", "d:free", "deepseek-v4-flash"]
        out = self.module._sort_models_by_tier(models, {"deepseek-v4-flash"})
        self.assertEqual(sorted(out), sorted(models))
        self.assertEqual(len(out), len(models))

    def test_no_api_key_makes_no_network_call(self):
        """Sorting needs plan data, but with no key there is nothing to fetch."""
        seen = []

        def opener(req, timeout=8.0):
            seen.append(req.full_url)
            raise AssertionError("no network without a key")

        mod = sys.modules["hermes_cli.urllib_security"]
        self.module._CATALOG_CACHE = ["b-model", "a-model"]
        self.module._KEY_ALLOW_CACHE = {}
        self.module._PLAN_SCOPE_CACHE.update(
            {"at": 0.0, "models": None, "plan": "", "free_cache": set(), "resolved": False}
        )
        with patch.object(self.module, "open_credentialed_url", opener, create=True):
            with patch.object(mod, "open_credentialed_url", opener):
                self.profile.fetch_models()
        self.assertEqual(seen, [])

    def test_tiers_apply_with_plan_only_off(self):
        """Sorting is independent of the filter toggle."""
        fake_plans = [
            {"name": "Indie",
             "scope_models": ["a-model", "b-model"],
             "free_cache_models": ["b-model"]},
        ]

        def opener(req, timeout=8.0):
            url = req.full_url
            if url.endswith("/account/quota"):
                return _FakeResponse(json.dumps({"plan": {"name": "Indie"}}).encode())
            if url.endswith("/api/plans"):
                return _FakeResponse(json.dumps(fake_plans).encode())
            return _FakeResponse(_catalog_payload([]))

        mod = sys.modules["hermes_cli.urllib_security"]
        self.module._CATALOG_CACHE = ["a-model", "b-model", "c-model", "d-model:free"]
        self.module._KEY_ALLOW_CACHE = {}
        self.module._PLAN_SCOPE_CACHE.update(
            {"at": 0.0, "models": None, "plan": "", "free_cache": set(), "resolved": False}
        )
        with patch.object(self.module, "_plan_only_enabled", lambda: False):
            with patch.object(mod, "open_credentialed_url", opener):
                with patch.dict(os.environ, {"KENARI_API_KEY": "kn-full"}):
                    models = self.profile.fetch_models(api_key="kn-full")
        # b-model cache-free -> first; then the free route; then the rest.
        self.assertEqual(models, ["b-model", "d-model:free", "a-model", "c-model"])


if __name__ == "__main__":
    unittest.main()
