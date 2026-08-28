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
import power_automate_topics

ROLES = ["Data Analyst", "BI Analyst", "Business Analyst", "Product Analyst", "Data Engineer", "Power Automate Developer"]

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
    "Power Automate Developer": {
        # No SQL component -- Power Automate is a no-code/low-code tool,
        # not a database one. Every topic here is conceptual (open spoken
        # discussion), unlike every other role above.
        "sql": [],
        "conceptual": power_automate_topics.POWER_AUTOMATE_TOPICS,
    },
}

# Which conceptual-topic pool a topic belongs to, for the table_context /
# response-schema branch in llm._interview_system_prompt.
CONCEPTUAL_TOPICS = set(case_topics.CASE_TOPICS) | set(power_automate_topics.POWER_AUTOMATE_TOPICS)


def topics_for_role(role: str) -> list[str]:
    """Flat ordered list (sql topics first, then conceptual) -- the
    role-based replacement for interview.GENERIC_TOPICS."""
    mix = ROLE_TOPIC_MIX[role]
    return mix["sql"] + mix["conceptual"]


def is_conceptual(topic: str) -> bool:
    return topic in CONCEPTUAL_TOPICS


# Target application(SQL):theory(conceptual) split, measured by actual
# QUESTIONS asked (not just topics touched -- a topic that got 3 follow-
# ups counts 3x), matched to what real completed interviews already
# showed naturally happening (65% application / 35% theory across the
# last 15 real sessions) rather than an arbitrary number.
TARGET_APPLICATION_RATIO = 0.65
_RATIO_TOLERANCE = 0.10  # only correct once the running ratio drifts this far off target -- avoids fighting normal short-term noise (e.g. the very first couple of switches, where any single topic swings the ratio hard)


def enforce_topic_ratio(*, conversation: list[dict], role: str, chosen_topic: str) -> str:
    """
    Called on every switch_topic decision (forced by the turn-budget cap,
    OR the interviewer's own discretionary choice) to keep a role with
    BOTH topic types roughly on the target application:theory split,
    rather than trusting the model's own topic choice to average out
    correctly on its own -- same "deterministic guardrail over model
    self-compliance" precedent as every other topic-tracking fix in this
    file/llm.py. A role with only one topic type (e.g. Power Automate
    Developer, all-conceptual) has nothing to balance and is returned
    unchanged.

    Only OVERRIDES `chosen_topic` when the running ratio has drifted
    more than _RATIO_TOLERANCE off target AND the model's own choice
    would make that drift worse -- a choice that's already correcting
    the balance, or within tolerance, passes through untouched.
    """
    mix = ROLE_TOPIC_MIX.get(role)
    if not mix or not mix["sql"] or not mix["conceptual"]:
        return chosen_topic

    sql_set, conceptual_set = set(mix["sql"]), set(mix["conceptual"])
    sql_count = sum(1 for t in conversation if t.get("role") == "assistant" and t.get("topic") in sql_set)
    concept_count = sum(1 for t in conversation if t.get("role") == "assistant" and t.get("topic") in conceptual_set)
    total = sql_count + concept_count
    if total == 0:
        return chosen_topic  # nothing asked yet to measure a ratio against

    current_ratio = sql_count / total
    covered = {t.get("topic") for t in conversation if t.get("topic")}

    if current_ratio < TARGET_APPLICATION_RATIO - _RATIO_TOLERANCE and chosen_topic not in sql_set:
        uncovered = [t for t in mix["sql"] if t not in covered]
        return uncovered[0] if uncovered else mix["sql"][0]
    if current_ratio > TARGET_APPLICATION_RATIO + _RATIO_TOLERANCE and chosen_topic not in conceptual_set:
        uncovered = [t for t in mix["conceptual"] if t not in covered]
        return uncovered[0] if uncovered else mix["conceptual"][0]
    return chosen_topic
