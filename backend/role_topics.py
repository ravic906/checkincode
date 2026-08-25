"""
Role -> blended topic-mix taxonomy for the role-based mock interview.

Blends SQL query-technique topics (topics.py) with conceptual/business-case
topics (case_topics.py) so a single spoken interview can range across both,
with the exact mix depending on which of the 5 target roles was picked.
This is new content, not a reorg of topics.py/case_topics.py -- same
"new taxonomy file beside the existing one" precedent case_topics.py itself
already set relative to topics.py.

Business Analyst and Product Analyst reuse CASE_DA_TOPICS as a placeholder
lens (no purpose-built topic list exists for them yet) -- flagged as a
known gap to revisit, not a design claim that they're identical roles.
"""

import topics
import case_topics

ROLES = ["Data Analyst", "BI Analyst", "Business Analyst", "Product Analyst", "Data Engineer"]

_SQL_NO_DML = [t for t in topics.ALL_TOPICS if t != topics.DML_TOPIC]

# Every conceptual topic used here must resolve to a topic_type of
# "conceptual" (see CONCEPTUAL_TOPICS below) -- SQL topics are pulled
# straight from topics.ALL_TOPICS.
ROLE_TOPIC_MIX = {
    "Data Analyst": {
        "sql": _SQL_NO_DML,
        "conceptual": [
            "Metric Design & Definition",
            "Root-Cause & Diagnostic Analysis",
            "A/B Testing & Experimentation",
            "Growth, Retention & Funnels",
            "Data Quality & Instrumentation",
            "Stakeholder Communication & Prioritization",
        ],
    },
    "BI Analyst": {
        "sql": _SQL_NO_DML,
        "conceptual": [
            "Metric Design & Definition",
            "Stakeholder Communication & Prioritization",
            "Data Quality & Instrumentation",
            "Data Modeling & Schema Design Trade-offs",
        ],
    },
    "Business Analyst": {
        "sql": [
            "Retrieving Records",
            "Sorting Query Results",
            "Working with Multiple Tables",
            "Working with Strings",
            "Working with Numbers",
            "Advanced Searching",
            "Reporting and Warehousing",
        ],
        "conceptual": [
            "Root-Cause & Diagnostic Analysis",
            "Stakeholder Communication & Prioritization",
            "Product Sense & Trade-offs",
            "Forecasting & Estimation",
            "Marketplace & Two-Sided Dynamics",
        ],
    },
    "Product Analyst": {
        "sql": _SQL_NO_DML,
        "conceptual": [
            "A/B Testing & Experimentation",
            "Growth, Retention & Funnels",
            "Product Sense & Trade-offs",
            "Metric Design & Definition",
            "Forecasting & Estimation",
        ],
    },
    "Data Engineer": {
        "sql": topics.ALL_TOPICS,  # DML fair game here -- the DE role touches writes constantly
        "conceptual": case_topics.CASE_DE_TOPICS,
    },
}

# Which conceptual-topic pool a topic belongs to, for the table_context /
# response-schema branch in llm._interview_system_prompt.
CONCEPTUAL_TOPICS = set(case_topics.CASE_TOPICS)


def topics_for_role(role: str) -> list[str]:
    """Flat ordered list (sql topics first, then conceptual) -- the
    role-based replacement for interview.GENERIC_TOPICS."""
    mix = ROLE_TOPIC_MIX[role]
    return mix["sql"] + mix["conceptual"]


def is_conceptual(topic: str) -> bool:
    return topic in CONCEPTUAL_TOPICS
