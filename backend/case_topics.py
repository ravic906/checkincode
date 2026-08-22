"""
Topic taxonomy for the Business Case practice track -- open-ended
analytical-reasoning questions with no single verifiable right answer,
graded by an AI rubric judge (llm.case_feedback) rather than execution.

Two lenses, both riding the same track='case' rows/grading mechanism --
which lens a problem belongs to is decided by topic membership, exactly
the same pattern stats_topics.py/data_lib_topics.py already use within
track='python' (a topic vocabulary, not a separate track value).

CASE_DA_TOPICS: Data Analyst case questions -- the round that decides
most DA offers at companies like Uber, Meta, Google ("why did WAU drop
15% last month", "design a metric for feature X").

CASE_DE_TOPICS: Data Engineer case questions -- pipeline/schema/scaling
trade-off discussions, the DE-flavored equivalent.
"""

CASE_DA_TOPICS = [
    "Metric Design & Definition",
    "Root-Cause & Diagnostic Analysis",
    "A/B Testing & Experimentation",
    "Growth, Retention & Funnels",
    "Marketplace & Two-Sided Dynamics",
    "Product Sense & Trade-offs",
    "Forecasting & Estimation",
    "Data Quality & Instrumentation",
    "Stakeholder Communication & Prioritization",
]

CASE_DE_TOPICS = [
    "Data Pipeline Design & Architecture",
    "Data Modeling & Schema Design Trade-offs",
    "Batch vs. Streaming Design Decisions",
    "Data Quality & Validation Strategy",
    "Scaling & Performance Trade-offs",
    "Data Governance & Access Control",
]

CASE_TOPICS = CASE_DA_TOPICS + CASE_DE_TOPICS
