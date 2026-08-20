"""
Postgres-backed per-user tier + daily usage counters.

Replaces the original MVP's in-memory _USAGE dict, which wiped on every
backend restart -- a real problem on Render's free tier (spins down after
15 min idle), where a paying user's tier would silently revert to "free"
until the next request happened to land after a fresh restart re-created
their in-memory entry. Persisting to the `users` table (already the same
Postgres used for interview sessions and problems) fixes that.
"""

import datetime

import db


def _today():
    return datetime.date.today().isoformat()


def _row_to_usage(row: dict) -> dict:
    return {
        "tier": row["tier"],
        "submissions": row["submissions_today"],
        "explanations": row["explanations_today"],
    }


def get_usage(user_id: str) -> dict:
    """Fetches (creating if needed) the user's tier + today's counters,
    resetting the daily counters if the stored date isn't today. Returns
    {"tier", "submissions", "explanations"} -- same shape the old
    in-memory _get_usage returned, so call sites didn't need to change."""
    today = _today()
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO users (id, tier, usage_date, submissions_today, explanations_today)
                    VALUES (%s, 'free', %s, 0, 0)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING *
                    """,
                    (user_id, today),
                )
                row = cur.fetchone()
                if row is None:
                    # Lost a race with a concurrent request creating the same
                    # user -- just re-read what they inserted.
                    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                    row = cur.fetchone()
            elif row["usage_date"] is None or row["usage_date"].isoformat() != today:
                cur.execute(
                    """
                    UPDATE users SET usage_date = %s, submissions_today = 0, explanations_today = 0
                    WHERE id = %s
                    RETURNING *
                    """,
                    (today, user_id),
                )
                row = cur.fetchone()
            return _row_to_usage(row)


def increment_submission(user_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET submissions_today = submissions_today + 1 WHERE id = %s", (user_id,))


def set_tier(user_id: str, tier: str, email: str | None = None):
    """Upserts so a payment webhook/verify call succeeds even if this is
    the user's very first request (e.g. they signed in on a different
    device than the one that has practice history)."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, email, tier, usage_date, submissions_today, explanations_today)
                VALUES (%s, %s, %s, %s, 0, 0)
                ON CONFLICT (id) DO UPDATE SET tier = EXCLUDED.tier,
                    email = COALESCE(EXCLUDED.email, users.email)
                """,
                (user_id, email, tier, _today()),
            )
