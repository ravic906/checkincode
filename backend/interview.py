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
CONNECTION_ISSUE_THRESHOLD = 2  # 2 consecutive STT/LLM failures -- one retry chance after the first, then proactively offer to retry/pause/end

# Superseded by role_topics.topics_for_role(target_role) now that every
# interview is role-based (SQL topics blended with conceptual ones) rather
# than always this one fixed SQL-only list -- kept only in case anything
# external still imports it; no call site in this codebase reads it anymore.
GENERIC_TOPICS = topics.ALL_TOPICS


def create_session(*, user_id: str, target_role: str, resume_text: str | None, skip_intro: bool, duration_seconds: int = INTERVIEW_DURATION_SECONDS, is_trial: bool = False, persona: str = "neutral", candidate_profile: dict | None = None, awaiting_history_pref: bool = False) -> dict:
    if is_trial:
        # A separate branch, not a lowered floor -- so a paid user's
        # request can never accidentally slide under the real 20 min floor.
        duration_seconds = TRIAL_DURATION_SECONDS
    else:
        duration_seconds = max(MIN_INTERVIEW_DURATION_SECONDS, min(INTERVIEW_DURATION_SECONDS, duration_seconds))
    session = {
        "session_id": str(uuid.uuid4()),
        "user_id": user_id,
        "mode": "role_based",  # legacy NOT NULL column, no longer read anywhere -- see role_topics.py for the real replacement (target_role)
        "target_role": target_role,  # one of role_topics.ROLES
        "candidate_profile": candidate_profile,  # [Profile Analyzer] output, computed once at session start
        "resume_text": resume_text,
        "skip_intro": skip_intro,
        "duration_seconds": duration_seconds,
        "started_at": time.time(),
        "topics_covered": [],  # list of topic names that have been asked about
        "conversation": [],  # [{"role": "assistant"|"user", "content": str, "topic": str|None}]
        "current_topic": None,
        "current_topic_turns": 0,
        "hint_used_this_topic": False,
        "consecutive_failures": 0,
        "last_table_context": None,
        "ended": False,
        "feedback": None,
        "persona": persona,  # "friendly" | "neutral" | "strict" -- immutable for the session's life
        "awaiting_history_pref": awaiting_history_pref,
    }
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO interview_sessions
                    (session_id, user_id, mode, target_role, candidate_profile, resume_text, skip_intro, duration_seconds,
                     started_at, topics_covered, conversation, current_topic,
                     current_topic_turns, hint_used_this_topic, consecutive_failures, last_table_context, ended, feedback, persona,
                     awaiting_history_pref)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    session["session_id"], session["user_id"], session["mode"],
                    session["target_role"], json.dumps(session["candidate_profile"]),
                    session["resume_text"], session["skip_intro"], session["duration_seconds"],
                    session["started_at"], json.dumps(session["topics_covered"]),
                    json.dumps(session["conversation"]), session["current_topic"],
                    session["current_topic_turns"], session["hint_used_this_topic"],
                    session["consecutive_failures"],
                    json.dumps(session["last_table_context"]),
                    session["ended"], json.dumps(session["feedback"]), session["persona"],
                    session["awaiting_history_pref"],
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
                    current_topic_turns=%s, hint_used_this_topic=%s, consecutive_failures=%s,
                    last_table_context=%s, ended=%s, feedback=%s, awaiting_history_pref=%s,
                    candidate_profile=%s
                WHERE session_id=%s
                """,
                (
                    json.dumps(session["topics_covered"]), json.dumps(session["conversation"]),
                    session["current_topic"], session["current_topic_turns"],
                    session.get("hint_used_this_topic", False),
                    session.get("consecutive_failures", 0),
                    json.dumps(session["last_table_context"]), session["ended"],
                    json.dumps(session["feedback"]), session.get("awaiting_history_pref", False),
                    json.dumps(session.get("candidate_profile")), session["session_id"],
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


def update_topic_tracking(session: dict, action: str, topic: str, candidate_stuck: bool = False, offer_hint: bool = False):
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

    `offer_hint` (the model gave a hint or a simpler restated question this
    turn) gets the same backstop, capped at one hint per topic: if it's
    already used its one hint on this topic and tries to offer another, the
    turn counter is maxed out the same way, forcing a switch rather than
    trusting the model to stop offering hints on its own.

    `action` is NOT used to decide whether a switch happened, deliberately
    -- confirmed live, the model mismatches the two in both directions: it
    labels a turn switch_topic while re-asking essentially the same
    question under a same-or-wrong topic name (which would reset the
    budget every turn and let it cycle on one topic indefinitely), and
    separately labels a turn follow_up while `topic` has actually already
    drifted to something else (which would keep incrementing the wrong
    topic's counter while session["current_topic"] silently stops matching
    what's actually being asked). Whether `topic` itself differs from the
    topic already in progress is the only signal trusted here.
    """
    if session["current_topic"] is None or topic != session["current_topic"]:
        session["current_topic"] = topic
        session["current_topic_turns"] = 1
        session["hint_used_this_topic"] = False
        if topic not in session["topics_covered"]:
            session["topics_covered"].append(topic)
    else:
        session["current_topic_turns"] += 1
        if offer_hint:
            if session.get("hint_used_this_topic"):
                session["current_topic_turns"] = max(session["current_topic_turns"], MAX_TURNS_PER_TOPIC)
            else:
                session["hint_used_this_topic"] = True
        if candidate_stuck:
            session["current_topic_turns"] = max(session["current_topic_turns"], MAX_TURNS_PER_TOPIC)
    save_session(session)


def record_failure(session: dict):
    """[Evaluator] Counts a consecutive STT/LLM failure toward
    CONNECTION_ISSUE_THRESHOLD -- called from the except-block of any
    interview endpoint that can fail transiently (LLM call, transcription).
    """
    session["consecutive_failures"] = session.get("consecutive_failures", 0) + 1
    save_session(session)


def reset_failures(session: dict):
    """Called after any successful turn, so an isolated glitch doesn't
    accumulate toward the threshold across unrelated later turns."""
    if session.get("consecutive_failures"):
        session["consecutive_failures"] = 0
        save_session(session)


def set_last_table_context(session: dict, table_context: dict | None):
    if table_context:
        session["last_table_context"] = table_context
        save_session(session)


def mark_ended(session: dict, feedback: dict):
    session["ended"] = True
    session["feedback"] = feedback
    save_session(session)


def record_topic_history(user_id: str, session_id: str, topic_scores: list[dict]):
    """
    [Feedback Generator] One row per topic_scores entry from a just-
    generated feedback report -- called from /api/interview/end right after
    mark_ended(). Silently does nothing for an empty list (e.g. a very
    short/abandoned interview with no scored topics) rather than erroring.
    """
    if not topic_scores:
        return
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO interview_topic_history (user_id, session_id, topic, score) VALUES (%s,%s,%s,%s)",
                [(user_id, session_id, t["topic"], t["score"]) for t in topic_scores if t.get("topic") is not None],
            )


def get_topic_history(user_id: str, topics: list[str] | None = None, limit_per_topic: int = 5) -> dict[str, list[dict]]:
    """
    [Profile Analyzer / Feedback Generator] Recent scores per topic across
    ALL of a user's past interviews, most recent first -- used to weight a
    new interview's recommended_topics toward rechecking weak areas, and to
    ground a feedback report's trend_note in real prior data rather than
    letting the model fabricate a "since last time" claim. Returns
    {topic: [{"score": int, "recorded_at": iso str}, ...]}, only for topics
    that have at least one recorded score (an empty dict means this user
    has no interview history yet).
    """
    query = "SELECT topic, score, recorded_at FROM interview_topic_history WHERE user_id = %s"
    params: list = [user_id]
    if topics:
        query += " AND topic = ANY(%s)"
        params.append(topics)
    query += " ORDER BY recorded_at DESC"
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    history: dict[str, list[dict]] = {}
    for row in rows:
        entries = history.setdefault(row["topic"], [])
        if len(entries) < limit_per_topic:
            entries.append({"score": row["score"], "recorded_at": row["recorded_at"].isoformat()})
    return history


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
