"""
Grading engine for the Python practice track, via a hosted code-execution
sandbox (Judge0 today) rather than in-process execution.

This is a deliberately different security model from sandbox.py's SQL
grading: SQL's safety rests entirely on DuckDB being a purpose-built query
engine with external access disabled, so a validated read-only SELECT
literally cannot do anything except read seeded tables. Python has no
equivalent -- it's Turing-complete and self-reflective, so a keyword
blocklist checked in-process (the SQL approach) is trivially bypassable
(__builtins__, ctypes, string-built eval, metaclass tricks, etc.), and
running submitted Python in the same process as the web server would
expose env vars, DB credentials, and everything else in memory.

Kept provider-agnostic on purpose, same pattern as llm.py/stt.py: swap to
a different hosted sandbox later (E2B, a self-hosted Judge0 CE instance,
etc.) purely by changing env vars, no code change, as long as the new
provider speaks Judge0's submission contract or this module is updated
to translate to a different one.
"""

import base64
import os
import time

import requests

JUDGE0_API_BASE = os.environ.get("JUDGE0_API_BASE", "https://judge0-ce.p.rapidapi.com")
JUDGE0_API_KEY = os.environ.get("JUDGE0_API_KEY", "")
JUDGE0_API_HOST = os.environ.get("JUDGE0_API_HOST", "judge0-ce.p.rapidapi.com")
# Python (3.x) language id on Judge0 CE -- override via env if a different
# hosted instance numbers languages differently.
JUDGE0_PYTHON_LANGUAGE_ID = int(os.environ.get("JUDGE0_PYTHON_LANGUAGE_ID", "71"))

RUN_TIMEOUT_SECONDS = 8
POLL_INTERVAL_SECONDS = 0.5
MAX_POLL_ATTEMPTS = 20  # ~10s of polling if the provider doesn't support wait=true


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode("ascii")


def _b64decode(s: str | None) -> str:
    if not s:
        return ""
    return base64.b64decode(s).decode("utf-8", errors="replace")


def run_python_submission(*, student_code: str, test_code: str) -> dict:
    """
    Runs `student_code` followed by `test_code` (plain assert statements)
    in the hosted sandbox. Returns {"passed": bool, "output": str, "error":
    str | None} -- `error` is a friendly message on assertion failure or
    an exception, never a raw stack dump. Raises RuntimeError if the
    sandbox provider itself is unreachable/misconfigured -- callers should
    catch this and surface a clean "grading temporarily unavailable"
    message rather than 500ing the submission.
    """
    if not JUDGE0_API_KEY:
        raise RuntimeError(
            "JUDGE0_API_KEY is not set. Configure JUDGE0_API_BASE / JUDGE0_API_KEY / "
            "JUDGE0_API_HOST env vars to enable Python grading."
        )

    source = f"{student_code}\n\n{test_code}\n"
    headers = {
        "Content-Type": "application/json",
        "X-RapidAPI-Key": JUDGE0_API_KEY,
        "X-RapidAPI-Host": JUDGE0_API_HOST,
    }
    payload = {
        "source_code": _b64(source),
        "language_id": JUDGE0_PYTHON_LANGUAGE_ID,
        "cpu_time_limit": RUN_TIMEOUT_SECONDS,
    }

    resp = requests.post(
        f"{JUDGE0_API_BASE}/submissions?base64_encoded=true&wait=true",
        headers=headers,
        json=payload,
        timeout=RUN_TIMEOUT_SECONDS + 5,
    )
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} error from Judge0: {resp.text[:500]}")
    data = resp.json()

    # Some Judge0 deployments ignore wait=true under load and just return a
    # token -- poll for the result in that case rather than assume synchronous.
    if "token" in data and "status" not in data:
        token = data["token"]
        for _ in range(MAX_POLL_ATTEMPTS):
            time.sleep(POLL_INTERVAL_SECONDS)
            poll = requests.get(
                f"{JUDGE0_API_BASE}/submissions/{token}?base64_encoded=true",
                headers=headers,
                timeout=10,
            )
            if not poll.ok:
                raise RuntimeError(f"{poll.status_code} error polling Judge0: {poll.text[:500]}")
            data = poll.json()
            if data.get("status", {}).get("id", 0) > 2:  # >2 means no longer queued/running
                break

    status = data.get("status", {})
    status_desc = status.get("description", "Unknown")
    stdout = _b64decode(data.get("stdout"))
    stderr = _b64decode(data.get("stderr"))
    compile_output = _b64decode(data.get("compile_output"))

    if status_desc == "Accepted":
        return {"passed": True, "output": stdout, "error": None}

    # Anything else (Runtime Error, Time Limit Exceeded, Compilation Error,
    # Wrong Answer -- unused here since we don't set expected_output, an
    # assertion failure surfaces as a Runtime Error/AssertionError instead)
    # is a failure -- surface the clearest message we have.
    error_text = stderr.strip() or compile_output.strip() or status_desc
    return {"passed": False, "output": stdout, "error": error_text}
