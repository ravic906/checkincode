"""
Candidate progress dashboard -- per-topic strengths/weaknesses computed
from a user's own submission history, a per-topic dismiss/restore filter
(purely cosmetic, never touches the underlying submissions), and a single
"what to practice next" suggestion.

Deliberately built on top of the existing `submissions` + `problems`
tables rather than a new tracking table -- every track (sql/python/case)
already writes one row per submission with a `correct` boolean (case's
rubric score thresholded at 70, see main.py's api_case_submit), so a
join against problems.topic is the whole aggregation; nothing new needs
to be recorded going forward.
"""
import db

# A topic needs at least this many DISTINCT problems attempted before its
# solve rate is trusted enough to call it a strength or a weakness -- one
# lucky/unlucky attempt shouldn't label a whole topic.
MIN_ATTEMPTS_FOR_SIGNAL = 2
STRENGTH_THRESHOLD = 0.7
WEAKNESS_THRESHOLD = 0.5
DIFFICULTY_RANK = {"easy": 0, "medium": 1, "hard": 2}


def _topic_stats(user_id: str) -> list[dict]:
    """One row per (track, topic) this user has ever attempted anything
    in: distinct problems attempted, distinct problems ever solved
    (correct on ANY attempt, not just the latest), and the resulting
    solve rate."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    p.track,
                    p.topic,
                    COUNT(DISTINCT s.problem_id) AS attempted,
                    COUNT(DISTINCT s.problem_id) FILTER (WHERE s.solved) AS solved
                FROM (
                    SELECT problem_id, BOOL_OR(correct) AS solved
                    FROM submissions
                    WHERE user_id = %s
                    GROUP BY problem_id
                ) s
                JOIN problems p ON p.id = s.problem_id
                GROUP BY p.track, p.topic
                ORDER BY p.track, p.topic
                """,
                (user_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["solve_rate"] = (r["solved"] / r["attempted"]) if r["attempted"] else 0.0
    return rows


def _dismissed_set(user_id: str) -> set[tuple[str, str]]:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT track, topic FROM user_topic_dismissals WHERE user_id = %s",
                (user_id,),
            )
            return {(track, topic) for track, topic in cur.fetchall()}


def dismiss_topic(user_id: str, track: str, topic: str) -> None:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_topic_dismissals (user_id, track, topic)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, track, topic) DO NOTHING
                """,
                (user_id, track, topic),
            )


def restore_topic(user_id: str, track: str, topic: str) -> None:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_topic_dismissals WHERE user_id = %s AND track = %s AND topic = %s",
                (user_id, track, topic),
            )


def _topic_catalog(track: str | None = None) -> list[dict]:
    """Every LIVE topic in the bank (optionally scoped to one track) with
    its average difficulty rank -- used only to pick a sensible "next
    logical topic" when a candidate has no live weakness left, by
    ordering their untried topics from easiest to hardest rather than
    picking one at random."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            query = "SELECT track, topic, difficulty FROM problems WHERE status = 'live'"
            params = []
            if track:
                query += " AND track = %s"
                params.append(track)
            cur.execute(query, params)
            rows = [dict(r) for r in cur.fetchall()]

    catalog: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        key = (r["track"], r["topic"])
        catalog.setdefault(key, []).append(DIFFICULTY_RANK.get(r["difficulty"], 1))

    return [
        {"track": t, "topic": top, "avg_difficulty": sum(ranks) / len(ranks), "problem_count": len(ranks)}
        for (t, top), ranks in catalog.items()
    ]


def get_progress(user_id: str) -> dict:
    stats = _topic_stats(user_id)
    dismissed = _dismissed_set(user_id)

    def is_dismissed(r):
        return (r["track"], r["topic"]) in dismissed

    signal_rows = [r for r in stats if r["attempted"] >= MIN_ATTEMPTS_FOR_SIGNAL]

    strengths = sorted(
        (r for r in signal_rows if r["solve_rate"] >= STRENGTH_THRESHOLD and not is_dismissed(r)),
        key=lambda r: (-r["solve_rate"], -r["attempted"]),
    )
    weaknesses = sorted(
        (r for r in signal_rows if r["solve_rate"] <= WEAKNESS_THRESHOLD and not is_dismissed(r)),
        key=lambda r: (r["solve_rate"], -r["attempted"]),
    )

    # Overall per-track totals (attempted/solved out of everything live in
    # the bank), regardless of dismiss state -- dismissing a topic only
    # hides it from Strengths/Weaknesses, never from the real numbers.
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT track, COUNT(*) AS total FROM problems WHERE status = 'live' GROUP BY track")
            totals_by_track = {r["track"]: r["total"] for r in cur.fetchall()}
    overall = {}
    for r in stats:
        t = overall.setdefault(r["track"], {"attempted": 0, "solved": 0, "total_available": totals_by_track.get(r["track"], 0)})
        t["attempted"] += r["attempted"]
        t["solved"] += r["solved"]
    for track, total in totals_by_track.items():
        overall.setdefault(track, {"attempted": 0, "solved": 0, "total_available": total})

    suggestion = _suggest_next_topic(weaknesses, stats, dismissed)

    return {
        "overall": overall,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "suggested_next_topic": suggestion,
    }


def _suggest_next_topic(weaknesses: list[dict], all_stats: list[dict], dismissed: set[tuple[str, str]]) -> dict | None:
    # Weak spot exists -> that's the practice priority, full stop.
    if weaknesses:
        top = weaknesses[0]
        return {
            "track": top["track"],
            "topic": top["topic"],
            "reason": f"Your weakest area so far -- solved {top['solved']} of {top['attempted']} attempted "
                      f"({round(top['solve_rate'] * 100)}%). Worth another pass before moving on.",
            "basis": "weakness",
        }

    # No weakness (dismissed or genuinely solid on everything attempted) --
    # find the easiest UNTRIED topic per track and recommend the easiest
    # overall, so progression stays roughly beginner-to-advanced.
    attempted_keys = {(r["track"], r["topic"]) for r in all_stats}
    candidates = [
        t for t in _topic_catalog()
        if (t["track"], t["topic"]) not in attempted_keys
        and (t["track"], t["topic"]) not in dismissed
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t["avg_difficulty"], -t["problem_count"]))
    pick = candidates[0]
    solid = bool(all_stats)
    return {
        "track": pick["track"],
        "topic": pick["topic"],
        "reason": (
            "You're solid on everything you've tried so far -- next logical topic to pick up."
            if solid else
            "A good place to start -- one of the more foundational topics in the bank."
        ),
        "basis": "progression",
    }
