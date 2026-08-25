"""
Mock SQL interview orchestration.

Unlike the graded practice problems (verified by actually running SQL in
DuckDB), a spoken interview answer is open-ended and conversational -- there
is no single correct output to diff against. So this module leans entirely
on the LLM to act as the interviewer: decide whether to follow up on a gap,
probe deeper on a promising thread, or move to a new topic, per the
progression the product spec calls for.

Session state is persisted to Postgres (db.py) on every mutation, so an
in-progress interview survives a browser crash, a transient LLM failure, or
the backend restarting -- see db.py's docstring for why. Every function
here takes/returns a plain dict (not an ORM object) to keep this simple.
"""

import json
import time
import uuid

import db
import topics

INTERVIEW_DURATION_SECONDS = 45 * 60
MIN_INTERVIEW_DURATION_SECONDS = 20 * 60
TRIAL_DURATION_SECONDS = 10 * 60  # fixed length for a free-tier trial interview, below the paid 20-45 min range
MAX_TURNS_PER_TOPIC = 3  # initial question + at most 2 follow_up/probe before a forced switch_topic
MAX_TURNS_INTRO = 2  # intro question + at most 1 follow-up -- it's a brief icebreaker, not a real interview topic, so the generic 3-turn budget is too generous here

# The interview can talk about every topic, including DML -- it's purely
# conversational, nothing gets executed, so the sandbox's read-only
# invariant (see topics.py, sandbox.py) doesn't apply here.
GENERIC_TOPICS = topics.ALL_TOPICS


def create_session(*, user_id: str, mode: str, resume_text: str | None, skip_intro: bool, duration_seconds: int = INTERVIEW_DURATION_SECONDS, is_trial: bool = False, persona: str = "neutral") -> dict:
    if is_trial:
        # A separate branch, not a lowered floor -- so a paid user's
        # request can never accidentally slide under the real 20 min floor.
        duration_seconds = TRIAL_DURATION_SECONDS
    else:
        duration_seconds = max(MIN_INTERVIEW_DURATION_SECONDS, min(INTERVIEW_DURATION_SECONDS, duration_seconds))
    session = {
        "session_id": str(uuid.uuid4()),
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
        "last_table_context": None,
        "ended": False,
        "feedback": None,
        "persona": persona,  # "friendly" | "neutral" | "strict" -- immutable for the session's life
    }
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO interview_sessions
                    (session_id, user_id, mode, resume_text, skip_intro, duration_seconds,
                     started_at, topics_covered, conversation, current_topic,
                     current_topic_turns, last_table_context, ended, feedback, persona)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    session["session_id"], session["user_id"], session["mode"],
                    session["resume_text"], session["skip_intro"], session["duration_seconds"],
                    session["started_at"], json.dumps(session["topics_covered"]),
                    json.dumps(session["conversation"]), session["current_topic"],
                    session["current_topic_turns"], json.dumps(session["last_table_context"]),
                    session["ended"], json.dumps(session["feedback"]), session["persona"],
                ),
            )
    return session


def save_session(session: dict):
    """Persists the current in-memory state of `session` to Postgres. Call
    after any mutation (record_turn, update_topic_tracking, mark_ended,
    setting last_table_context, ...) so a crash right after doesn't lose it."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE interview_sessions SET
                    topics_covered=%s, conversation=%s, current_topic=%s,
                    current_topic_turns=%s, last_table_context=%s, ended=%s, feedback=%s
                WHERE session_id=%s
                """,
                (
                    json.dumps(session["topics_covered"]), json.dumps(session["conversation"]),
                    session["current_topic"], session["current_topic_turns"],
                    json.dumps(session["last_table_context"]), session["ended"],
                    json.dumps(session["feedback"]), session["session_id"],
                ),
            )


def get_session(session_id: str) -> dict | None:
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM interview_sessions WHERE session_id = %s", (session_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def elapsed_seconds(session: dict) -> float:
    return time.time() - session["started_at"]


def remaining_seconds(session: dict) -> float:
    return max(0, session["duration_seconds"] - elapsed_seconds(session))


def is_time_up(session: dict) -> bool:
    return remaining_seconds(session) <= 0


def record_turn(session: dict, role: str, content: str, topic: str | None = None):
    session["conversation"].append({"role": role, "content": content, "topic": topic})
    save_session(session)


def remove_last_turn(session: dict):
    """Rolls back the most recently recorded turn (e.g. after an LLM call
    fails partway through a request) so a resumed session doesn't retain a
    dangling turn with no reply."""
    if session["conversation"]:
        session["conversation"].pop()
        save_session(session)


def update_topic_tracking(session: dict, action: str, topic: str, candidate_stuck: bool = False):
    """
    Tracks how many consecutive question-turns have been spent on the
    current topic, so callers can enforce MAX_TURNS_PER_TOPIC deterministically
    rather than relying on the model to police its own turn budget.

    `candidate_stuck` (the model's read on whether the answer just given was
    a genuine non-attempt, e.g. "I don't know") gets the same deterministic
    treatment: if true, the turn counter is maxed out so the topic is forced
    to switch on the very next turn, regardless of what action the model
    picked for THIS turn -- it's been observed to keep picking follow_up and
    re-asking a near-identical question rather than switching after a clear
    non-answer, same class of unreliability as the turn-budget itself.
    """
    if action == "switch_topic" or session["current_topic"] is None:
        session["current_topic"] = topic
        session["current_topic_turns"] = 1
        if topic not in session["topics_covered"]:
            session["topics_covered"].append(topic)
    else:
        session["current_topic_turns"] += 1
        if candidate_stuck:
            session["current_topic_turns"] = max(session["current_topic_turns"], MAX_TURNS_PER_TOPIC)
    save_session(session)


def set_last_table_context(session: dict, table_context: dict | None):
    if table_context:
        session["last_table_context"] = table_context
        save_session(session)


def mark_ended(session: dict, feedback: dict):
    session["ended"] = True
    session["feedback"] = feedback
    save_session(session)


def topic_cap_reached(session: dict) -> bool:
    limit = MAX_TURNS_INTRO if session["current_topic"] == "intro" else MAX_TURNS_PER_TOPIC
    return session["current_topic_turns"] >= limit


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


def last_question(session: dict) -> dict | None:
    """Returns the most recent assistant turn (the question currently
    awaiting an answer), used to rehydrate a resumed session's UI."""
    for turn in reversed(session["conversation"]):
        if turn["role"] == "assistant":
            return turn
    return None
