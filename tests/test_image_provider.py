"""Offline tests for the kenari-image backend plugin.

Loads ``kenari-image/__init__.py`` under a faithful stub of the image-gen ABC
(``agent.image_gen_provider``) with a stubbed ``httpx``, so request shaping,
response mapping and error handling are exercised without a Hermes tree or
network. The live contract was verified separately against the real registry
and the real ``image_generate`` tool.

Run: python -m unittest discover -s tests
"""

from __future__ import annotations

import base64
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
IMAGE_DIR = REPO / "kenari-image"

PNG_1PX = base64.b64encode(
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
).decode()


# ── stubs ──────────────────────────────────────────────────────────────────


def _install_abc_stub() -> types.ModuleType:
    # ADDITIVE: another suite may already have published an ``agent`` package
    # with its own stubs. Replacing the module object would strand those
    # children (they'd keep pointing at the old one) and break that suite.
    agent = sys.modules.get("agent")
    if agent is None:
        agent = types.ModuleType("agent")
        sys.modules["agent"] = agent

    mod = types.ModuleType("agent.image_gen_provider")
    env_store: dict[str, str] = {}
    saved: list = []

    class ImageGenProvider:
        name = "stub"

        def is_available(self) -> bool:  # pragma: no cover
            return True

        def capabilities(self):
            return {"modalities": ["text"], "max_reference_images": 0}

        def list_models(self):
            return []

        def generate(self, *a, **k):  # pragma: no cover
            raise NotImplementedError

    def resolve_aspect_ratio(value):
        v = value.strip().lower() if isinstance(value, str) else ""
        return v if v in ("landscape", "square", "portrait") else "landscape"

    def normalize_reference_images(value):
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return None
        return [i.strip() for i in value if isinstance(i, str) and i.strip()] or None

    def save_b64_image(b64_data, *, prefix="image", extension="png"):
        saved.append(("b64", len(b64_data)))
        return Path(f"/cache/{prefix}_x.png")

    def save_url_image(url, *, prefix="image", **kw):
        saved.append(("url", url))
        return Path(f"/cache/{prefix}_y.png")

    def success_response(*, image, model, prompt, aspect_ratio, provider, modality="text", extra=None):
        payload = {"success": True, "image": image, "model": model, "prompt": prompt,
                   "aspect_ratio": aspect_ratio, "modality": modality, "provider": provider}
        for k, v in (extra or {}).items():
            payload.setdefault(k, v)
        return payload

    def error_response(*, error, error_type="provider_error", provider="", model="", prompt="",
                       aspect_ratio="landscape"):
        return {"success": False, "image": None, "error": error, "error_type": error_type,
                "model": model, "prompt": prompt, "aspect_ratio": aspect_ratio, "provider": provider}

    mod.ImageGenProvider = ImageGenProvider
    mod.DEFAULT_ASPECT_RATIO = "landscape"
    mod.resolve_aspect_ratio = resolve_aspect_ratio
    mod.normalize_reference_images = normalize_reference_images
    mod.save_b64_image = save_b64_image
    mod.save_url_image = save_url_image
    mod.success_response = success_response
    mod.error_response = error_response
    mod.__env_store__ = env_store
    mod.__saved__ = saved

    ws = types.ModuleType("agent.web_search_provider")
    ws.get_provider_env = lambda name: (env_store.get(name) or "").strip()

    fs = types.ModuleType("agent.file_safety")
    fs.raise_if_read_blocked = lambda path: None

    agent.image_gen_provider = mod
    agent.file_safety = fs
    sys.modules["agent.image_gen_provider"] = mod
    sys.modules["agent.file_safety"] = fs
    # Only claim get_provider_env if this suite owns the module; a peer suite
    # may have a richer web_search_provider stub in place.
    existing_ws = sys.modules.get("agent.web_search_provider")
    if existing_ws is None or not hasattr(existing_ws, "get_provider_env"):
        agent.web_search_provider = ws
        sys.modules["agent.web_search_provider"] = ws
    return mod


ABC = _install_abc_stub()


class _Resp:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        return None


def _install_httpx_stub() -> types.ModuleType:
    mod = types.ModuleType("httpx")
    state = {"posts": [], "gets": [], "post_response": _Resp(), "get_response": _Resp()}

    class RequestError(Exception):
        pass

    def post(url, **kw):
        state["posts"].append({"url": url, **kw})
        return state["post_response"]

    def get(url, **kw):
        state["gets"].append({"url": url, **kw})
        return state["get_response"]

    mod.post = post
    mod.get = get
    mod.RequestError = RequestError
    mod.__state__ = state
    # NOT registered in sys.modules: mutating the global httpx would break the
    # other provider suites, which need the real module. It is injected onto
    # the loaded plugin module instead (see load_provider).
    return mod


HTTPX = _install_httpx_stub()


def load_provider():
    import importlib.util

    global ABC
    # Re-install every run: the other test modules replace agent.* stubs with
    # their own, and discovery imports them before anything executes.
    ABC = _install_abc_stub()

    for name in ("kenari_image_provider",):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("kenari_image_provider", IMAGE_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["kenari_image_provider"] = module
    real_httpx = sys.modules.get("httpx")
    sys.modules["httpx"] = HTTPX
    try:
        spec.loader.exec_module(module)
    finally:
        if real_httpx is not None:
            sys.modules["httpx"] = real_httpx
        else:
            sys.modules.pop("httpx", None)
    module.httpx = HTTPX
    return module


def set_env(**values):
    ABC.__env_store__.clear()
    ABC.__env_store__.update(values)


def reset_http():
    HTTPX.__state__["posts"].clear()
    HTTPX.__state__["gets"].clear()
    HTTPX.__state__["post_response"] = _Resp()
    HTTPX.__state__["get_response"] = _Resp()


class Base(unittest.TestCase):
    def setUp(self):
        reset_http()
        self.mod = load_provider()
        ABC.__saved__.clear()

        # ``get_provider_env`` is imported from agent.web_search_provider at
        # CALL time, and a peer suite may own that module with a different env
        # store. Bind it to ours for the duration of the test, restoring after.
        self._ws_mod = sys.modules.get("agent.web_search_provider")
        self._orig_gpe = getattr(self._ws_mod, "get_provider_env", None)
        if self._ws_mod is not None:
            self._ws_mod.get_provider_env = lambda name: (ABC.__env_store__.get(name) or "").strip()

        set_env(KENARI_API_KEY="kn-test")
        self.mod._CATALOG_CACHE.update({"at": 0.0, "models": None})

    def tearDown(self):
        set_env()
        if self._ws_mod is not None and self._orig_gpe is not None:
            self._ws_mod.get_provider_env = self._orig_gpe


# ── availability / catalog ─────────────────────────────────────────────────


class TestAvailability(Base):
    def test_available_with_key(self):
        self.assertTrue(self.mod.KenariImageGenProvider().is_available())

    def test_unavailable_without_key(self):
        set_env()
        self.assertFalse(self.mod.KenariImageGenProvider().is_available())

    def test_capabilities_declare_editing(self):
        caps = self.mod.KenariImageGenProvider().capabilities()
        self.assertIn("image", caps["modalities"])
        self.assertEqual(caps["max_reference_images"], 16)

    def test_curated_catalog_has_free_routes(self):
        set_env()  # no key -> dynamic fetch skipped, curated used
        rows = self.mod.KenariImageGenProvider().list_models()
        self.assertTrue(rows)
        free = [r for r in rows if r["price"] == "free"]
        self.assertEqual(len(free), 3, "the three agnes free routes")
        self.assertTrue(all(r["id"].endswith(":free") for r in free))

    def test_dynamic_catalog_filters_to_image_output_models(self):
        HTTPX.__state__["get_response"] = _Resp(200, {"data": [
            {"id": "agnes-image-2-5-flash:free", "modalities": {"input": ["text"], "output": ["image"]}},
            {"id": "deepseek-v4-1-flash", "modalities": {"input": ["text"], "output": ["text"]}},
            {"id": "nano-banana-pro", "modalities": {"input": ["text", "image"], "output": ["image"]}},
            {"id": "weird"},  # no modalities -> skipped
        ]})
        rows = {r["id"] for r in self.mod.KenariImageGenProvider().list_models()}
        self.assertEqual(rows, {"agnes-image-2-5-flash:free", "nano-banana-pro"})

    def test_catalog_failure_falls_back_to_curated(self):
        HTTPX.__state__["get_response"] = _Resp(500, ValueError("boom"))
        rows = self.mod.KenariImageGenProvider().list_models()
        self.assertEqual(len(rows), len(self.mod.CURATED_MODELS))

    def test_catalog_request_uses_ua_and_bearer(self):
        self.mod.KenariImageGenProvider().list_models()
        req = HTTPX.__state__["gets"][0]
        self.assertEqual(req["headers"]["User-Agent"], "hermes-kenari-plugin")
        self.assertEqual(req["headers"]["Authorization"], "Bearer kn-test")


# ── key resolution layers ──────────────────────────────────────────────────


class TestKeyResolution(Base):
    def test_canonical_env_var(self):
        self.assertEqual(self.mod._api_key(), "kn-test")

    def test_falls_back_to_model_key_env_pointer(self):
        set_env()
        cfg = {"model": {"provider": "kenari", "key_env": "HERMES_CUSTOM_KENARI_API_KEY"}}
        fake = types.ModuleType("hermes_cli.config")
        fake.load_config = lambda: cfg
        fake.get_env_value_prefer_dotenv = lambda n: "kn-ptr" if n == "HERMES_CUSTOM_KENARI_API_KEY" else ""
        with patch.dict(sys.modules, {"hermes_cli.config": fake}):
            self.assertEqual(self.mod._api_key(), "kn-ptr")

    def test_pointer_ignored_for_other_providers(self):
        set_env()
        cfg = {"model": {"provider": "openrouter", "key_env": "OTHER"}}
        fake = types.ModuleType("hermes_cli.config")
        fake.load_config = lambda: cfg
        fake.get_env_value_prefer_dotenv = lambda n: "not-kenari"
        with patch.dict(sys.modules, {"hermes_cli.config": fake}):
            self.assertEqual(self.mod._api_key(), "")

    def test_credential_resolver_fallback(self):
        set_env()
        fake_cfg = types.ModuleType("hermes_cli.config")
        fake_cfg.load_config = lambda: {}
        fake_cfg.get_env_value_prefer_dotenv = lambda n: ""
        fake_auth = types.ModuleType("hermes_cli.auth")
        fake_auth.resolve_api_key_provider_credentials = lambda p: {"api_key": "kn-pool"}
        with patch.dict(sys.modules, {"hermes_cli.config": fake_cfg, "hermes_cli.auth": fake_auth}):
            self.assertEqual(self.mod._api_key(), "kn-pool")

    def test_availability_agrees_with_pointer(self):
        set_env()
        cfg = {"model": {"provider": "kenari", "key_env": "CUSTOM"}}
        fake = types.ModuleType("hermes_cli.config")
        fake.load_config = lambda: cfg
        fake.get_env_value_prefer_dotenv = lambda n: "kn-ptr"
        with patch.dict(sys.modules, {"hermes_cli.config": fake}):
            self.assertTrue(self.mod.KenariImageGenProvider().is_available())


# ── model / size resolution ────────────────────────────────────────────────


class TestModelResolution(Base):
    def test_default_is_free_route(self):
        self.assertTrue(self.mod._resolve_model().endswith(":free"))

    def test_env_override(self):
        with patch.dict("os.environ", {"KENARI_IMAGE_MODEL": "gpt-image-2"}):
            self.assertEqual(self.mod._resolve_model(), "gpt-image-2")

    def test_sizes_are_openai_shaped(self):
        self.assertEqual(self.mod._size_for("square"), "1024x1024")
        self.assertEqual(self.mod._size_for("landscape"), "1536x1024")
        self.assertEqual(self.mod._size_for("portrait"), "1024x1536")
        self.assertEqual(self.mod._size_for("nonsense"), "1024x1024")


# ── generation ─────────────────────────────────────────────────────────────


class TestGenerate(Base):
    def test_text_to_image_posts_json_with_b64_format(self):
        HTTPX.__state__["post_response"] = _Resp(200, {"data": [{"b64_json": PNG_1PX}]})
        out = self.mod.KenariImageGenProvider().generate("a canary", "square")
        self.assertTrue(out["success"])
        self.assertEqual(out["modality"], "text")
        self.assertEqual(out["provider"], "kenari")
        self.assertEqual(out["size"], "1024x1024")
        req = HTTPX.__state__["posts"][0]
        self.assertTrue(req["url"].endswith("/images/generations"))
        self.assertEqual(req["json"]["response_format"], "b64_json")
        self.assertEqual(req["json"]["size"], "1024x1024")
        self.assertEqual(req["json"]["n"], 1)
        self.assertEqual(req["headers"]["User-Agent"], "hermes-kenari-plugin")

    def test_empty_prompt_is_rejected_without_a_request(self):
        out = self.mod.KenariImageGenProvider().generate("   ", "square")
        self.assertFalse(out["success"])
        self.assertEqual(out["error_type"], "bad_request")
        self.assertEqual(HTTPX.__state__["posts"], [])

    def test_missing_key_reports_auth_required(self):
        set_env()
        out = self.mod.KenariImageGenProvider().generate("a canary", "square")
        self.assertFalse(out["success"])
        self.assertEqual(out["error_type"], "auth_required")
        self.assertIn("KENARI_API_KEY", out["error"])

    def test_edit_uses_multipart_and_image_field(self):
        HTTPX.__state__["post_response"] = _Resp(200, {"data": [{"b64_json": PNG_1PX}]})
        src = REPO / "tests" / "_tmp_src.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        try:
            out = self.mod.KenariImageGenProvider().generate("make it blue", "square", image_url=str(src))
        finally:
            src.unlink(missing_ok=True)
        self.assertTrue(out["success"])
        self.assertEqual(out["modality"], "image")
        req = HTTPX.__state__["posts"][0]
        self.assertTrue(req["url"].endswith("/images/edits"))
        self.assertIn("image", [f[0] for f in req["files"]])
        self.assertNotIn("json", req)

    def test_edit_response_with_empty_b64_uses_data_uri_url(self):
        """The real edit endpoint returns b64_json:'' + a data: URI in url."""
        data_uri = "data:image/png;base64," + PNG_1PX
        HTTPX.__state__["post_response"] = _Resp(200, {"data": [{"b64_json": "", "url": data_uri}]})
        src = REPO / "tests" / "_tmp_src2.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        try:
            out = self.mod.KenariImageGenProvider().generate("x", "square", image_url=str(src))
        finally:
            src.unlink(missing_ok=True)
        self.assertTrue(out["success"], out.get("error"))
        kinds = [k for k, _ in ABC.__saved__]
        self.assertIn("b64", kinds, "a data URI must be decoded locally, never fetched")

    def test_plain_url_response_is_cached(self):
        HTTPX.__state__["post_response"] = _Resp(200, {"data": [{"url": "https://cdn.example/x.png"}]})
        out = self.mod.KenariImageGenProvider().generate("a canary", "square")
        self.assertTrue(out["success"])
        self.assertEqual(ABC.__saved__[-1], ("url", "https://cdn.example/x.png"))

    def test_no_image_data_is_empty_response(self):
        HTTPX.__state__["post_response"] = _Resp(200, {"data": [{}]})
        out = self.mod.KenariImageGenProvider().generate("a canary", "square")
        self.assertFalse(out["success"])
        self.assertEqual(out["error_type"], "empty_response")

    def test_reference_images_only_without_primary_still_edits(self):
        HTTPX.__state__["post_response"] = _Resp(200, {"data": [{"b64_json": PNG_1PX}]})
        src = REPO / "tests" / "_tmp_ref.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        try:
            out = self.mod.KenariImageGenProvider().generate(
                "blend", "square", reference_image_urls=[str(src)])
        finally:
            src.unlink(missing_ok=True)
        self.assertTrue(out["success"])
        self.assertTrue(HTTPX.__state__["posts"][0]["url"].endswith("/images/edits"))

    def test_network_error_is_reported_not_raised(self):
        def boom(*a, **k):
            raise HTTPX.RequestError("no route")
        HTTPX.__state__["post_response"] = _Resp(200)
        with patch.object(HTTPX, "post", boom):
            out = self.mod.KenariImageGenProvider().generate("a canary", "square")
        self.assertFalse(out["success"])
        self.assertEqual(out["error_type"], "network_error")


# ── HTTP error mapping ─────────────────────────────────────────────────────


class TestErrorMapping(Base):
    def _fail(self, status, payload=None):
        HTTPX.__state__["post_response"] = _Resp(status, payload or {"error": {"message": "detail here"}})
        return self.mod.KenariImageGenProvider().generate("a canary", "square")

    def test_401_is_auth_error(self):
        out = self._fail(401)
        self.assertFalse(out["success"])
        self.assertEqual(out["error_type"], "auth_error")
        self.assertIn("401", out["error"])

    def test_402_is_quota_error_with_actionable_text(self):
        out = self._fail(402)
        self.assertEqual(out["error_type"], "quota_error")
        self.assertIn("balance", out["error"].lower())

    def test_403_is_auth_error(self):
        self.assertEqual(self._fail(403)["error_type"], "auth_error")

    def test_429_is_rate_limited(self):
        self.assertEqual(self._fail(429)["error_type"], "rate_limited")

    def test_500_is_provider_error(self):
        self.assertEqual(self._fail(500)["error_type"], "provider_error")

    def test_detail_is_appended_once(self):
        out = self._fail(400, {"error": {"message": "model not found"}})
        self.assertIn("model not found", out["error"])
        self.assertEqual(out["error"].count("model not found"), 1)

    def test_non_json_body_does_not_crash(self):
        HTTPX.__state__["post_response"] = _Resp(400, ValueError("bad json"), text="<html>500</html>")
        out = self.mod.KenariImageGenProvider().generate("a canary", "square")
        self.assertFalse(out["success"])
        self.assertEqual(out["error_type"], "provider_error")


# ── data URI loading ──────────────────────────────────────────────────────


class TestLoadRef(Base):
    def test_data_uri_is_decoded_locally(self):
        data, name, ctype = self.mod._load_ref_bytes("data:image/png;base64," + PNG_1PX)
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertTrue(name.endswith(".png"))
        self.assertEqual(ctype, "image/png")

    def test_malformed_data_uri_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.mod._load_ref_bytes("data:image/png;base64,!!!!not-base64!!!!")

    def test_empty_ref_raises(self):
        with self.assertRaises(ValueError):
            self.mod._load_ref_bytes("   ")

    def test_local_png_detected(self):
        p = REPO / "tests" / "_tmp_load.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
        try:
            data, name, ctype = self.mod._load_ref_bytes(str(p))
        finally:
            p.unlink(missing_ok=True)
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertEqual(ctype, "image/png")


if __name__ == "__main__":
    unittest.main()
