"""
Grading engine for the Python practice track, via a hosted code-execution
sandbox (E2B today) rather than in-process execution.

This is a deliberately different security model from sandbox.py's SQL
grading: SQL's safety rests entirely on DuckDB being a purpose-built query
engine with external access disabled, so a validated read-only SELECT
literally cannot do anything except read seeded tables. Python has no
equivalent -- it's Turing-complete and self-reflective, so a keyword
blocklist checked in-process (the SQL approach) is trivially bypassable
(__builtins__, ctypes, string-built eval, metaclass tricks, etc.), and
running submitted Python in the same process as the web server would
expose env vars, DB credentials, and everything else in memory.

Kept provider-agnostic at the public-interface level, same intent as
llm.py/stt.py, even though E2B's SDK-based integration (unlike Judge0's
plain REST contract) means a future provider swap would need this
module's internals rewritten, not just env vars -- run_python_submission()
is the seam every caller depends on, and that signature doesn't change.
"""

import os

from e2b_code_interpreter import Sandbox

E2B_API_KEY = os.environ.get("E2B_API_KEY", "")
RUN_TIMEOUT_SECONDS = 8


def run_python_submission(*, student_code: str, test_code: str) -> dict:
    """
    Runs `student_code` followed by `test_code` (plain assert statements)
    in a fresh E2B sandbox. Returns {"passed": bool, "output": str,
    "error": str | None} -- `error` is a friendly message on assertion
    failure or an exception, never a raw stack dump. Raises RuntimeError
    if the sandbox provider itself is unreachable/misconfigured -- callers
    should catch this and surface a clean "grading temporarily
    unavailable" message rather than 500ing the submission.
    """
    if not E2B_API_KEY:
        raise RuntimeError(
            "E2B_API_KEY is not set. Configure it to enable Python grading."
        )

    source = f"{student_code}\n\n{test_code}\n"

    try:
        with Sandbox.create(api_key=E2B_API_KEY, timeout=RUN_TIMEOUT_SECONDS) as sandbox:
            execution = sandbox.run_code(source, timeout=RUN_TIMEOUT_SECONDS)
    except Exception as e:
        raise RuntimeError(f"E2B sandbox error: {e}")

    stdout = "".join(execution.logs.stdout)
    stderr = "".join(execution.logs.stderr)

    if execution.error is None:
        return {"passed": True, "output": stdout, "error": None}

    # AssertionError from a failed test_code assertion, or any other
    # exception the student's code raised -- surface the clearest message
    # we have, never the raw traceback object.
    err = execution.error
    error_text = f"{err.name}: {err.value}" if err.value else err.name
    if not error_text.strip():
        error_text = stderr.strip() or "Execution failed."
    return {"passed": False, "output": stdout, "error": error_text}
