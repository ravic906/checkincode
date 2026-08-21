"""
Single source of truth for the Python topic taxonomy, parallel to
topics.py's SQL taxonomy but a separate module -- the two are conceptually
unrelated domains (different practice track entirely), so keeping them
apart avoids overloading topics.py's SQL-Cookbook-specific docstring/
rationale with a second, unrelated topic list.

Based on the Python Cookbook (Beazley/Jones) chapter structure, the same
"well-tested progression, borrow the book's own structure" approach
topics.py already uses for SQL.

PY_EXTRA_TOPICS are excluded from PY_GRADEABLE_TOPICS because a hosted
code-execution sandbox (pysandbox.py) can't safely or meaningfully verify
them in a single graded submission: they need real network access, are
inherently timing/non-deterministic, need native code compilation, or need
real OS/process access a sandbox must not grant. They're still fine as
interview-only conversation topics, same as SQL's DML/EXTRA_TOPICS split.
"""

PY_GRADEABLE_TOPICS = [
    "Data Structures and Algorithms",
    "Strings and Text",
    "Numbers, Dates, and Times",
    "Iterators and Generators",
    "Files and I/O",  # in-sandbox virtual FS only, never real disk access
    "Data Encoding and Processing",
    "Functions",
    "Classes and Objects",
    "Metaprogramming",
    "Modules and Packages",
    "Testing, Debugging, and Exceptions",
]

# Real topics, but not safely/meaningfully gradeable by a single sandboxed
# execution -- see module docstring.
PY_EXTRA_TOPICS = [
    "Network and Web Programming",
    "Concurrency",
    "Utility Scripting and System Administration",
    "C Extensions",
]

PY_ALL_TOPICS = PY_GRADEABLE_TOPICS + PY_EXTRA_TOPICS
