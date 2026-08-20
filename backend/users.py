"""
Postgres-backed per-user tier + daily/monthly usage counters.

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


def _current_month():
    return datetime.date.today().strftime("%Y-%m")


def _row_to_usage(row: dict) -> dict:
    return {
        "tier": row["tier"],
        "submissions": row["submissions_today"],
        "explanations": row["explanations_today"],
        "interview_trial_used": row["interview_trial_used"],
        "interviews_this_month": row["interviews_this_month"],
    }


def get_usage(user_id: str) -> dict:
    """Fetches (creating if needed) the user's tier + today's/this month's
    counters, resetting each on its own rollover (daily counters if the
    stored date isn't today, the interview counter if the stored month
    isn't this month). Returns {"tier", "submissions", "explanations",
    "interview_trial_used", "interviews_this_month"}."""
    today = _today()
    month = _current_month()
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO users (id, tier, usage_date, submissions_today, explanations_today, interview_month, interviews_this_month)
                    VALUES (%s, 'free', %s, 0, 0, %s, 0)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING *
                    """,
                    (user_id, today, month),
                )
                row = cur.fetchone()
                if row is None:
                    # Lost a race with a concurrent request creating the same
                    # user -- just re-read what they inserted.
                    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                    row = cur.fetchone()
            else:
                needs_daily_reset = row["usage_date"] is None or row["usage_date"].isoformat() != today
                needs_monthly_reset = row["interview_month"] != month
                if needs_daily_reset or needs_monthly_reset:
                    sets, params = [], []
                    if needs_daily_reset:
                        sets += ["usage_date = %s", "submissions_today = 0", "explanations_today = 0"]
                        params += [today]
                    if needs_monthly_reset:
                        sets += ["interview_month = %s", "interviews_this_month = 0"]
                        params += [month]
                    params.append(user_id)
                    cur.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s RETURNING *", params)
                    row = cur.fetchone()
            return _row_to_usage(row)


def increment_submission(user_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET submissions_today = submissions_today + 1 WHERE id = %s", (user_id,))


def mark_interview_trial_used(user_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET interview_trial_used = TRUE WHERE id = %s", (user_id,))


def increment_interview_count(user_id: str):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET interviews_this_month = interviews_this_month + 1 WHERE id = %s", (user_id,))


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
