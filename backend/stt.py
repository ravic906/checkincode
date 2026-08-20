"""
Speech-to-text for the mock interview, via an OpenAI-compatible audio
transcription endpoint (DeepInfra's hosted Whisper today: whisper-large-v3-turbo).

Kept provider-agnostic on purpose, same pattern as llm.py -- swap to
Groq Whisper, OpenAI, or anything else with the same /audio/transcriptions
contract later purely by changing STT_API_BASE/STT_API_KEY/STT_MODEL,
no code change. This module never mentions "DeepInfra" in its public
interface for that reason. (Previously pointed at Fireworks, which
deprecated its audio transcription API entirely in June 2026.)

Unlike the browser's old Web Speech API (client-side, live interim
results), this is record-the-full-answer-then-transcribe: the frontend
sends one complete audio clip per turn, we send it here, and get back
plain text. No streaming/websocket plumbing -- simpler to build and run,
at the cost of a short pause after the candidate finishes speaking
instead of live word-by-word captions.
"""

import os

import requests

STT_API_BASE = os.environ.get("STT_API_BASE", "https://api.deepinfra.com/v1/openai")
STT_API_KEY = os.environ.get("STT_API_KEY", "")
STT_MODEL = os.environ.get("STT_MODEL", "openai/whisper-large-v3-turbo")


def transcribe(audio_bytes: bytes, filename: str) -> str:
    """
    Transcribes one recorded answer to text. Raises RuntimeError if
    STT_API_KEY isn't configured or the HTTP call fails -- callers should
    catch this and surface a clean error rather than 500ing the interview.
    """
    if not STT_API_KEY:
        raise RuntimeError(
            "STT_API_KEY is not set. Configure STT_API_BASE / STT_API_KEY / "
            "STT_MODEL env vars to enable voice answers."
        )

    resp = requests.post(
        f"{STT_API_BASE}/audio/transcriptions",
        headers={"Authorization": f"Bearer {STT_API_KEY}"},
        files={"file": (filename, audio_bytes)},
        data={"model": STT_MODEL, "response_format": "json"},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} error from STT provider: {resp.text[:500]}")

    data = resp.json()
    return (data.get("text") or "").strip()
