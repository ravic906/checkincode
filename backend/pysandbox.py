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

import json
import os

from e2b_code_interpreter import Sandbox

E2B_API_KEY = os.environ.get("E2B_API_KEY", "")
RUN_TIMEOUT_SECONDS = 8

_EXAMPLES_START = "___PHOENIX_EXAMPLES_START___"
_EXAMPLES_END = "___PHOENIX_EXAMPLES_END___"


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


def extract_examples(*, canonical_solution: str, test_code: str, function_signature: str, max_examples: int = 2) -> list[dict]:
    """
    Runs `canonical_solution` + `test_code` in a real E2B sandbox (the same
    engine that grades actual submissions) with the target function
    wrapped in a tracer that records the real arguments and real return
    value of its first `max_examples` calls made by `test_code`'s own
    assertions.

    This deliberately never asks the model to separately describe an
    "example" -- doing so risks a hand-written example silently drifting
    from what the function actually does. Instead the example IS a real,
    already-passing test call, captured by instrumenting execution rather
    than statically parsing test_code (which breaks the moment a test
    uses an intermediate variable or a `.equals(...)`/`np.array_equal(...)`
    comparison instead of a bare `f(x) == y`).

    Returns [] (never raises) if extraction fails for any reason -- this
    is a nice-to-have display enhancement, not something that should be
    able to break grading or block a draft from being approved.
    """
    if not E2B_API_KEY:
        return []

    tracer_source = f"""
_phoenix_examples = []

{canonical_solution}

_phoenix_orig_fn = {function_signature}

def _phoenix_tracer(*args, **kwargs):
    _phoenix_result = _phoenix_orig_fn(*args, **kwargs)
    if len(_phoenix_examples) < {max_examples}:
        _phoenix_examples.append({{"args": [repr(a) for a in args], "result": repr(_phoenix_result)}})
    return _phoenix_result

{function_signature} = _phoenix_tracer

{test_code}

import json as _phoenix_json
print("{_EXAMPLES_START}")
print(_phoenix_json.dumps(_phoenix_examples))
print("{_EXAMPLES_END}")
"""
    try:
        with Sandbox.create(api_key=E2B_API_KEY, timeout=RUN_TIMEOUT_SECONDS) as sandbox:
            execution = sandbox.run_code(tracer_source, timeout=RUN_TIMEOUT_SECONDS)
    except Exception:
        return []

    if execution.error is not None:
        return []

    stdout = "".join(execution.logs.stdout)
    try:
        start = stdout.index(_EXAMPLES_START) + len(_EXAMPLES_START)
        end = stdout.index(_EXAMPLES_END)
        return json.loads(stdout[start:end].strip())
    except (ValueError, json.JSONDecodeError):
        return []
