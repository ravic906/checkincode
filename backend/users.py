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
import json

import db

# Prepaid Pro access window: a payment buys this many days of `paid` tier
# from the purchase date (or from the current pro_expires_at if renewing
# before it lapses, so paying early never loses remaining days). There's
# no real recurring auto-debit -- see set_pro_period()/cancel_pro().
PLAN_DURATION_DAYS = {"monthly": 30, "yearly": 365}


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
        "pro_plan": row.get("pro_plan"),
        "pro_expires_at": row["pro_expires_at"].isoformat() if row.get("pro_expires_at") else None,
        "pro_auto_renew": row.get("pro_auto_renew", True),
        "has_resume": bool(row.get("resume_text")),
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

            # Prepaid Pro window lapsed -- there's no recurring auto-debit
            # to fail, so this is the only place tier actually reverts to
            # free once pro_expires_at has passed (checked lazily here,
            # same pattern as the daily/monthly counter rollovers above).
            if row["tier"] == "paid" and row["pro_expires_at"] is not None:
                if row["pro_expires_at"] < datetime.datetime.now(datetime.timezone.utc):
                    cur.execute(
                        """
                        UPDATE users SET tier = 'free', pro_plan = NULL,
                            pro_expires_at = NULL, pro_auto_renew = TRUE
                        WHERE id = %s RETURNING *
                        """,
                        (user_id,),
                    )
                    row = cur.fetchone()
            return _row_to_usage(row)


def get_resume(user_id: str) -> str | None:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT resume_text FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return row[0] if row else None


def set_resume(user_id: str, resume_text: str) -> None:
    """Upserts so this works even if the target has never hit any other
    endpoint yet (same pattern as set_admin) -- persists the resume to the
    account so future interviews can reuse it without a re-upload. Called
    by the same parse-resume endpoint that used to only return the parsed
    text for one-off use; uploading is how "update" works, no separate
    endpoint needed."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, tier, usage_date, submissions_today, explanations_today, resume_text)
                VALUES (%s, 'free', %s, 0, 0, %s)
                ON CONFLICT (id) DO UPDATE SET resume_text = EXCLUDED.resume_text
                """,
                (user_id, _today(), resume_text),
            )


def delete_resume(user_id: str) -> None:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET resume_text = NULL WHERE id = %s", (user_id,))


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


def record_profile_info(user_id: str, email: str | None = None, username: str | None = None, full_name: str | None = None) -> None:
    """Best-effort capture of a signed-in user's Clerk profile fields,
    called from high-traffic identity-bearing endpoints (see main.py's
    /api/usage) so admin support/lookups don't depend on a payment ever
    having happened (set_tier/set_pro_period were the only prior writers
    of the email column). Upserts so it works even if the user has never
    hit any other endpoint. Each field only overwrites when actually
    provided -- a request missing one (e.g. no username set in Clerk)
    never blanks out a value captured on an earlier call."""
    if not email and not username and not full_name:
        return
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, email, username, full_name, tier, usage_date, submissions_today, explanations_today)
                VALUES (%s, %s, %s, %s, 'free', %s, 0, 0)
                ON CONFLICT (id) DO UPDATE SET
                    email = COALESCE(EXCLUDED.email, users.email),
                    username = COALESCE(EXCLUDED.username, users.username),
                    full_name = COALESCE(EXCLUDED.full_name, users.full_name)
                """,
                (user_id, email, username, full_name, _today()),
            )


def record_activity(user_id: str, event_type: str, metadata: dict | None = None) -> None:
    """Appends one row to the general site-activity log (see db.py's
    user_activity_events) -- best-effort, fire-and-forget from callers'
    perspective. Separate from submissions/interview tables, which already
    fully capture their own domains; this is for everything else worth
    showing on a per-user activity timeline (sign-ins, track switches,
    interview start/end, resume actions, plan changes)."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_activity_events (user_id, event_type, metadata) VALUES (%s, %s, %s)",
                (user_id, event_type, json.dumps(metadata) if metadata is not None else None),
            )


def get_activity(user_id: str, limit: int = 300) -> list[dict]:
    """Most recent activity events for one user, newest first -- for the
    admin per-user drill-down page."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                "SELECT event_type, metadata, created_at FROM user_activity_events "
                "WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]


def list_admins() -> list[dict]:
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT id, email, created_at FROM users WHERE is_admin = TRUE ORDER BY created_at")
            return [dict(r) for r in cur.fetchall()]


def is_admin(user_id: str) -> bool:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT is_admin FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return bool(row and row[0])


def set_admin(user_id: str, is_admin_value: bool) -> None:
    """Grants or revokes admin rights for an explicit target -- always
    called by an existing admin (or the bootstrap static token), never
    by the target account itself (see /api/admin/set-admin). Upserts so
    this works even if the target has never hit any other endpoint yet."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, tier, usage_date, submissions_today, explanations_today, is_admin)
                VALUES (%s, 'free', %s, 0, 0, %s)
                ON CONFLICT (id) DO UPDATE SET is_admin = EXCLUDED.is_admin
                """,
                (user_id, _today(), is_admin_value),
            )


def get_admin_summary() -> dict:
    """Total users + tier breakdown, for the admin user-analytics page."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE tier = 'paid') AS paid FROM users")
            row = cur.fetchone()
            return {
                "total_users": row["total"],
                "paid_users": row["paid"],
                "free_users": row["total"] - row["paid"],
            }


def list_all_users() -> list[dict]:
    """Every user with aggregated submission stats, for the admin
    user-analytics page's user table. Per-user submission-level detail
    (which problems, when) is a separate call -- see
    problems.get_user_submission_history -- since that's only fetched
    on demand when an admin drills into one user."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("""
                SELECT u.id, u.email, u.username, u.full_name, u.tier, u.submissions_today,
                       u.interviews_this_month, u.created_at,
                       COUNT(s.id) AS total_submissions,
                       COUNT(s.id) FILTER (WHERE s.correct) AS correct_submissions,
                       COUNT(DISTINCT s.problem_id) FILTER (WHERE s.correct) AS solved_count
                FROM users u
                LEFT JOIN submissions s ON s.user_id = u.id
                GROUP BY u.id, u.email, u.username, u.full_name, u.tier, u.submissions_today,
                         u.interviews_this_month, u.created_at
                ORDER BY u.created_at DESC
            """)
            return [dict(r) for r in cur.fetchall()]


def set_tier(user_id: str, tier: str, email: str | None = None):
    """Upserts so a payment webhook/verify call succeeds even if this is
    the user's very first request (e.g. they signed in on a different
    device than the one that has practice history). Manual admin
    override -- doesn't touch pro_plan/pro_expires_at, so an admin-granted
    'paid' tier doesn't carry a fake expiry date (stays paid until an
    admin changes it back), and downgrading a real paid subscriber this
    way also clears their prepaid window so it can't silently resurrect
    itself via the next get_usage() lazy check."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, email, tier, usage_date, submissions_today, explanations_today)
                VALUES (%s, %s, %s, %s, 0, 0)
                ON CONFLICT (id) DO UPDATE SET tier = EXCLUDED.tier,
                    email = COALESCE(EXCLUDED.email, users.email),
                    pro_plan = CASE WHEN EXCLUDED.tier = 'free' THEN NULL ELSE users.pro_plan END,
                    pro_expires_at = CASE WHEN EXCLUDED.tier = 'free' THEN NULL ELSE users.pro_expires_at END
                """,
                (user_id, email, tier, _today()),
            )


def set_pro_period(user_id: str, plan: str, email: str | None = None):
    """Called after a verified payment: grants `paid` tier through a new
    expiry date. If the user already has unexpired Pro time left (e.g.
    renewing a few days early), the new period stacks on top of it
    instead of overwriting it, so paying early never costs them days.
    Also resets pro_auto_renew to True -- a fresh purchase is itself the
    user opting back in, even if they'd previously cancelled."""
    if plan not in PLAN_DURATION_DAYS:
        raise ValueError(f"Unknown plan '{plan}'")
    days = PLAN_DURATION_DAYS[plan]
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, email, tier, usage_date, submissions_today, explanations_today, pro_plan, pro_expires_at, pro_auto_renew)
                VALUES (%s, %s, 'paid', %s, 0, 0, %s, now() + make_interval(days => %s), TRUE)
                ON CONFLICT (id) DO UPDATE SET
                    tier = 'paid',
                    email = COALESCE(EXCLUDED.email, users.email),
                    pro_plan = %s,
                    pro_expires_at = GREATEST(now(), COALESCE(users.pro_expires_at, now())) + make_interval(days => %s),
                    pro_auto_renew = TRUE
                """,
                (user_id, email, _today(), plan, days, plan, days),
            )


def cancel_pro(user_id: str) -> dict | None:
    """Self-serve cancel. Two cases:

    - Has a real prepaid period (pro_expires_at set, the normal path
      going forward): just stops it being treated as auto-renewing.
      No refund, no early cutoff -- access runs out naturally at
      pro_expires_at (see the lazy expiry check in get_usage()).
    - Grandfathered paid account from before this feature existed
      (tier='paid', pro_expires_at NULL -- e.g. an old one-time
      payment or an admin grant): there's no prepaid window to let
      run out, so cancelling here downgrades to free immediately
      instead of silently doing nothing.

    Returns the updated row, or None if the user isn't paid at all."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE users SET
                    pro_auto_renew = CASE WHEN pro_expires_at IS NOT NULL THEN FALSE ELSE pro_auto_renew END,
                    tier = CASE WHEN pro_expires_at IS NULL THEN 'free' ELSE tier END,
                    pro_plan = CASE WHEN pro_expires_at IS NULL THEN NULL ELSE pro_plan END
                WHERE id = %s AND tier = 'paid'
                RETURNING *
                """,
                (user_id,),
            )
            row = cur.fetchone()
            return _row_to_usage(row) if row else None
