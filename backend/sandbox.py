"""
Sandboxed execution of student SQL against a problem's dataset.

Design:
- Every submission gets a brand-new in-memory DuckDB connection (cheap --
  DuckDB in-memory connections are lightweight) so students can never see
  or affect each other's state, and can't leave anything behind.
- Only a single read-only SELECT/WITH statement is allowed. We reject
  anything else (DDL, DML, multiple statements, PRAGMA, ATTACH, COPY, etc.)
  with a keyword blocklist plus a "exactly one statement" check.
- Execution runs in a worker thread with a hard wall-clock timeout; if it
  doesn't finish in time we report a timeout instead of hanging the request.
- Result rows are capped so a runaway cross join can't blow up memory.
"""

import re
import concurrent.futures
import duckdb

STATEMENT_TIMEOUT_SECONDS = 5
MAX_RESULT_ROWS = 1000

# Keywords that would mutate state, touch the filesystem, or otherwise step
# outside "run a read-only query against the seeded tables."
FORBIDDEN_KEYWORDS = [
    "insert", "update", "delete", "drop", "alter", "create", "attach",
    "detach", "copy", "export", "import", "pragma", "call", "install",
    "load", "set ", "vacuum", "checkpoint", "grant", "revoke",
]


class SqlValidationError(Exception):
    pass


class SqlTimeoutError(Exception):
    pass


def validate_student_sql(query: str) -> str:
    """Raises SqlValidationError if the query isn't a single safe SELECT."""
    if not query or not query.strip():
        raise SqlValidationError("Query is empty.")

    stripped = query.strip()

    # Allow one optional trailing semicolon, but reject multiple statements.
    body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in body:
        raise SqlValidationError(
            "Only a single SQL statement is allowed (no semicolons except "
            "one at the very end)."
        )

    lowered = body.lower()
    if not (lowered.lstrip().startswith("select") or lowered.lstrip().startswith("with")):
        raise SqlValidationError(
            "Only SELECT (or WITH ... SELECT) statements are allowed."
        )

    for kw in FORBIDDEN_KEYWORDS:
        if re.search(r"\b" + re.escape(kw.strip()) + r"\b", lowered):
            raise SqlValidationError(
                f"Query contains a disallowed keyword: '{kw.strip()}'. "
                "Only read-only SELECT queries are permitted."
            )

    return body


def _execute(problem: dict, query: str):
    con = duckdb.connect(":memory:")
    try:
        con.execute(problem["schema_sql"])
        con.execute(problem["seed_sql"])
        cur = con.execute(query)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_RESULT_ROWS + 1)
        truncated = len(rows) > MAX_RESULT_ROWS
        rows = rows[:MAX_RESULT_ROWS]
        return columns, [list(r) for r in rows], truncated
    finally:
        con.close()


def run_query_against_problem(problem: dict, query: str, timeout=STATEMENT_TIMEOUT_SECONDS):
    """
    Validates and runs `query` against `problem`'s sandboxed dataset.
    Returns (columns, rows, truncated). Raises SqlValidationError,
    SqlTimeoutError, or duckdb.Error (caller should catch and surface as a
    plain-language "your SQL has an error" message).
    """
    safe_query = validate_student_sql(query)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_execute, problem, safe_query)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise SqlTimeoutError(
                f"Query did not finish within {timeout} seconds. "
                "Check for an unintended cross join or infinite recursion."
            )


def compute_expected_output(problem: dict):
    """Runs the canonical query once (used at problem-load time / tests)."""
    return _execute(problem, problem["canonical_sql"])


def _normalize_cell(v):
    # DuckDB may return Decimal/date/etc; stringify for stable comparison
    # and for JSON serialization in the API response.
    return str(v) if v is not None else None


def compare_results(expected_columns, expected_rows, actual_columns, actual_rows, order_matters: bool):
    """
    Returns (is_correct: bool, diff_summary: str | None).
    Compares values only (not column names), since students may alias
    columns differently -- what matters is the data being right.
    """
    norm_expected = [[_normalize_cell(v) for v in row] for row in expected_rows]
    norm_actual = [[_normalize_cell(v) for v in row] for row in actual_rows]

    if len(expected_columns) != len(actual_columns):
        return False, (
            f"Expected {len(expected_columns)} column(s), your query "
            f"returned {len(actual_columns)}."
        )

    if not order_matters:
        norm_expected = sorted(norm_expected, key=lambda r: [str(x) for x in r])
        norm_actual = sorted(norm_actual, key=lambda r: [str(x) for x in r])

    if norm_expected == norm_actual:
        return True, None

    if len(norm_expected) != len(norm_actual):
        return False, (
            f"Expected {len(norm_expected)} row(s), your query returned "
            f"{len(norm_actual)} row(s)."
        )

    return False, "Row values differ from the expected output."
