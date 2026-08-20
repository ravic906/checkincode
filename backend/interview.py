"""
Mock SQL interview orchestration.

Unlike the graded practice problems (verified by actually running SQL in
DuckDB), a spoken interview answer is open-ended and conversational -- there
is no single correct output to diff against. So this module leans entirely
on the LLM to act as the interviewer: decide whether to follow up on a gap,
probe deeper on a promising thread, or move to a new topic, per the
progression the product spec calls for.

Sessions live in memory only (same tradeoff as usage tracking in main.py) --
fine for an MVP, would need a real store (Redis/Postgres) before this survives
a server restart mid-interview reliably.
"""

import time
import uuid

INTERVIEW_DURATION_SECONDS = 45 * 60
MIN_INTERVIEW_DURATION_SECONDS = 20 * 60
MAX_TURNS_PER_TOPIC = 3  # initial question + at most 2 follow_up/probe before a forced switch_topic

GENERIC_TOPICS = [
    "SELECT / WHERE filtering basics",
    "JOINs (inner, left, self-join)",
    "Aggregation and GROUP BY / HAVING",
    "Subqueries and CTEs",
    "Window functions",
    "NULL handling semantics",
    "Indexing and query performance",
    "Normalization and schema design",
    "Transactions and ACID properties",
]

# session_id -> session dict
_SESSIONS: dict[str, dict] = {}


def create_session(*, user_id: str, mode: str, resume_text: str | None, skip_intro: bool, duration_seconds: int = INTERVIEW_DURATION_SECONDS) -> dict:
    duration_seconds = max(MIN_INTERVIEW_DURATION_SECONDS, min(INTERVIEW_DURATION_SECONDS, duration_seconds))
    session_id = str(uuid.uuid4())
    session = {
        "session_id": session_id,
        "user_id": user_id,
        "mode": mode,  # "personalized" | "generic"
        "resume_text": resume_text,
        "skip_intro": skip_intro,
        "duration_seconds": duration_seconds,
        "started_at": time.time(),
        "topics_covered": [],  # list of topic names that have been asked about
        "conversation": [],  # [{"role": "assistant"|"user", "content": str, "topic": str|None}]
        "current_topic": None,
        "current_topic_turns": 0,
        "ended": False,
        "feedback": None,
    }
    _SESSIONS[session_id] = session
    return session


def get_session(session_id: str) -> dict | None:
    return _SESSIONS.get(session_id)


def elapsed_seconds(session: dict) -> float:
    return time.time() - session["started_at"]


def remaining_seconds(session: dict) -> float:
    return max(0, session["duration_seconds"] - elapsed_seconds(session))


def is_time_up(session: dict) -> bool:
    return remaining_seconds(session) <= 0


def record_turn(session: dict, role: str, content: str, topic: str | None = None):
    session["conversation"].append({"role": role, "content": content, "topic": topic})


def update_topic_tracking(session: dict, action: str, topic: str):
    """
    Tracks how many consecutive question-turns have been spent on the
    current topic, so callers can enforce MAX_TURNS_PER_TOPIC deterministically
    rather than relying on the model to police its own turn budget.
    """
    if action == "switch_topic" or session["current_topic"] != topic:
        session["current_topic"] = topic
        session["current_topic_turns"] = 1
        if topic not in session["topics_covered"]:
            session["topics_covered"].append(topic)
    else:
        session["current_topic_turns"] += 1


def topic_cap_reached(session: dict) -> bool:
    return session["current_topic_turns"] >= MAX_TURNS_PER_TOPIC


def next_topic(session: dict, topics: list[str]) -> str:
    """
    Deterministically picks the next topic to force a switch to, once
    MAX_TURNS_PER_TOPIC is hit -- prefers a topic not yet covered this
    interview, cycling back through the list if everything's been touched.
    """
    uncovered = [t for t in topics if t not in session["topics_covered"]]
    if uncovered:
        return uncovered[0]
    # Everything's been covered at least once -- cycle to the topic after
    # the current one so we don't just immediately re-pick the same topic.
    if session["current_topic"] in topics:
        idx = topics.index(session["current_topic"])
        return topics[(idx + 1) % len(topics)]
    return topics[0]
