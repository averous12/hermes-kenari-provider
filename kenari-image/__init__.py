"""Kenari.id image generation backend.

Serves Hermes' ``image_generate`` tool from Kenari's OpenAI-compatible surface:

* ``POST /v1/images/generations`` — text-to-image (JSON body).
* ``POST /v1/images/edits`` — edit/reference images (``multipart/form-data``,
  up to 16 source files).

Selected by ``image_gen.provider: kenari``; with the key present it is one of
the available providers. When unset, Hermes picks the single *available*
provider, so this only serves when it is the only image backend configured.

Rides the same ``KENARI_API_KEY`` as the model provider — no extra credential.
Three ``:free`` image routes cost Rp 0.

Response shapes differ between the two endpoints (measured live), so both are
handled: ``generations`` returns ``data[0].b64_json``; ``edits`` returns an
EMPTY ``b64_json`` with a ``data:image/png;base64,...`` URI in ``data[0].url``.
"""

from __future__ import annotations

import base64
import binascii
import logging
import mimetypes
import os
from typing import Any, Dict, List, Optional

import httpx

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
)

logger = logging.getLogger(__name__)

PROVIDER = "kenari"
KEY_ENV = "KENARI_API_KEY"
BASE_URL_ENV = "KENARI_BASE_URL"
DEFAULT_BASE_URL = "https://kenari.id/v1"
USER_AGENT = "hermes-kenari-plugin"
TIMEOUT = 180.0
MAX_REFERENCE_IMAGES = 16

# Kenari snaps the requested size to a discrete bucket set (measured on all
# three agnes routes — identical across them):
#
#   1:1   1024x1024, 2048x2048
#   3:2   1248x832   /  832x1248
#   16:9  1312x736   /  736x1312
#   4:3   1152x864   /  864x1152
#
# Anything off-bucket is rounded to the nearest (512x512 → 1024x1024,
# 1792x1024 → 1312x736), and unparseable sizes are rejected with HTTP 400.
# We therefore REQUEST the exact buckets so what is asked for is what comes
# back; other backends get the same [OI]-ish values and snap as they see fit.
SIZES = {
    "landscape": "1248x832",
    "square": "1024x1024",
    "portrait": "832x1248",
}

# Larger 1:1 tier, offered explicitly because the model honors it.
SIZES_LARGE = {
    "landscape": "1312x736",
    "square": "2048x2048",
    "portrait": "736x1312",
}

DEFAULT_MODEL = "agnes-image-2-5-flash:free"

# Fallback catalog, used only when /v1/models is unreachable. Mirrors the live
# image-output set; the dynamic fetch is authoritative.
CURATED_MODELS: Dict[str, Dict[str, Any]] = {
    "agnes-image-2-0-flash:free": {"display": "Agnes Image 2.0 Flash (free)", "speed": "Fast"},
    "agnes-image-2-1-flash:free": {"display": "Agnes Image 2.1 Flash (free)", "speed": "Fast"},
    "agnes-image-2-5-flash:free": {"display": "Agnes Image 2.5 Flash (free)", "speed": "Fast"},
    "gpt-image-2": {"display": "GPT Image 2", "speed": "Moderate"},
    "grok-imagine-image": {"display": "Grok Imagine Image", "speed": "Fast"},
    "grok-imagine-image-2-0": {"display": "Grok Imagine Image 2.0", "speed": "Fast"},
    "grok-imagine-image-quality": {"display": "Grok Imagine Image (Quality)", "speed": "Slower"},
    "hunyuan-image-alpha": {"display": "Hunyuan Image Alpha", "speed": "Moderate"},
    "hunyuan-image-v2.0": {"display": "Hunyuan Image 2.0", "speed": "Moderate"},
    "hunyuan-image-v3.0": {"display": "Hunyuan Image 3.0", "speed": "Moderate"},
    "hunyuan-image-v3.0-art": {"display": "Hunyuan Image 3.0 Art", "speed": "Moderate"},
    "nano-banana": {"display": "Nano Banana", "speed": "Fast"},
    "nano-banana-2": {"display": "Nano Banana 2", "speed": "Fast"},
    "nano-banana-2-lite": {"display": "Nano Banana 2 Lite", "speed": "Fast"},
    "nano-banana-pro": {"display": "Nano Banana Pro", "speed": "Slower"},
    "qwen-image-2.0-pro": {"display": "Qwen Image 2.0 Pro", "speed": "Moderate"},
    "qwen-image-3.0-pro": {"display": "Qwen Image 3.0 Pro", "speed": "Moderate"},
}

_CATALOG_CACHE: Dict[str, Any] = {"at": 0.0, "models": None}


# ── credential / endpoint resolution ────────────────────────────────────────


def _api_key() -> str:
    """The Kenari key, however the Provider page saved it.

    Mirrors ``kenari-web``: the canonical ``KENARI_API_KEY``, then the desktop
    card's ``model.key_env`` pointer (only when the main model targets kenari),
    then the provider's own credential resolver (credential pool). Without the
    fallbacks a card-saved key makes image gen silently unavailable while chat
    works.
    """
    from agent.web_search_provider import get_provider_env

    val = get_provider_env(KEY_ENV)
    if val:
        return val

    try:
        from hermes_cli.config import get_env_value_prefer_dotenv, load_config

        model_cfg = (load_config() or {}).get("model")
        if isinstance(model_cfg, dict) and \
                str(model_cfg.get("provider") or "").strip().lower() == PROVIDER:
            pointer = model_cfg.get("key_env") or model_cfg.get("api_key_env")
            if pointer:
                pointed = (get_env_value_prefer_dotenv(str(pointer)) or "").strip()
                if pointed:
                    logger.debug("kenari-image: key via model.key_env=%s", pointer)
                    return pointed
    except Exception as exc:  # noqa: BLE001 — optional fallback
        logger.debug("kenari-image: model.key_env lookup unavailable: %s", exc)

    try:
        from hermes_cli.auth import resolve_api_key_provider_credentials

        creds = resolve_api_key_provider_credentials(PROVIDER) or {}
        if isinstance(creds, dict):
            resolved = str(creds.get("api_key") or "").strip()
            if resolved:
                logger.debug("kenari-image: key via the provider credential resolver")
                return resolved
    except Exception as exc:  # noqa: BLE001
        logger.debug("kenari-image: provider credential resolution unavailable: %s", exc)
    return ""


def _base_url() -> str:
    from agent.web_search_provider import get_provider_env

    return (get_provider_env(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_api_key()}", "User-Agent": USER_AGENT}


def _image_config() -> Dict[str, Any]:
    """``image_gen`` (or ``image_gen.kenari``) from config.yaml; ``{}`` on failure."""
    try:
        from hermes_cli.config import load_config

        section = (load_config() or {}).get("image_gen")
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("kenari-image: config unavailable: %s", exc)
        return {}


def _image_dimensions(path: str) -> Optional[tuple[int, int]]:
    """``(width, height)`` read from the PNG/JPEG header, or ``None``.

    Kenari snaps sizes to its own buckets, so the requested size is not what
    the user got. Reporting the request would be a lie in the tool result.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
            if head[:2] == b"\xff\xd8":  # JPEG: walk the segment markers
                fh.seek(2)
                while True:
                    marker = fh.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    if marker[1] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        fh.read(3)
                        h = int.from_bytes(fh.read(2), "big")
                        w = int.from_bytes(fh.read(2), "big")
                        return w, h
                    length = int.from_bytes(fh.read(2), "big")
                    if length < 2:
                        return None
                    fh.seek(length - 2, os.SEEK_CUR)
    except Exception:  # noqa: BLE001 — metadata only, never fail the generation
        return None
    return None


def _resolve_model() -> str:
    """``KENARI_IMAGE_MODEL`` → ``image_gen.kenari.model`` → ``image_gen.model`` → default."""
    env = (os.environ.get("KENARI_IMAGE_MODEL") or "").strip()
    if env:
        return env
    cfg = _image_config()
    sub = cfg.get("kenari")
    if isinstance(sub, dict):
        chosen = str(sub.get("model") or "").strip()
        if chosen:
            return chosen
    return str(cfg.get("model") or "").strip() or DEFAULT_MODEL


def _size_for(aspect: str) -> str:
    return SIZES.get(aspect, SIZES["square"])


# ── failures ───────────────────────────────────────────────────────────────


def _fail(message: str, error_type: str, **kw: Any) -> Dict[str, Any]:
    return error_response(error=message, error_type=error_type, provider=PROVIDER, **kw)


def _http_error(status: int, detail: str) -> tuple[str, str]:
    """``(message, error_type)`` for a failed Kenari call.

    ``402`` is the exhausted path: image calls are billed to balance, and a
    plan's ``continue with balance`` switch governs whether they are served.
    """
    if status == 401:
        return f"{KEY_ENV} is invalid (Kenari returned 401)", "auth_error"
    if status == 402:
        return (
            "Kenari refused the image request: insufficient balance or the plan's "
            "spend cap is exhausted. Top up, or enable \"continue with balance\"."
        ), "quota_error"
    if status == 403:
        return "This key is not allowed to call that image model (403).", "auth_error"
    if status == 429:
        return "Kenari rate-limited the image request (HTTP 429).", "rate_limited"
    if status >= 500:
        return f"Kenari image service error (HTTP {status}).", "provider_error"
    return f"Kenari returned HTTP {status}: {detail}".strip(), "provider_error"


# ── source images ──────────────────────────────────────────────────────────


def _load_ref_bytes(ref: str) -> tuple[bytes, str, str]:
    """``(data, filename, content_type)`` for a URL, data URI or local path.

    ``data:`` URIs are decoded locally — Kenari's edit endpoint RETURNS image
    data in that form, and a caller may hand one back in. Downloading one would
    fail (httpx rejects a >1 MB data URI as "URL too long").
    """
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("empty image reference")

    lower = ref.lower()
    if lower.startswith("data:"):
        header, _, payload = ref.partition(",")
        ext = "png"
        if "image/" in header:
            ext = header.split("image/", 1)[1].split(";", 1)[0].strip() or "png"
        try:
            data = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"malformed data URI: {exc}") from exc
        return data, f"image.{ext}", f"image/{ext}"

    if lower.startswith(("http://", "https://")):
        from tools.url_safety import create_ssrf_safe_client, is_safe_url

        if not is_safe_url(ref):
            raise ValueError(f"image URL failed the SSRF safety check: {ref}")
        with create_ssrf_safe_client(timeout=60, follow_redirects=True) as client:
            resp = client.get(ref)
        resp.raise_for_status()
        name = ref.split("?", 1)[0].rsplit("/", 1)[-1] or "image.png"
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip() \
            or mimetypes.guess_type(name)[0] or "image/png"
        return resp.content, name, ctype

    from agent.file_safety import raise_if_read_blocked

    raise_if_read_blocked(ref)
    with open(ref, "rb") as fh:
        data = fh.read()
    name = os.path.basename(ref) or "image.png"
    return data, name, mimetypes.guess_type(name)[0] or "image/png"


def _decode_payload(data: Any, *, prefix: str) -> Optional[str]:
    """Cache whatever the API returned; ``None`` when nothing usable came back."""
    if not isinstance(data, dict):
        return None
    b64 = data.get("b64_json")
    # The edit endpoint returns "" here, so emptiness must count as absent.
    if isinstance(b64, str) and b64.strip():
        try:
            return str(save_b64_image(b64.strip(), prefix=prefix))
        except Exception as exc:  # noqa: BLE001
            logger.warning("kenari-image: could not save b64 image: %s", exc)
            return None

    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if url.lower().startswith("data:"):
        try:
            raw = base64.b64decode(url.partition(",")[2], validate=False)
            return str(save_b64_image(base64.b64encode(raw).decode(), prefix=prefix))
        except Exception as exc:  # noqa: BLE001
            logger.warning("kenari-image: could not decode data-URI image: %s", exc)
            return None
    try:
        return str(save_url_image(url, prefix=prefix))
    except Exception as exc:  # noqa: BLE001
        logger.debug("kenari-image: could not cache image URL (%s); keeping the URL", exc)
        return url


# ── the provider ───────────────────────────────────────────────────────────


class KenariImageGenProvider(ImageGenProvider):
    """Kenari ``/v1/images/*`` backend."""

    name = PROVIDER
    display_name = "Kenari"

    def is_available(self) -> bool:
        # Same resolver the request path uses, so availability and auth agree.
        return bool(_api_key())

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text", "image"], "max_reference_images": MAX_REFERENCE_IMAGES}

    def list_models(self) -> List[Dict[str, Any]]:
        rows = self._dynamic_catalog() or CURATED_MODELS
        out = []
        for model_id, meta in rows.items():
            meta = meta or {}
            out.append({
                "id": model_id,
                "display": meta.get("display") or model_id,
                "speed": meta.get("speed", ""),
                "strengths": meta.get("strengths", ""),
                "price": "free" if model_id.endswith(":free") else meta.get("price", "paid"),
            })
        return out

    def _dynamic_catalog(self) -> Dict[str, Dict[str, Any]]:
        """Image-output models from ``/v1/models``; ``{}`` when unavailable.

        Cached for 10 minutes: the picker may call this on every paint, and a
        failure must not make image generation look absent.
        """
        import time

        now = time.monotonic()
        cached = _CATALOG_CACHE.get("models")
        if isinstance(cached, dict) and now - float(_CATALOG_CACHE.get("at") or 0.0) < 600:
            return cached
        if not _api_key():
            return {}
        try:
            resp = httpx.get(f"{_base_url()}/models", headers=_headers(), timeout=20.0)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug("kenari-image: catalog fetch failed: %s", exc)
            return {}
        entries = payload.get("data") if isinstance(payload, dict) else payload
        found: Dict[str, Dict[str, Any]] = {}
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("id") or "").strip()
            if not model_id:
                continue
            mods = entry.get("modalities")
            outputs = mods.get("output") if isinstance(mods, dict) else None
            if not (isinstance(outputs, list) and any("image" in str(o) for o in outputs)):
                continue
            found[model_id] = {
                "display": model_id,
                "strengths": "Text-to-image and editing",
            }
        if found:
            _CATALOG_CACHE.update({"at": now, "models": found})
        return found

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        model = _resolve_model()
        if not prompt:
            return _fail("An image prompt is required.", "bad_request", model=model, aspect_ratio=aspect)
        if not _api_key():
            return _fail(
                f"{KEY_ENV} is not set. Add it under Settings → Providers → Keys → Kenari.",
                "auth_required", model=model, aspect_ratio=aspect,
            )

        sources: List[str] = []
        if isinstance(image_url, str) and image_url.strip():
            sources.append(image_url.strip())
        sources.extend(normalize_reference_images(reference_image_urls) or [])
        sources = sources[:MAX_REFERENCE_IMAGES]
        size = _size_for(aspect)
        is_edit = bool(sources)

        try:
            if is_edit:
                files = []
                for ref in sources:
                    data, fname, ctype = _load_ref_bytes(ref)
                    files.append(("image", (fname, data, ctype)))
                resp = httpx.post(
                    f"{_base_url()}/images/edits",
                    data={"model": model, "prompt": prompt, "size": size, "n": "1"},
                    files=files, headers=_headers(), timeout=TIMEOUT,
                )
            else:
                resp = httpx.post(
                    f"{_base_url()}/images/generations",
                    json={"model": model, "prompt": prompt, "size": size, "n": 1,
                          "response_format": "b64_json"},
                    headers={**_headers(), "Content-Type": "application/json"},
                    timeout=TIMEOUT,
                )
        except httpx.RequestError as exc:
            logger.warning("kenari-image: request failed: %s", exc)
            return _fail(f"Could not reach Kenari: {exc}", "network_error",
                         model=model, aspect_ratio=aspect)
        except Exception as exc:  # noqa: BLE001 — e.g. an unreadable source image
            return _fail(f"Could not prepare the request: {exc}", "io_error",
                         model=model, aspect_ratio=aspect)

        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                err = (body or {}).get("error")
                if isinstance(err, dict):
                    detail = str(err.get("message") or err.get("code") or "")
                elif err:
                    detail = str(err)
            except Exception:  # noqa: BLE001
                detail = (resp.text or "")[:200]
            message, error_type = _http_error(resp.status_code, detail)
            if detail and detail not in message:
                message = f"{message} — {detail}"
            logger.warning("kenari-image: HTTP %s: %s", resp.status_code, detail or "(no detail)")
            return _fail(message, error_type, model=model, aspect_ratio=aspect)

        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return _fail(f"Could not parse Kenari response: {exc}", "provider_error",
                         model=model, aspect_ratio=aspect)

        rows = payload.get("data") if isinstance(payload, dict) else None
        first = rows[0] if isinstance(rows, list) and rows else None
        image = _decode_payload(first, prefix=f"kenari_{model.replace(':', '_')}")
        if not image:
            return _fail("Kenari returned no image data.", "empty_response",
                         model=model, aspect_ratio=aspect)

        extra: Dict[str, Any] = {"size": size, "requested_size": size}
        dims = _image_dimensions(image) if os.path.isfile(image) else None
        if dims:
            extra["size"] = f"{dims[0]}x{dims[1]}"
            extra["width"], extra["height"] = dims
            if extra["size"] != size:
                logger.debug("kenari-image: requested %s, got %s", size, extra["size"])
        revised = first.get("revised_prompt") if isinstance(first, dict) else None
        if revised:
            extra["revised_prompt"] = revised
        return success_response(
            image=image, model=model, prompt=prompt, aspect_ratio=aspect,
            provider=PROVIDER, modality="image" if is_edit else "text", extra=extra,
        )


def register(ctx) -> None:
    """Plugin entry point."""
    ctx.register_image_gen_provider(KenariImageGenProvider())
