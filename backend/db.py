"""
Postgres persistence for in-progress interview sessions.

Interviews need to survive things a plain in-memory dict can't: a browser
tab crashing, a transient LLM failure, or the backend itself restarting
(Render's free tier spins down after 15 min idle, wiping in-memory state).
This module makes Postgres the source of truth for session state so a
candidate can resume exactly where they left off, up until the interview
actually ends -- after that only the feedback report matters, so there's
no need for anything fancier than this.
"""

import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set -- interview sessions require Postgres "
                "for resumability. Configure it via render.yaml's database "
                "binding (or a local Postgres URL for dev)."
            )
        _pool = psycopg2.pool.SimpleConnectionPool(1, 5, DATABASE_URL)
    return _pool


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def init_schema():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS interview_sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    resume_text TEXT,
                    skip_intro BOOLEAN NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    started_at DOUBLE PRECISION NOT NULL,
                    topics_covered JSONB NOT NULL DEFAULT '[]',
                    conversation JSONB NOT NULL DEFAULT '[]',
                    current_topic TEXT,
                    current_topic_turns INTEGER NOT NULL DEFAULT 0,
                    last_table_context JSONB,
                    ended BOOLEAN NOT NULL DEFAULT FALSE,
                    feedback JSONB,
                    persona TEXT NOT NULL DEFAULT 'neutral',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            # Migration: `interview_sessions` already existed in prod before
            # persona was added.
            cur.execute("""
                ALTER TABLE interview_sessions ADD COLUMN IF NOT EXISTS persona TEXT NOT NULL DEFAULT 'neutral'
            """)
            # Migration: role-based interviewing replaced the old fixed
            # SQL-only topic list -- `mode` (personalized/generic) stays in
            # place, unused, rather than a destructive drop; target_role and
            # the once-computed candidate_profile are the real replacements.
            cur.execute("""
                ALTER TABLE interview_sessions ADD COLUMN IF NOT EXISTS target_role TEXT
            """)
            cur.execute("""
                ALTER TABLE interview_sessions ADD COLUMN IF NOT EXISTS candidate_profile JSONB
            """)
            # Migration: one-hint-per-topic deterministic cap (Phase 2 of
            # the mock interview rebuild) -- same "don't trust the model to
            # self-limit" precedent as the topic-turn budget above.
            cur.execute("""
                ALTER TABLE interview_sessions ADD COLUMN IF NOT EXISTS hint_used_this_topic BOOLEAN NOT NULL DEFAULT FALSE
            """)
            # Migration: connection-issue detection (Phase 3) -- counts
            # consecutive STT/LLM failures within a session so the
            # interviewer can proactively offer to retry/pause/end after
            # repeated trouble, rather than just erroring on every attempt.
            cur.execute("""
                ALTER TABLE interview_sessions ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER NOT NULL DEFAULT 0
            """)
            # Migration: lets the opening monologue ask a returning candidate
            # (one with real interview_topic_history) whether to focus this
            # session on past weak areas or start fresh, rather than always
            # silently assuming history should be used. True only between
            # the monologue being spoken and the candidate's reply to that
            # specific question; api_interview_answer clears it right after
            # classifying that reply.
            cur.execute("""
                ALTER TABLE interview_sessions ADD COLUMN IF NOT EXISTS awaiting_history_pref BOOLEAN NOT NULL DEFAULT FALSE
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS problems (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    difficulty TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    tags JSONB NOT NULL DEFAULT '[]',
                    description TEXT NOT NULL,
                    schema_sql TEXT,
                    seed_sql TEXT,
                    canonical_sql TEXT,
                    order_matters BOOLEAN NOT NULL DEFAULT FALSE,
                    status TEXT NOT NULL DEFAULT 'live',
                    is_free BOOLEAN NOT NULL DEFAULT FALSE,
                    track TEXT NOT NULL DEFAULT 'sql',
                    starter_code TEXT,
                    function_signature TEXT,
                    test_code TEXT,
                    canonical_solution TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            # Migration: `problems` already existed in prod before is_free was
            # added -- CREATE TABLE IF NOT EXISTS above won't add a column to
            # an existing table, so do it explicitly (idempotent, no-ops if
            # the column's already there).
            cur.execute("""
                ALTER TABLE problems ADD COLUMN IF NOT EXISTS is_free BOOLEAN NOT NULL DEFAULT FALSE
            """)
            # Migration: adds the Python practice track alongside the
            # original SQL-only bank. `track` discriminates rows ('sql' vs
            # 'python'); existing rows implicitly become 'sql'. The four
            # Python-only columns stay nullable and unused on SQL rows,
            # mirroring schema_sql/seed_sql/canonical_sql being unused on
            # (future) non-SQL rows -- a single shared table extended with
            # nullable columns, not a parallel table, since the bulk of the
            # fields (id/title/difficulty/topic/tags/description/status/
            # is_free/created_at) apply identically to both tracks.
            cur.execute("""
                ALTER TABLE problems ADD COLUMN IF NOT EXISTS track TEXT NOT NULL DEFAULT 'sql'
            """)
            cur.execute("""
                ALTER TABLE problems ADD COLUMN IF NOT EXISTS starter_code TEXT
            """)
            cur.execute("""
                ALTER TABLE problems ADD COLUMN IF NOT EXISTS function_signature TEXT
            """)
            cur.execute("""
                ALTER TABLE problems ADD COLUMN IF NOT EXISTS test_code TEXT
            """)
            cur.execute("""
                ALTER TABLE problems ADD COLUMN IF NOT EXISTS canonical_solution TEXT
            """)
            # Sample input/output shown to students on the problem page --
            # for SQL, {columns, rows} computed from canonical_sql against
            # the real seed data; for Python, a list of real (args, result)
            # pairs captured by actually running test_code with the target
            # function instrumented (see pysandbox.extract_examples). Never
            # separately hand-written, so it can't drift from the truth.
            cur.execute("""
                ALTER TABLE problems ADD COLUMN IF NOT EXISTS examples JSONB
            """)
            # SQL-specific columns are meaningless for track='python' rows --
            # relax NOT NULL (idempotent; no-ops once already dropped) so a
            # Python problem insert doesn't need to fake empty-string SQL.
            cur.execute("""ALTER TABLE problems ALTER COLUMN schema_sql DROP NOT NULL""")
            cur.execute("""ALTER TABLE problems ALTER COLUMN seed_sql DROP NOT NULL""")
            cur.execute("""ALTER TABLE problems ALTER COLUMN canonical_sql DROP NOT NULL""")
            # Business Case track (track='case') -- open-ended analytical-
            # reasoning questions with no single verifiable answer, graded
            # by an AI rubric judge (llm.case_feedback) rather than
            # execution. Same shared-table-with-nullable-columns pattern as
            # the Python columns above: case_prompt is the scenario shown
            # to students, case_context is optional supporting data,
            # rubric_points is what a strong answer should hit (JSONB list
            # of strings), sample_strong_answer is internal-only -- used to
            # self-validate a draft's rubric at generation time, never
            # shown to students.
            cur.execute("""ALTER TABLE problems ADD COLUMN IF NOT EXISTS case_prompt TEXT""")
            cur.execute("""ALTER TABLE problems ADD COLUMN IF NOT EXISTS case_context TEXT""")
            cur.execute("""ALTER TABLE problems ADD COLUMN IF NOT EXISTS rubric_points JSONB""")
            cur.execute("""ALTER TABLE problems ADD COLUMN IF NOT EXISTS sample_strong_answer TEXT""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS content_cadence (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    last_batch_generated_at TIMESTAMPTZ,
                    CONSTRAINT single_row CHECK (id = 1)
                )
            """)
            cur.execute("""
                INSERT INTO content_cadence (id, last_batch_generated_at)
                VALUES (1, NULL)
                ON CONFLICT (id) DO NOTHING
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS submissions (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    problem_id TEXT NOT NULL,
                    correct BOOLEAN NOT NULL,
                    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_submissions_user_problem
                ON submissions (user_id, problem_id)
            """)
            # Migration: the actual submitted code/answer was never stored,
            # only correct/timestamp -- added so a user can look back at
            # what they wrote on a past attempt at a given problem, not
            # just whether it passed. NULL on every pre-existing row (the
            # text is simply gone for those), populated going forward.
            cur.execute("""
                ALTER TABLE submissions ADD COLUMN IF NOT EXISTS query_text TEXT
            """)
            # Migration: the grading result itself (the SQL/Python error or
            # diff shown at submit time, or the Case track's overall_summary)
            # was discarded after the response was sent -- a user looking
            # back at a failed attempt in submission history could see THAT
            # it failed and the code they wrote, but not WHY. NULL on
            # pre-existing rows and on any correct SQL/Python submission
            # (nothing to explain there); Case submissions get a summary
            # either way since that track always produces one.
            cur.execute("""
                ALTER TABLE submissions ADD COLUMN IF NOT EXISTS result_text TEXT
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT,
                    tier TEXT NOT NULL DEFAULT 'free',
                    usage_date DATE,
                    submissions_today INTEGER NOT NULL DEFAULT 0,
                    explanations_today INTEGER NOT NULL DEFAULT 0,
                    interview_trial_used BOOLEAN NOT NULL DEFAULT FALSE,
                    interview_month TEXT,
                    interviews_this_month INTEGER NOT NULL DEFAULT 0,
                    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            # Migration: `users` already existed in prod before
            # interview_trial_used / interview_month / interviews_this_month
            # were added.
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS interview_trial_used BOOLEAN NOT NULL DEFAULT FALSE
            """)
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS interview_month TEXT
            """)
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS interviews_this_month INTEGER NOT NULL DEFAULT 0
            """)
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE
            """)
            # Migration: prepaid Pro access window (monthly/yearly), added
            # when self-serve cancel + a yearly plan were introduced.
            # There's no real recurring auto-debit -- a payment just buys
            # `paid` access until pro_expires_at, and cancelling only stops
            # it from being renewed, it doesn't revoke access early.
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_plan TEXT
            """)
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_expires_at TIMESTAMPTZ
            """)
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_auto_renew BOOLEAN NOT NULL DEFAULT TRUE
            """)
            # Migration: resume moved from per-interview-session-only
            # (interview_sessions.resume_text, re-uploaded every time) to a
            # persistent per-account asset -- upload once, every future
            # interview reuses it automatically until updated or deleted.
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS resume_text TEXT
            """)
            # Migration: username/full_name, captured the same way email is
            # (see users.record_profile_info) -- best-effort from the
            # signed-in Clerk profile, for admin lookups/support that
            # shouldn't depend on an anonymous account id alone.
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT
            """)
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT
            """)
            # Additional, hidden, adversarially-constructed seed datasets for
            # SQL problems -- problems.seed_sql stays test case #1 (the one
            # shown to students); each row here is one more dataset a
            # student's query must also pass on Submit. A problem with zero
            # rows here grades exactly as before this table existed, so the
            # backfill across the existing bank can proceed incrementally.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS problem_test_cases (
                    id SERIAL PRIMARY KEY,
                    problem_id TEXT NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
                    seed_sql TEXT NOT NULL,
                    defeats_wrong_query TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_problem_test_cases_problem_id
                    ON problem_test_cases (problem_id)
            """)
            # Cross-interview memory (Phase 4 of the mock interview rebuild)
            # -- one row per topic per completed interview, so a future
            # interview's profile analysis and feedback can reference real
            # growth/dips over time instead of only the current transcript.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS interview_topic_history (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES interview_sessions(session_id) ON DELETE CASCADE,
                    topic TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_interview_topic_history_user_topic
                    ON interview_topic_history (user_id, topic, recorded_at)
            """)
            # General site-activity log -- separate from `submissions`
            # (already the full per-problem-attempt record, joined against
            # `problems` for track/correctness in the admin views) and from
            # interview_sessions/interview_topic_history (already the full
            # interview record). This table is for everything else worth
            # showing on a per-user activity timeline: sign-ins, track
            # switches, interview start/end, resume actions, plan changes --
            # see users.record_activity()/get_activity().
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_activity_events (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    metadata JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_activity_events_user_created
                    ON user_activity_events (user_id, created_at)
            """)
            # Inbuilt support ticketing -- there was previously no way at
            # all for a user to reach the team from within the product.
            # email is always captured (even for a signed-in user, whose
            # `user_id` alone isn't something a human can reply to) so a
            # ticket is always actionable regardless of sign-in state.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    email TEXT,
                    subject TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    resolved_at TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_support_tickets_status_created
                    ON support_tickets (status, created_at)
            """)
            # One row per admin reply actually emailed out for a ticket --
            # separate from support_tickets itself so a ticket can have a
            # real back-and-forth thread, not just a single response.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ticket_replies (
                    id SERIAL PRIMARY KEY,
                    ticket_id INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
                    message TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_ticket_replies_ticket
                    ON ticket_replies (ticket_id, created_at)
            """)


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
