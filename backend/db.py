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
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS problems (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    difficulty TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    tags JSONB NOT NULL DEFAULT '[]',
                    description TEXT NOT NULL,
                    schema_sql TEXT NOT NULL,
                    seed_sql TEXT NOT NULL,
                    canonical_sql TEXT NOT NULL,
                    order_matters BOOLEAN NOT NULL DEFAULT FALSE,
                    status TEXT NOT NULL DEFAULT 'live',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
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


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
