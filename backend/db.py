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
            # SQL-specific columns are meaningless for track='python' rows --
            # relax NOT NULL (idempotent; no-ops once already dropped) so a
            # Python problem insert doesn't need to fake empty-string SQL.
            cur.execute("""ALTER TABLE problems ALTER COLUMN schema_sql DROP NOT NULL""")
            cur.execute("""ALTER TABLE problems ALTER COLUMN seed_sql DROP NOT NULL""")
            cur.execute("""ALTER TABLE problems ALTER COLUMN canonical_sql DROP NOT NULL""")
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


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
