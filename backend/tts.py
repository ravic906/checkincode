"""
Text-to-speech for the mock interview, via an OpenAI-compatible audio speech
endpoint (OpenAI's tts-1 today).

Kept provider-agnostic on purpose, same pattern as stt.py -- swap providers
later purely by changing TTS_API_BASE/TTS_API_KEY/TTS_MODEL, no code change.
This module never mentions "OpenAI" in its public interface for that reason.

Replaces the frontend's previous use of the browser's Web Speech API
(window.speechSynthesis), which has no voice selection and reads flat/
robotic on most systems -- a real interviewer needs to sound like one.
"""

import os

import requests

TTS_API_BASE = os.environ.get("TTS_API_BASE", "https://api.openai.com/v1")
TTS_API_KEY = os.environ.get("TTS_API_KEY", "")
TTS_MODEL = os.environ.get("TTS_MODEL", "tts-1")
TTS_VOICE = os.environ.get("TTS_VOICE", "shimmer")


def synthesize(text: str) -> bytes:
    """
    Synthesizes `text` to speech, returning raw MP3 bytes. Raises
    RuntimeError if TTS_API_KEY isn't configured or the HTTP call fails --
    callers should catch this and fall back to browser TTS rather than
    going silent, since voice quality is a nice-to-have, not something
    that should be able to break the interview.
    """
    if not TTS_API_KEY:
        raise RuntimeError(
            "TTS_API_KEY is not set. Configure TTS_API_BASE / TTS_API_KEY / "
            "TTS_MODEL / TTS_VOICE env vars to enable natural interviewer speech."
        )

    resp = requests.post(
        f"{TTS_API_BASE}/audio/speech",
        headers={"Authorization": f"Bearer {TTS_API_KEY}"},
        json={"model": TTS_MODEL, "voice": TTS_VOICE, "input": text, "response_format": "mp3"},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} error from TTS provider: {resp.text[:500]}")

    return resp.content
