"""BYO voice providers for Pollen (HP-62).

The UI's default path is the browser (Web Speech). When the operator sets
`HIVEPILOT_VOICE_TTS_API_KEY` and a cloud `voice_tts_provider`,
`POST /v1/voice/tts` proxies the synthesis so the key never reaches the
browser. STT stays in the browser unless a cloud STT key is set — this
module only advertises that fact; cloud STT upload is a later slice.
"""

from __future__ import annotations

from typing import Any

import requests

from hivepilot.config import settings

_TTS_PROVIDERS = frozenset({"browser", "openai", "elevenlabs", "cartesia"})
_STT_PROVIDERS = frozenset({"browser", "openai"})


def _norm(value: str | None, allowed: frozenset[str], default: str) -> str:
    raw = (value or default).strip().lower()
    return raw if raw in allowed else default


def public_config() -> dict[str, Any]:
    tts = _norm(settings.voice_tts_provider, _TTS_PROVIDERS, "browser")
    stt = _norm(settings.voice_stt_provider, _STT_PROVIDERS, "browser")
    return {
        "stt": stt,
        "tts": tts,
        "cloud_tts": tts != "browser" and bool(settings.voice_tts_api_key),
        "cloud_stt": stt != "browser" and bool(settings.voice_stt_api_key),
    }


def synthesize(text: str) -> bytes:
    """Return audio bytes for `text`. Raises ValueError if cloud TTS is off."""
    cfg = public_config()
    if not cfg["cloud_tts"]:
        raise ValueError("cloud TTS is not configured")
    body = (text or "").strip()
    if not body:
        raise ValueError("text is required")
    provider = cfg["tts"]
    key = settings.voice_tts_api_key or ""
    if provider == "openai":
        return _openai_tts(body, key)
    if provider == "elevenlabs":
        return _elevenlabs_tts(body, key)
    if provider == "cartesia":
        return _cartesia_tts(body, key)
    raise ValueError(f"unsupported tts provider: {provider}")


def _openai_tts(text: str, key: str) -> bytes:
    resp = requests.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "gpt-4o-mini-tts", "voice": "alloy", "input": text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def _elevenlabs_tts(text: str, key: str) -> bytes:
    resp = requests.post(
        "https://api.elevenlabs.io/v1/text-to-speech/JBFqnCBsd6RMkjVDRZzb",
        headers={"xi-api-key": key, "Accept": "audio/mpeg"},
        json={"text": text, "model_id": "eleven_multilingual_v2"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def _cartesia_tts(text: str, key: str) -> bytes:
    resp = requests.post(
        "https://api.cartesia.ai/tts/bytes",
        headers={"X-API-Key": key, "Cartesia-Version": "2024-06-10"},
        json={
            "transcript": text,
            "model_id": "sonic-english",
            "voice": {"mode": "id", "id": "a0e99841-438c-4a64-b679-ae309f2178ea"},
            "output_format": {"container": "mp3", "encoding": "mp3", "sample_rate": 44100},
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content
