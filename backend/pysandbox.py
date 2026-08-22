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


def test_code_discriminates(*, test_code: str, function_signature: str) -> bool:
    """
    Verifies test_code actually checks something, by running it against
    a deliberately WRONG stub (always returns None) instead of the real
    canonical_solution. Returns True if the stub correctly fails the
    tests (proving they discriminate right answers from wrong ones);
    False if the stub passes anyway (proving the tests are vacuous --
    e.g. `assert isinstance(result, dict)` or `assert x is None or
    isinstance(x, float)` pass no matter what the function returns).

    This replaced a regex-based attempt to recognize "real" assertion
    styles (==, .equals(, array_equal(, 'x' in output, etc.) -- that
    approach kept missing legitimate idioms (pd.testing.assert_frame_equal
    called bare, without the `assert` keyword, was invisible to it) and
    turned into an unwinnable game of enumerating every valid Python
    comparison style. Checking discriminative power directly sidesteps
    the whole question of *how* the test is written.

    Returns True (benefit of the doubt) on any sandbox-level error --
    this check exists to catch vacuous tests, not to be a second point
    of failure for legitimate drafts when the sandbox itself hiccups.
    """
    if not E2B_API_KEY:
        return True
    stub_source = f"def {function_signature}(*args, **kwargs):\n    return None\n\n{test_code}\n"
    try:
        with Sandbox.create(api_key=E2B_API_KEY, timeout=RUN_TIMEOUT_SECONDS) as sandbox:
            execution = sandbox.run_code(stub_source, timeout=RUN_TIMEOUT_SECONDS)
    except Exception:
        return True
    return execution.error is not None


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

    # OOP problems' function_signature is the class name itself (e.g.
    # "ProductCounter"), and test_code instantiates it then calls methods
    # on the instance. Tracing calls TO the class (the old, function-only
    # approach below) only ever captured the *constructor* -- a real but
    # useless example (an object's repr is its memory address). Instead,
    # for classes, patch every public method on the class in place so
    # instance method calls (pc.add_product("apple") -> None,
    # pc.get_counts() -> {"apple": 1}) are what gets captured -- that's
    # the actual observable behavior a student needs to see.
    tracer_source = f"""
_phoenix_examples = []

{canonical_solution}

_phoenix_target = {function_signature}

import inspect as _phoenix_inspect
import itertools as _phoenix_itertools

def _phoenix_safe_repr(v):
    # A plain repr() is actively misleading for two common return/arg
    # shapes: a domain object with no __repr__ override shows its memory
    # address ("<Employee object at 0x...>"), and a generator shows its
    # own object repr instead of the values it would actually yield.
    # Recurses into list/tuple/set so a list of such objects doesn't slip
    # through unhandled.
    try:
        if _phoenix_inspect.isgenerator(v):
            return "yields: " + repr([_phoenix_safe_repr(x) for x in _phoenix_itertools.islice(v, 5)])
        if isinstance(v, (list, tuple, set)):
            opener, closer = {{"list": ("[", "]"), "tuple": ("(", ")"), "set": ("{{", "}}")}}[type(v).__name__]
            return opener + ", ".join(_phoenix_safe_repr(x) for x in v) + closer
        if hasattr(v, "__dict__") and type(v).__repr__ is object.__repr__:
            return type(v).__name__ + repr(vars(v))
        return repr(v)
    except Exception:
        return repr(v)

def _phoenix_capture_result(result):
    # Peeking into a generator to describe it must not consume the actual
    # generator test_code still holds a reference to and will keep
    # iterating -- tee() forks it into two independent iterators so the
    # real one returned to the caller is untouched.
    if _phoenix_inspect.isgenerator(result):
        _peek, _real = _phoenix_itertools.tee(result)
        return "yields: " + repr([_phoenix_safe_repr(x) for x in _phoenix_itertools.islice(_peek, 5)]), _real
    return _phoenix_safe_repr(result), result

if _phoenix_inspect.isclass(_phoenix_target):
    def _phoenix_wrap_method(_orig_method):
        def _phoenix_wrapped(self, *args, **kwargs):
            _phoenix_result = _orig_method(self, *args, **kwargs)
            _phoenix_result_repr, _phoenix_result = _phoenix_capture_result(_phoenix_result)
            if len(_phoenix_examples) < {max_examples}:
                _phoenix_examples.append({{
                    "method": _orig_method.__name__,
                    "args": [_phoenix_safe_repr(a) for a in args],
                    "result": _phoenix_result_repr,
                }})
            return _phoenix_result
        return _phoenix_wrapped

    for _phoenix_name in list(vars(_phoenix_target)):
        if _phoenix_name.startswith("__"):
            continue
        _phoenix_attr = getattr(_phoenix_target, _phoenix_name)
        if callable(_phoenix_attr):
            setattr(_phoenix_target, _phoenix_name, _phoenix_wrap_method(_phoenix_attr))
else:
    def _phoenix_tracer(*args, **kwargs):
        _phoenix_result = _phoenix_target(*args, **kwargs)
        _phoenix_result_repr, _phoenix_result = _phoenix_capture_result(_phoenix_result)
        if len(_phoenix_examples) < {max_examples}:
            _phoenix_examples.append({{"args": [_phoenix_safe_repr(a) for a in args], "result": _phoenix_result_repr}})
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
