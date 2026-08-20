"""
Single source of truth for the SQL topic taxonomy, shared by the practice
problem bank (problems.py, graded) and the mock interview rotation
(interview.py, conversational).

Based on the SQL Cookbook (Molinaro) chapter structure -- a well-tested
progression from basics to advanced query technique -- plus three topics
that book doesn't cover (they're about database design/ops, not query
recipes) but that come up constantly in real interviews.

"Inserting, Updating, and Deleting" is deliberately excluded from
GRADEABLE_TOPICS: the practice sandbox is read-only by design (see
sandbox.py) and that invariant isn't changing, so no graded practice
problem is ever written against this topic.

EXTRA_TOPICS (Normalization, Transactions/ACID, Indexing/Performance) are
also excluded from GRADEABLE_TOPICS -- they're design/ops questions, not
"run a query and diff the output" recipes (a transaction problem
inherently needs multiple statements, which conflicts with the
single-SELECT grading model). Both DML and EXTRA_TOPICS are still fine as
interview topics, since the interview never executes anything -- it's
just conversation.
"""

DML_TOPIC = "Inserting, Updating, and Deleting"

COOKBOOK_TOPICS = [
    "Retrieving Records",
    "Sorting Query Results",
    "Working with Multiple Tables",
    DML_TOPIC,
    "Metadata Queries",
    "Working with Strings",
    "Working with Numbers",
    "Date Arithmetic",
    "Date Manipulation",
    "Working with Ranges",
    "Advanced Searching",
    "Reporting and Warehousing",
    "Hierarchical Queries",
    "Odds and Ends",
]

# Topics that aren't Cookbook query-recipe chapters but come up constantly
# in real interviews -- kept alongside rather than dropped.
EXTRA_TOPICS = [
    "Normalization and Schema Design",
    "Transactions and ACID Properties",
    "Indexing and Query Performance",
]

ALL_TOPICS = COOKBOOK_TOPICS + EXTRA_TOPICS

# Topics a graded practice problem can be written against -- the Cookbook
# query-recipe topics minus DML (grading requires read-only execution).
# EXTRA_TOPICS are design/ops questions that don't fit single-SELECT
# grading, so they stay interview-only (see module docstring).
GRADEABLE_TOPICS = [t for t in COOKBOOK_TOPICS if t != DML_TOPIC]
