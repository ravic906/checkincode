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
import time
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


_CREATE_TABLE_RE = re.compile(
    r'create\s+table\s+(?:if\s+not\s+exists\s+)?(?:"[^"]+"\.)?"?([a-zA-Z_][a-zA-Z0-9_]*)"?',
    re.IGNORECASE,
)
_FROM_JOIN_RE = re.compile(r'\b(?:from|join)\s+"?([a-zA-Z_][a-zA-Z0-9_]*)"?', re.IGNORECASE)
_CTE_NAME_RE = re.compile(
    r'(?:\bwith\s+(?:recursive\s+)?|,\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(',
    re.IGNORECASE,
)


def _extract_schema_table_names(schema_sql: str) -> set[str]:
    """Returns the lowercased set of real table names declared by this
    problem's schema_sql, via regex over CREATE TABLE statements -- not by
    executing schema_sql in DuckDB, since this runs on every single student
    submission and a second DuckDB connect+execute per submission is pure
    waste when schema_sql is just platform-authored `CREATE TABLE name
    (...)` statements, never user input."""
    return {m.group(1).lower() for m in _CREATE_TABLE_RE.finditer(schema_sql)}


def validate_query_references_real_table(query: str, table_names: set[str]) -> None:
    """Raises SqlValidationError if `query` never touches any of the
    problem's real tables -- e.g. `SELECT 'APAC', 'Priya Raman', 48210`
    with no FROM clause at all, which would otherwise pass
    validate_student_sql cleanly and, if it happens to match the cached
    expected output, get graded correct without ever reading real data.

    A query that only references its own CTE aliases (`WITH fake AS
    (SELECT 'x') SELECT * FROM fake`) must still fail this check -- a CTE
    name is not a real table. A query that references a real table
    *inside* a CTE body (`WITH t AS (SELECT * FROM orders) SELECT * FROM
    t`) must pass, since `orders` genuinely appears in the query text.
    This scans the whole query text (not just the outer scope), so a real
    table reference buried inside a subquery or CTE body still counts.
    """
    if not table_names:
        return  # schema_sql didn't parse to any table name -- nothing to enforce
    referenced = {m.group(1).lower() for m in _FROM_JOIN_RE.finditer(query)}
    cte_names = {m.group(1).lower() for m in _CTE_NAME_RE.finditer(query)}
    referenced -= cte_names
    if not (referenced & table_names):
        raise SqlValidationError(
            "Query must reference at least one real table from the problem's "
            "schema (no bare-literal / hardcoded-value queries allowed)."
        )


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
    # The keyword blocklist below only stops SQL *statements* that mutate
    # state (INSERT/DROP/etc). It does nothing to stop DuckDB *table
    # functions* that read the local filesystem or network -- read_csv(),
    # glob(), read_parquet('http://...'), etc. are all still plain SELECTs.
    # disabling external access at the connection level is the real fix;
    # confirmed this blocks read_csv_auto('/etc/passwd') and glob() while
    # leaving normal queries against the seeded in-memory tables untouched.
    con = duckdb.connect(":memory:", config={"enable_external_access": False})
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


def compute_expected_output(problem: dict):
    """Runs the canonical query once (used at problem-load time / tests)."""
    return _execute(problem, problem["canonical_sql"])


def run_query_against_test_cases(
    problem: dict,
    query: str,
    test_case_seeds: list[str],
    expected_per_case: list[tuple],
    timeout=STATEMENT_TIMEOUT_SECONDS,
):
    """
    Validates `query` once, then runs it against each seed dataset in
    `test_case_seeds` (problem["seed_sql"] plus however many hidden
    problem_test_cases rows the caller chose to include -- Run passes a
    short slice, Submit passes all of them), comparing each run's output
    to that dataset's own pre-computed expected output in
    `expected_per_case` (same order, each entry `(columns, rows)` from
    _EXPECTED_CACHE).

    Returns a dict: {"correct": bool, "failed_index": int | None,
    "actual_preview": ..., "expected_preview": ..., "timing_ms": int}.
    Stops at the first failing dataset (fail-fast) rather than running
    every remaining one -- this platform reports one diff, not a wall of
    them, and there's no partial credit to compute by continuing.
    """
    safe_query = validate_student_sql(query)
    validate_query_references_real_table(safe_query, _extract_schema_table_names(problem["schema_sql"]))
    order_matters = problem.get("order_matters", False)

    started = time.monotonic()
    last_columns, last_rows, last_truncated = [], [], False
    for i, seed_sql in enumerate(test_case_seeds):
        case_problem = {"schema_sql": problem["schema_sql"], "seed_sql": seed_sql}
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_execute, case_problem, safe_query)
            try:
                columns, rows, truncated = future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                raise SqlTimeoutError(
                    f"Query did not finish within {timeout} seconds. "
                    "Check for an unintended cross join or infinite recursion."
                )
        last_columns, last_rows, last_truncated = columns, rows, truncated
        expected_columns, expected_rows = expected_per_case[i]
        is_correct, diff = compare_results(expected_columns, expected_rows, columns, rows, order_matters)
        if not is_correct:
            return {
                "correct": False,
                "failed_index": i,
                "total_cases": len(test_case_seeds),
                "diff": diff,
                "columns": columns,
                "rows": rows,
                "expected_columns": expected_columns,
                "expected_rows": expected_rows,
                "timing_ms": int((time.monotonic() - started) * 1000),
            }

    return {
        "correct": True,
        "failed_index": None,
        "total_cases": len(test_case_seeds),
        "diff": None,
        "columns": last_columns,
        "rows": last_rows,
        "expected_columns": expected_per_case[-1][0] if expected_per_case else [],
        "expected_rows": expected_per_case[-1][1] if expected_per_case else [],
        "timing_ms": int((time.monotonic() - started) * 1000),
    }


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
