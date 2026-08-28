"""
FastAPI backend for the SQL practice MVP.

Run with:
    uvicorn main:app --reload --port 8000

Env vars (see llm.py):
    LLM_API_BASE, LLM_API_KEY, LLM_MODEL

Tiering (Postgres-backed, see users.py):
    - Every request carries an `X-User-Id` header (frontend generates a
      random one and stores it in localStorage) as a fallback identity.
      If it also carries `Authorization: Bearer <Clerk session token>`,
      that's verified (see auth.py) and used instead -- signing in isn't
      mandatory for practice mode, only for anything tied to a real
      identity (payments).
    - Free tier: FREE_DAILY_SUBMISSIONS submissions/day, plus only the
      curated free-tier problem subset. No AI help ("Ask Phoenix" is a
      Pro-only feature -- see /api/ask-phoenix).
    - Paid tier: unlimited submissions + Ask Phoenix + the full problem
      bank + mock interviews. Upgrading goes through Razorpay (payments.py):
      POST /api/payments/create-order then POST /api/payments/verify.
"""

import hmac
import os
import re
from urllib.parse import unquote

from fastapi import FastAPI, Header, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import duckdb

import problems as problems_module
from problems import get_problem, list_problems_summary
import sandbox
import llm
import interview
import resume_parser
import stt
import tts
import db
import topics
import role_topics
import support
import email_sender
import py_topics
import stats_topics
import data_lib_topics
import case_topics
import pysandbox
import auth
import users as users_module
import payments
import dashboard

app = FastAPI(title="SQL Practice MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FREE_DAILY_SUBMISSIONS = 20
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# How many of a SQL problem's datasets the free/fast "Run" action checks --
# a quick iteration signal, not the real judgment. Submit always checks
# every dataset the problem has (see _grade_sql_submission).
SQL_RUN_TEST_CASE_COUNT = 2

# Precompute expected output for every problem once at startup so grading
# doesn't re-run the canonical query on every submission. Each entry is a
# LIST of (columns, rows) tuples -- index 0 is the primary/visible
# seed_sql's expected output, indices 1+ are each hidden
# problem_test_cases dataset's own expected output (see
# problems.get_test_case_seeds). A problem not yet backfilled with hidden
# datasets simply has a one-element list, which grades exactly as the
# single-dataset model did before hidden test cases existed.
_EXPECTED_CACHE = {}


def _compute_expected_cache_entry(p: dict) -> list[tuple]:
    entry = [sandbox.compute_expected_output(p)[:2]]
    for seed_sql in problems_module.get_test_case_seeds(p["id"]):
        case_problem = {"schema_sql": p["schema_sql"], "seed_sql": seed_sql, "canonical_sql": p["canonical_sql"]}
        entry.append(sandbox.compute_expected_output(case_problem)[:2])
    return entry


@app.on_event("startup")
def _startup():
    if db.DATABASE_URL:
        db.init_schema()
        problems_module.seed_if_empty()
        problems_module.mark_free_problems()
        for p in problems_module.list_all_live_problems():
            if p.get("track", "sql") != "sql":
                # Python submissions are graded live against test_code via
                # pysandbox on every submit, not diffed against a cached
                # expected output -- nothing to precompute here.
                continue
            _EXPECTED_CACHE[p["id"]] = _compute_expected_cache_entry(p)


class SubmitRequest(BaseModel):
    problem_id: str
    query: str


ACTIVITY_EVENT_TYPES = {"viewed_sql_track", "viewed_python_track", "viewed_case_track", "viewed_mock_interview"}


class LogActivityRequest(BaseModel):
    event_type: str
    metadata: dict | None = None


MAX_SUPPORT_SUBJECT_LEN = 200
MAX_SUPPORT_MESSAGE_LEN = 5000


MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024  # 8 MB -- generous for a screenshot, not for a video


class AskPhoenixRequest(BaseModel):
    problem_id: str
    current_query: str | None = None
    conversation: list[dict] = []
    question: str


class AskPhoenixTopicRequest(BaseModel):
    track: str
    topic: str
    conversation: list[dict] = []
    question: str


class InterviewStartRequest(BaseModel):
    target_role: str  # one of role_topics.ROLES
    resume_text: str | None = None  # optional override for this interview; also (re-)saves to the account, same as parse-resume
    skip_intro: bool = False
    duration_minutes: int = 45
    persona: str = "neutral"  # "friendly" | "neutral" | "strict"


class InterviewAnswerRequest(BaseModel):
    session_id: str
    answer_text: str


class InterviewEndRequest(BaseModel):
    session_id: str


class InterviewTtsRequest(BaseModel):
    text: str


@app.get("/api/topics")
def api_topics():
    """Exposes the topic taxonomy so the frontend doesn't have to hand-
    duplicate topics.py's lists as a drift-prone JS array. `gradeable`/
    `all` stay SQL-only and unchanged -- interview-feedback "topics to
    study" pills link into the SQL practice bank specifically, since
    Mock Interview stays SQL-only. `python`, `stats`, and `data_lib` are
    additive: the Python-track equivalents (general Cookbook topics,
    the cross-track statistics topic lens, and the pandas/numpy topic
    lens), for any consumer that needs the full taxonomy rather than
    just whatever happens to be live right now."""
    return {
        "gradeable": topics.GRADEABLE_TOPICS,
        "all": topics.ALL_TOPICS,
        "python": py_topics.PY_GRADEABLE_TOPICS,
        "stats": stats_topics.STATS_TOPICS,
        "data_lib": data_lib_topics.DATA_LIBRARY_TOPICS,
        "case_da": case_topics.CASE_DA_TOPICS,
        "case_de": case_topics.CASE_DE_TOPICS,
    }


@app.get("/api/problems")
def api_list_problems(
    difficulty: str | None = None,
    tag: str | None = None,
    topic: str | None = None,
    track: str | None = None,
    x_user_id: str = Header(default=None),
    authorization: str | None = Header(default=None),
):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    tier = users_module.get_usage(user_id)["tier"]
    problems = list_problems_summary(difficulty=difficulty, tag=tag, topic=topic, user_id=user_id, track=track)
    for p in problems:
        p["locked"] = not p["is_free"] and tier != "paid"
    return {"problems": problems}


@app.get("/api/problems/{problem_id}")
def api_get_problem(problem_id: str, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    p = get_problem(problem_id)
    if not p:
        raise HTTPException(404, "Problem not found")

    user_id = auth.resolve_user_id(authorization, x_user_id)
    tier = users_module.get_usage(user_id)["tier"]
    if not p["is_free"] and tier != "paid":
        raise HTTPException(
            402,
            "This problem is part of the Pro problem bank (₹199/mo). "
            "Free tier includes a curated sample -- upgrade to unlock all problems.",
        )

    if p.get("track") == "python":
        return {
            "id": p["id"],
            "title": p["title"],
            "difficulty": p["difficulty"],
            "topic": p["topic"],
            "tags": p["tags"],
            "description": p["description"],
            "track": "python",
            "starter_code": p["starter_code"],
            "examples": p.get("examples") or [],
        }

    if p.get("track") == "case":
        return {
            "id": p["id"],
            "title": p["title"],
            "difficulty": p["difficulty"],
            "topic": p["topic"],
            "tags": p["tags"],
            "track": "case",
            "case_prompt": p["case_prompt"],
            "case_context": p.get("case_context"),
        }

    sample_tables = _build_sample_tables(p["schema_sql"], p["seed_sql"])

    return {
        "id": p["id"],
        "title": p["title"],
        "difficulty": p["difficulty"],
        "topic": p["topic"],
        "tags": p["tags"],
        "description": p["description"],
        "track": "sql",
        "schema_sql": p["schema_sql"].strip(),
        "sample_tables": sample_tables,
        "examples": p.get("examples"),
    }


@app.get("/api/usage")
def api_usage(
    x_user_id: str = Header(default=None),
    authorization: str | None = Header(default=None),
    x_user_email: str | None = Header(default=None),
    x_user_username: str | None = Header(default=None),
    x_user_full_name: str | None = Header(default=None),
):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    if x_user_email or x_user_username or x_user_full_name:
        # Percent-decoded: the frontend encodes these since a full name can
        # contain non-ASCII characters raw HTTP headers can't carry.
        users_module.record_profile_info(
            user_id,
            email=unquote(x_user_email) if x_user_email else None,
            username=unquote(x_user_username) if x_user_username else None,
            full_name=unquote(x_user_full_name) if x_user_full_name else None,
        )
    u = users_module.get_usage(user_id)
    return {
        "user_id": user_id,
        "tier": u["tier"],
        "submissions_today": u["submissions"],
        "free_daily_submissions": FREE_DAILY_SUBMISSIONS,
        "interview_trial_used": u["interview_trial_used"],
        "interviews_this_month": u["interviews_this_month"],
        "max_interviews_per_month": MAX_INTERVIEWS_PER_MONTH,
        "is_admin": users_module.is_admin(user_id),
        "pro_plan": u["pro_plan"],
        "pro_expires_at": u["pro_expires_at"],
        "pro_auto_renew": u["pro_auto_renew"],
        "has_resume": u["has_resume"],
    }


@app.post("/api/activity")
def api_log_activity(req: LogActivityRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """
    Client-driven half of the site-activity log (see users.record_activity)
    -- for browsing/navigation signals the backend has no other way to
    observe (which track a candidate opened, whether they looked at Mock
    Interview at all), as opposed to the server-side calls at points like
    interview start/end or resume upload where the backend already knows
    an event happened.

    event_type is restricted to a fixed whitelist rather than trusting
    whatever the client sends -- same "never trust client input, validate
    against a known vocabulary" pattern as target_role/persona elsewhere in
    this file, so this endpoint can only ever append one of a few known,
    reviewed event shapes to a real user's own row, never arbitrary data.
    """
    if req.event_type not in ACTIVITY_EVENT_TYPES:
        raise HTTPException(400, f"event_type must be one of {sorted(ACTIVITY_EVENT_TYPES)}")
    user_id = auth.resolve_user_id(authorization, x_user_id)
    users_module.record_activity(user_id, req.event_type, req.metadata)
    return {"logged": True}


@app.post("/api/support/tickets")
async def api_create_support_ticket(
    subject: str = Form(...), message: str = Form(...), email: str = Form(...),
    attachment: UploadFile | None = File(default=None),
    x_user_id: str = Header(default=None), authorization: str | None = Header(default=None),
):
    """
    Inbuilt support ticketing -- previously there was no way at all for a
    user to reach the team from within the product. Deliberately open to
    anonymous callers too (support access shouldn't depend on being signed
    in, especially since sign-in itself is one of the things that could be
    broken); user_id is whatever auth.resolve_user_id resolves to, for
    context, but email is what actually makes a ticket actionable.

    multipart/form-data rather than a JSON body, purely to carry the
    optional attachment alongside the text fields -- same reasoning as
    every other file-upload endpoint in this file.
    """
    subject = subject.strip()
    message = message.strip()
    email = email.strip()
    if not subject or not message or not email:
        raise HTTPException(400, "subject, message, and email are all required.")
    if len(subject) > MAX_SUPPORT_SUBJECT_LEN:
        raise HTTPException(413, f"Subject too long -- {MAX_SUPPORT_SUBJECT_LEN} characters max.")
    if len(message) > MAX_SUPPORT_MESSAGE_LEN:
        raise HTTPException(413, f"Message too long -- {MAX_SUPPORT_MESSAGE_LEN} characters max.")

    attachment_filename = attachment_content_type = attachment_data = None
    if attachment is not None and attachment.filename:
        attachment_data = await attachment.read(MAX_ATTACHMENT_BYTES + 1)
        if len(attachment_data) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, f"Attachment too large -- {MAX_ATTACHMENT_BYTES // (1024*1024)} MB max.")
        attachment_filename = attachment.filename
        attachment_content_type = attachment.content_type

    user_id = auth.resolve_user_id(authorization, x_user_id)
    ticket = support.create_ticket(
        user_id, email, subject, message,
        attachment_filename=attachment_filename, attachment_content_type=attachment_content_type,
        attachment_data=attachment_data,
    )
    return {"ticket": ticket}


@app.get("/api/admin/tickets")
def api_admin_list_tickets(request: Request, status: str | None = None):
    """Support ticket queue for the admin portal. `status` ("open"/
    "resolved") filters to just that state; omitted, returns everything."""
    _require_admin(request)
    if status and status not in ("open", "resolved"):
        raise HTTPException(400, "status must be 'open' or 'resolved'.")
    return {"tickets": support.list_tickets(status)}


class SetTicketStatusRequest(BaseModel):
    status: str


@app.post("/api/admin/tickets/{ticket_id}/status")
def api_admin_set_ticket_status(ticket_id: int, req: SetTicketStatusRequest, request: Request):
    _require_admin(request)
    if req.status not in ("open", "resolved"):
        raise HTTPException(400, "status must be 'open' or 'resolved'.")
    ticket = support.set_ticket_status(ticket_id, req.status)
    if not ticket:
        raise HTTPException(404, "Ticket not found.")
    return {"ticket": ticket}


@app.post("/api/admin/tickets/{ticket_id}/reply")
async def api_admin_reply_to_ticket(
    ticket_id: int, request: Request,
    message: str = Form(...), attachment: UploadFile | None = File(default=None),
):
    """Sends an actual email to the ticket's submitter and records it in
    the thread -- the whole point of inbuilt ticketing over the earlier
    mailto:-link-only version, which only ever opened the admin's OWN mail
    client rather than sending anything from the server. Only recorded
    (with its attachment, if any) once the email actually sends."""
    _require_admin(request)
    message = message.strip()
    if not message:
        raise HTTPException(400, "message is required.")
    ticket = support.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found.")
    if not ticket.get("email"):
        raise HTTPException(400, "This ticket has no email address to reply to.")

    attachment_filename = attachment_content_type = attachment_data = None
    if attachment is not None and attachment.filename:
        attachment_data = await attachment.read(MAX_ATTACHMENT_BYTES + 1)
        if len(attachment_data) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, f"Attachment too large -- {MAX_ATTACHMENT_BYTES // (1024*1024)} MB max.")
        attachment_filename = attachment.filename
        attachment_content_type = attachment.content_type

    try:
        email_sender.send_email(
            to=ticket["email"],
            subject=f"Re: {ticket['subject']}",
            body_text=message,
            attachment={"filename": attachment_filename, "data": attachment_data} if attachment_data else None,
        )
    except RuntimeError as e:
        raise HTTPException(502, f"Couldn't send the reply email ({e}).")

    reply = support.add_reply(
        ticket_id, message,
        attachment_filename=attachment_filename, attachment_content_type=attachment_content_type,
        attachment_data=attachment_data,
    )
    return {"reply": reply}


@app.get("/api/admin/tickets/{ticket_id}/attachment")
def api_admin_get_ticket_attachment(ticket_id: int, request: Request):
    _require_admin(request)
    attachment = support.get_ticket_attachment(ticket_id)
    if not attachment:
        raise HTTPException(404, "This ticket has no attachment.")
    return Response(
        content=attachment["data"],
        media_type=attachment["content_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{attachment["filename"]}"'},
    )


@app.get("/api/admin/tickets/{ticket_id}/replies/{reply_id}/attachment")
def api_admin_get_reply_attachment(ticket_id: int, reply_id: int, request: Request):
    _require_admin(request)
    attachment = support.get_reply_attachment(reply_id)
    if not attachment:
        raise HTTPException(404, "This reply has no attachment.")
    return Response(
        content=attachment["data"],
        media_type=attachment["content_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{attachment["filename"]}"'},
    )


@app.get("/api/progress")
def api_progress(x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    progress = problems_module.get_user_progress(user_id)
    progress["mock_interviews"] = users_module.get_usage(user_id)["interviews_this_month"]
    return progress


@app.delete("/api/submissions")
def api_reset_submissions(x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """Wipes all of the requesting user's submission history (solved
    status resets to nothing). Irreversible -- the frontend confirms with
    the user before calling this."""
    user_id = auth.resolve_user_id(authorization, x_user_id)
    deleted = problems_module.reset_user_submissions(user_id)
    return {"deleted": deleted}


class MergeProgressRequest(BaseModel):
    anonymous_user_id: str


@app.post("/api/merge-progress")
def api_merge_progress(req: MergeProgressRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """Folds progress made anonymously (before signing in) into the real
    account. The frontend calls this right after detecting a fresh
    sign-in, passing the browser's old anonymous X-User-Id -- that id
    never changes once assigned, so it's the same one whatever solving
    happened under before an account existed."""
    user_id = auth.resolve_user_id(authorization, x_user_id)
    if not user_id.startswith("clerk:"):
        raise HTTPException(401, "Sign in first.")
    if not req.anonymous_user_id or req.anonymous_user_id == user_id:
        return {"merged_submissions": 0}
    merged = problems_module.merge_user_progress(req.anonymous_user_id, user_id)
    return {"merged_submissions": merged}


class CreateOrderRequest(BaseModel):
    plan: str = "monthly"  # "monthly" | "yearly"


class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@app.post("/api/payments/create-order")
def api_payments_create_order(req: CreateOrderRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    if not user_id.startswith("clerk:"):
        raise HTTPException(401, "Sign in before upgrading -- Pro is tied to your account, not an anonymous browser id.")
    if req.plan not in payments.PLAN_PRICES_PAISE:
        raise HTTPException(400, "plan must be 'monthly' or 'yearly'")
    try:
        return payments.create_order(user_id, req.plan)
    except payments.PaymentsNotConfigured as e:
        raise HTTPException(503, str(e))


@app.post("/api/payments/verify")
def api_payments_verify(req: VerifyPaymentRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    if not user_id.startswith("clerk:"):
        raise HTTPException(401, "Sign in before upgrading -- Pro is tied to your account, not an anonymous browser id.")
    try:
        ok = payments.verify_payment_signature(req.razorpay_order_id, req.razorpay_payment_id, req.razorpay_signature)
    except payments.PaymentsNotConfigured as e:
        raise HTTPException(503, str(e))
    if not ok:
        raise HTTPException(400, "Payment signature verification failed.")

    # Read the plan back from Razorpay's own order record rather than
    # trusting a client-supplied value -- the order's notes are the
    # authoritative account of what was actually paid for.
    try:
        order = payments.get_order(req.razorpay_order_id)
    except payments.PaymentsNotConfigured as e:
        raise HTTPException(503, str(e))
    plan = (order.get("notes") or {}).get("plan", "monthly")
    if plan not in payments.PLAN_PRICES_PAISE:
        plan = "monthly"

    users_module.set_pro_period(user_id, plan)
    users_module.record_activity(user_id, "upgraded_to_pro", {"plan": plan})
    return {"user_id": user_id, "tier": "paid", "plan": plan}


@app.post("/api/payments/cancel")
def api_payments_cancel(x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """Self-serve cancel. No refund, no early cutoff -- Pro access simply
    runs out at the end of the period already paid for (pro_expires_at),
    instead of being revoked immediately."""
    user_id = auth.resolve_user_id(authorization, x_user_id)
    if not user_id.startswith("clerk:"):
        raise HTTPException(401, "Sign in first.")
    updated = users_module.cancel_pro(user_id)
    if updated is None:
        raise HTTPException(400, "No active Pro subscription to cancel.")
    users_module.record_activity(user_id, "cancelled_pro")
    return updated


def _preview(columns, rows, limit=10):
    return {"columns": columns, "rows": rows[:limit]}


def _build_sample_tables(schema_sql: str, seed_sql: str) -> dict:
    """
    Seeds a throwaway in-memory DuckDB from `schema_sql`+`seed_sql` and
    dumps every resulting table's columns/first-15-rows -- the same shape
    the problem page's "Sample Data" panel renders. Factored out of
    api_get_problem so _grade_sql_submission can build the identical
    preview for whichever hidden test-case dataset a submission actually
    failed against, not just the always-visible seed_sql one.
    """
    con = duckdb.connect(":memory:", config={"enable_external_access": False})
    try:
        con.execute(schema_sql)
        con.execute(seed_sql)
        table_names = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
        sample_tables = {}
        for t in table_names:
            quoted = '"' + t.replace('"', '""') + '"'
            cols = [d[0] for d in con.execute(f"SELECT * FROM {quoted} LIMIT 0").description]
            rows = con.execute(f"SELECT * FROM {quoted} LIMIT 15").fetchall()
            sample_tables[t] = {
                "columns": cols,
                "rows": [[sandbox._normalize_cell(v) for v in r] for r in rows],
            }
        return sample_tables
    finally:
        con.close()


@app.post("/api/submit")
def api_submit(req: SubmitRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    u = users_module.get_usage(user_id)

    if u["submissions"] >= FREE_DAILY_SUBMISSIONS and u["tier"] == "free":
        raise HTTPException(
            429,
            f"Daily free submission limit ({FREE_DAILY_SUBMISSIONS}) reached. "
            "Upgrade to keep practicing today.",
        )

    problem = get_problem(req.problem_id)
    if not problem:
        raise HTTPException(404, "Problem not found")

    if not problem["is_free"] and u["tier"] != "paid":
        raise HTTPException(
            402,
            "This problem is part of the Pro problem bank (₹199/mo). "
            "Free tier includes a curated sample -- upgrade to unlock all problems.",
        )

    users_module.increment_submission(user_id)
    u["submissions"] += 1

    if problem.get("track") == "python":
        result = {"correct": False, "error": None, "output": None}
        try:
            graded = pysandbox.run_python_submission(
                student_code=req.query, test_code=problem["test_code"],
            )
        except RuntimeError as e:
            result["error"] = f"Grading temporarily unavailable ({e})."
        else:
            result["correct"] = graded["passed"]
            result["output"] = graded["output"]
            if not graded["passed"]:
                result["error"] = graded["error"]
        problems_module.record_submission(user_id, problem["id"], result["correct"], req.query, result.get("error"))
        return result

    result = _grade_sql_submission(problem, req.query)
    problems_module.record_submission(user_id, problem["id"], result["correct"], req.query, result.get("error"))
    return result


@app.get("/api/submissions/{problem_id}")
def api_my_submissions_for_problem(problem_id: str, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """A signed-in-or-anonymous user's own past attempts at one problem
    (query/code/answer text, pass/fail, when) -- self-service, not an
    admin endpoint: scoped to the caller's own resolved identity only, so
    this can never reveal another user's submissions."""
    user_id = auth.resolve_user_id(authorization, x_user_id)
    return {"submissions": problems_module.get_user_submissions_for_problem(user_id, problem_id)}


@app.get("/api/dashboard/progress")
def api_dashboard_progress(x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """
    The candidate progress dashboard: per-track solved/attempted totals,
    per-topic Strengths/Weaknesses (computed from this user's own
    submission history, topics a user dismissed excluded), and one
    suggested next topic to practice. See dashboard.py for the actual
    computation -- this endpoint is just auth + a thin wrapper.
    """
    user_id = auth.resolve_user_id(authorization, x_user_id)
    return dashboard.get_progress(user_id)


class DismissTopicRequest(BaseModel):
    track: str
    topic: str


@app.post("/api/dashboard/dismiss-topic")
def api_dashboard_dismiss_topic(req: DismissTopicRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """Hides one topic from the caller's own Strengths/Weaknesses lists --
    purely a display filter, never touches the underlying submissions or
    the real solved/attempted counts."""
    user_id = auth.resolve_user_id(authorization, x_user_id)
    dashboard.dismiss_topic(user_id, req.track, req.topic)
    return {"track": req.track, "topic": req.topic, "dismissed": True}


@app.post("/api/dashboard/restore-topic")
def api_dashboard_restore_topic(req: DismissTopicRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """Undoes a dismiss -- the topic can reappear in Strengths/Weaknesses
    on the next /api/dashboard/progress call if it still qualifies."""
    user_id = auth.resolve_user_id(authorization, x_user_id)
    dashboard.restore_topic(user_id, req.track, req.topic)
    return {"track": req.track, "topic": req.topic, "dismissed": False}


def _grade_sql_submission(problem: dict, query: str, num_cases: int | None = None) -> dict:
    """
    Runs `query` against `problem`'s primary seed_sql plus its hidden
    problem_test_cases datasets, sliced to the first `num_cases` of them
    if given (Run uses a short slice for a fast iteration signal; Submit
    passes None for the full set -- only Submit can mark a problem
    solved). A wrong-but-plausible query has to coincidentally agree with
    canonical_sql across every dataset checked, not just one fixed one.
    Returns the same result shape SQL submissions have always returned,
    reporting the first failing dataset's actual/expected preview.
    """
    all_seeds = [problem["seed_sql"]] + problems_module.get_test_case_seeds(problem["id"])
    expected_per_case = _EXPECTED_CACHE[problem["id"]]
    if num_cases is not None:
        all_seeds = all_seeds[:num_cases]
        expected_per_case = expected_per_case[:num_cases]

    result = {"correct": False, "error": None, "actual_preview": None, "expected_preview": None}
    try:
        outcome = sandbox.run_query_against_test_cases(problem, query, all_seeds, expected_per_case)
    except (sandbox.SqlValidationError, sandbox.SqlTimeoutError) as e:
        result["error"] = str(e)
        return result
    except duckdb.Error as e:
        result["error"] = f"SQL error: {e}"
        return result

    result["correct"] = outcome["correct"]
    result["actual_preview"] = _preview(
        outcome["columns"], [[sandbox._normalize_cell(v) for v in r] for r in outcome["rows"]]
    )
    if not outcome["correct"]:
        result["error"] = outcome["diff"]
        result["expected_preview"] = _preview(
            outcome["expected_columns"], [[sandbox._normalize_cell(v) for v in r] for r in outcome["expected_rows"]]
        )
        # failed_index 0 is problem["seed_sql"] -- the same dataset shown in
        # "Sample Data" on the problem page. Any later index is one of the
        # hidden problem_test_cases datasets, never shown anywhere in the
        # UI -- the frontend uses this to disclose that the Expected/Your
        # output tables below are computed against data the candidate has
        # never seen, rather than silently showing unfamiliar values with
        # no explanation (confirmed confusing: without this, a candidate
        # has no way to tell a hidden-dataset failure apart from a mistake
        # against the sample they can actually see).
        result["is_hidden_case"] = outcome["failed_index"] > 0
        # Beyond hidden-vs-sample, a candidate with several datasets running
        # (Submit especially) had no way to tell WHICH one broke, or how
        # many were even checked -- "it failed somewhere" with no sense of
        # scope. 1-indexed so it reads naturally ("case 2 of 5"), and
        # total_cases lets the frontend show that even a pass-so-far Run
        # only exercised a slice of the full Submit check.
        result["failed_case_number"] = outcome["failed_index"] + 1
        # The expected/actual *output* previews above aren't enough to
        # debug a hidden-case failure -- they're the query's result, which
        # may be filtered/aggregated/limited and hide the actual input
        # data shape entirely. User feedback: "they need to look at the
        # test case which failed", not just be told it failed. So also
        # hand back that failing case's own input tables (same shape as
        # the problem page's "Sample Data" panel) -- once a candidate has
        # already failed a case, seeing its raw data is a debugging aid,
        # not overfitting risk.
        result["failed_case_tables"] = _build_sample_tables(
            problem["schema_sql"], all_seeds[outcome["failed_index"]]
        )
    result["total_cases"] = len(all_seeds)
    return result


@app.post("/api/run")
def api_run(req: SubmitRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """
    Fast iteration check while writing a query -- SQL only for now (the
    thing this exists to let a student sanity-check is exactly the new
    multi-dataset grading; Python/Business-Case submissions already run
    their real grading path with no lighter-weight variant to offer).
    Deliberately does NOT call increment_submission or record_submission:
    Run never touches the daily quota and can never mark a problem
    solved, only Submit can -- see _grade_sql_submission's docstring.
    """
    user_id = auth.resolve_user_id(authorization, x_user_id)
    problem = get_problem(req.problem_id)
    if not problem:
        raise HTTPException(404, "Problem not found")
    if problem.get("track") != "sql":
        raise HTTPException(400, "Run is only available for SQL problems right now -- use Submit.")

    u = users_module.get_usage(user_id)
    if not problem["is_free"] and u["tier"] != "paid":
        raise HTTPException(
            402,
            "This problem is part of the Pro problem bank (₹199/mo). "
            "Free tier includes a curated sample -- upgrade to unlock all problems.",
        )

    return _grade_sql_submission(problem, req.query, num_cases=SQL_RUN_TEST_CASE_COUNT)


@app.post("/api/ask-phoenix")
def api_ask_phoenix(req: AskPhoenixRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """
    Open-ended contextual help about a problem -- Pro-only, unlimited (no
    daily quota, unlike the old free-tier explanation counters this
    replaces). Can be asked at any point while viewing a problem, not just
    after a wrong submission.
    """
    user_id = auth.resolve_user_id(authorization, x_user_id)
    u = users_module.get_usage(user_id)

    if u["tier"] != "paid":
        raise HTTPException(
            402,
            "Ask Phoenix is a Pro feature (₹199/mo) -- upgrade to get contextual AI help on every problem.",
        )

    problem = get_problem(req.problem_id)
    if not problem:
        raise HTTPException(404, "Problem not found")

    try:
        llm_result = llm.ask_phoenix(
            user_id=user_id,
            problem=problem,
            current_query=req.current_query,
            conversation=req.conversation,
            question=req.question,
        )
        return {"answer": llm_result["answer"], "llm_usage": llm_result["usage"]}
    except Exception as e:
        raise HTTPException(502, f"Ask Phoenix unavailable right now ({e}).")


@app.post("/api/ask-phoenix/topic")
def api_ask_phoenix_topic(req: AskPhoenixTopicRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """
    Ask Phoenix for a TOPIC concept, not a specific problem -- reached
    from the progress dashboard (a Strengths/Weaknesses row, or the
    suggested-next-topic card), where there's no problem_id/current_query
    to anchor on. Same Pro gate and unlimited-use shape as the per-problem
    /api/ask-phoenix; see llm.explain_topic for why the system prompt
    differs (nothing to protect, so it teaches the concept directly).
    """
    user_id = auth.resolve_user_id(authorization, x_user_id)
    u = users_module.get_usage(user_id)

    if u["tier"] != "paid":
        raise HTTPException(
            402,
            "Ask Phoenix is a Pro feature (₹199/mo) -- upgrade to get contextual AI help on any topic.",
        )

    try:
        llm_result = llm.explain_topic(
            user_id=user_id,
            track=req.track,
            topic=req.topic,
            conversation=req.conversation,
            question=req.question,
        )
        return {"answer": llm_result["answer"], "llm_usage": llm_result["usage"]}
    except Exception as e:
        raise HTTPException(502, f"Ask Phoenix unavailable right now ({e}).")


class CaseSubmitRequest(BaseModel):
    problem_id: str
    answer: str
    follow_up_question: str | None = None
    follow_up_answer: str | None = None


@app.post("/api/case/submit")
def api_case_submit(req: CaseSubmitRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """
    Business Case track submission -- rubric-graded by an AI judge
    (llm.case_feedback), not execution, since there's no single verifiable
    right answer. Same free/paid gating shape as SQL/Python (curated free
    sample + Pro-gated full bank, riding the same FREE_DAILY_SUBMISSIONS
    counter) -- deliberately NOT fully Pro-gated like Ask Phoenix/Mock
    Interview, since this track is meant to be the platform's headline
    differentiator.

    Two-pass flow: the first call (follow_up_question/follow_up_answer
    both omitted) may come back with status="follow_up_needed" instead of
    a score -- the frontend shows that question, collects one more
    free-text response, and calls again with follow_up_question (echoed
    back verbatim) and follow_up_answer filled in for final scoring. The
    daily submission counter only increments once scoring is actually
    final, so a follow-up round doesn't cost the student two submissions
    for one logical answer.
    """
    user_id = auth.resolve_user_id(authorization, x_user_id)
    u = users_module.get_usage(user_id)

    if u["submissions"] >= FREE_DAILY_SUBMISSIONS and u["tier"] == "free":
        raise HTTPException(
            429,
            f"Daily free submission limit ({FREE_DAILY_SUBMISSIONS}) reached. "
            "Upgrade to keep practicing today.",
        )

    problem = get_problem(req.problem_id)
    if not problem or problem.get("track") != "case":
        raise HTTPException(404, "Case problem not found")

    if not problem["is_free"] and u["tier"] != "paid":
        raise HTTPException(
            402,
            "This problem is part of the Pro problem bank (₹199/mo). "
            "Free tier includes a curated sample -- upgrade to unlock all problems.",
        )

    try:
        result = llm.case_feedback(
            user_id=user_id,
            problem=problem,
            answer=req.answer,
            follow_up_question=req.follow_up_question,
            follow_up_answer=req.follow_up_answer,
        )
    except Exception as e:
        raise HTTPException(502, f"Case feedback unavailable right now ({e}).")

    if result["status"] == "final":
        users_module.increment_submission(user_id)
        # No single boolean "correct" for a rubric-scored written answer --
        # reuse the existing submissions table (shared by the solved-by-
        # category charts and per-user history) with a pass threshold, so
        # this track shows up in the same admin/user views without a
        # parallel table just for its own history.
        problems_module.record_submission(user_id, problem["id"], (result.get("score") or 0) >= 70, req.answer, result.get("overall_summary"))

    return result


def intro_question(target_role: str) -> str:
    """The hardcoded (non-LLM) opening question, asked verbatim before
    skip_intro was a thing and still used whenever it's False. Was a bare
    constant assuming every role is SQL-heavy -- broke the moment a
    non-SQL role (Power Automate Developer) existed. Text is byte-for-byte
    identical to the old constant for every role that has real SQL
    topics, so this changes nothing for an already-QA'd role; only a
    role with no SQL component gets the generic phrasing."""
    has_sql = bool(role_topics.ROLE_TOPIC_MIX.get(target_role, {}).get("sql"))
    domain = "your experience working with SQL and databases" if has_sql else f"your experience relevant to a {target_role} role"
    return f"Let's get started. Could you briefly introduce yourself and walk me through {domain}?"


MAX_INTERVIEWS_PER_MONTH = 5  # Pro-tier cap -- interviews now also cost real STT + LLM-turn spend, not just the occasional Ask Phoenix call


def _require_paid_or_trial(u: dict) -> bool:
    """Returns True if this request is consuming the user's free interview
    trial (caller must then mark it used and cap the session's duration);
    False if they're paid (no trial needed); raises 402 if neither paid nor
    trial-eligible. Does NOT enforce the paid-tier monthly interview cap --
    that's a starting-a-new-session concern only, checked separately in
    api_interview_start so it can't retroactively block /answer or
    /parse-resume calls against a session that was already validly started
    under the cap (the count only changes once, at /start, not per turn)."""
    if u["tier"] == "paid":
        return False
    if not u["interview_trial_used"]:
        return True
    raise HTTPException(
        402,
        "You've used your free interview trial. Upgrade to Pro (₹199/mo) "
        f"for up to {MAX_INTERVIEWS_PER_MONTH} mock interviews a month.",
    )


MAX_RESUME_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB -- a resume has no business being bigger


@app.post("/api/interview/parse-resume")
async def api_parse_resume(file: UploadFile = File(...), x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    u = users_module.get_usage(user_id)
    if not users_module.is_admin(user_id):
        _require_paid_or_trial(u)  # trial consumption only happens at /start, not here

    file_bytes = await file.read(MAX_RESUME_UPLOAD_BYTES + 1)
    if len(file_bytes) > MAX_RESUME_UPLOAD_BYTES:
        raise HTTPException(413, "Resume file too large -- 5 MB max.")
    try:
        text = resume_parser.extract_text(file.filename, file_bytes)
    except resume_parser.UnsupportedResumeFormat as e:
        raise HTTPException(400, str(e))
    users_module.set_resume(user_id, text)  # persist to the account -- every future interview reuses it until updated/deleted
    users_module.record_activity(user_id, "resume_uploaded", {"chars": len(text)})
    return {"resume_text": text}


@app.delete("/api/interview/resume")
def api_delete_resume(x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    users_module.delete_resume(user_id)
    users_module.record_activity(user_id, "resume_deleted")
    return {"deleted": True}


@app.post("/api/interview/start")
def api_interview_start(req: InterviewStartRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    u = users_module.get_usage(user_id)
    is_unrestricted = users_module.is_admin(user_id)  # admins skip the trial gate, the monthly cap, and all usage bookkeeping below
    is_trial = False
    if not is_unrestricted:
        is_trial = _require_paid_or_trial(u)
        if not is_trial and u["interviews_this_month"] >= MAX_INTERVIEWS_PER_MONTH:
            raise HTTPException(
                402,
                f"You've used all {MAX_INTERVIEWS_PER_MONTH} mock interviews included this month. "
                "They reset at the start of next month.",
            )

    if req.target_role not in role_topics.ROLES:
        raise HTTPException(400, f"target_role must be one of {role_topics.ROLES}")
    if req.persona not in ("friendly", "neutral", "strict"):
        raise HTTPException(400, "persona must be 'friendly', 'neutral', or 'strict'")

    # An explicit resume_text here (re-)saves to the account, same as
    # parse-resume -- uploading/passing a resume is how "update" works, no
    # separate endpoint needed. Otherwise fall back to whatever's already
    # saved, so once uploaded a candidate never needs to re-upload.
    resume_text = req.resume_text
    if resume_text:
        users_module.set_resume(user_id, resume_text)
    else:
        resume_text = users_module.get_resume(user_id)

    topics_list = role_topics.topics_for_role(req.target_role)
    topic_history = interview.get_topic_history(user_id, topics=topics_list)
    has_history = bool(topic_history)
    # Deliberately history-agnostic here even when topic_history exists --
    # the candidate hasn't said yet whether they want it used this session
    # (see the ask_history_pref monologue addition below). If they confirm
    # "yes" in api_interview_answer's awaiting_history_pref branch, THAT's
    # where analyze_candidate_profile gets called again, this time with
    # topic_history, before the real first question is generated.
    profile = llm.analyze_candidate_profile(
        user_id=user_id, resume_text=resume_text, target_role=req.target_role, topic_history=None,
    )

    session = interview.create_session(
        user_id=user_id,
        target_role=req.target_role,
        resume_text=resume_text,
        skip_intro=req.skip_intro,
        duration_seconds=req.duration_minutes * 60,
        is_trial=is_trial,
        persona=req.persona,
        candidate_profile=profile,
        awaiting_history_pref=has_history,
    )
    if is_unrestricted:
        pass  # no trial/count bookkeeping at all for admins
    elif is_trial:
        # Mark the trial used now, at session creation, not at completion --
        # otherwise an abandoned-and-restarted interview would let a free
        # user get multiple trials for the cost of one.
        users_module.mark_interview_trial_used(user_id)
    else:
        users_module.increment_interview_count(user_id)

    users_module.record_activity(user_id, "interview_start", {"session_id": session["session_id"], "target_role": req.target_role})

    # Turn 1: greeting/settle-in/plan monologue, always -- independent of
    # skip_intro, which only controls whether the SEPARATE "introduce
    # yourself" question (turn 2) is asked or skipped in favor of a live
    # first real question. No candidate input happens between turns 1 and 2
    # -- UNLESS has_history, in which case the monologue itself ends by
    # asking the history-preference question and turn 2 waits for that
    # spoken reply (handled in api_interview_answer) before being generated.
    opening_monologue = llm.build_opening_monologue(
        target_role=req.target_role, profile=profile, persona=session["persona"], ask_history_pref=has_history,
    )
    interview.record_turn(session, "assistant", opening_monologue, None)

    if has_history:
        return {
            "session_id": session["session_id"],
            "opening_monologue": opening_monologue,
            "question": None,
            "topic": None,
            "action": "awaiting_history_pref",
            "table_context": None,
            "remaining_seconds": interview.remaining_seconds(session),
            "duration_seconds": session["duration_seconds"],
        }

    if req.skip_intro:
        # Forcing the opening topic explicitly, rather than leaving turn 1
        # to the model's own judgment plus a "don't ask an icebreaker"
        # instruction -- confirmed live that a prompt-only negative
        # instruction here was unreliable (the model kept defaulting to a
        # "tell me about your experience/background/tools" opener anyway,
        # even when that exact pattern was named as forbidden). forced_topic
        # is the same mechanism that already reliably forces a real switch
        # once MAX_TURNS_PER_TOPIC is hit elsewhere in this file -- reusing
        # a proven-reliable path instead of inventing a new instruction the
        # model has to newly learn to obey.
        opening_topic = (profile.get("recommended_topics") or topics_list)[0]
        try:
            result = llm.interview_turn(
                user_id=user_id,
                topics=topics_list,
                resume_text=resume_text,
                conversation=[],
                forced_topic=opening_topic,
                persona=session["persona"],
                target_role=req.target_role,
                candidate_profile=profile,
            )
        except Exception as e:
            raise HTTPException(502, f"AI interviewer unavailable right now ({e}).")
        question, topic, action, table_context = result["question"], result["topic"], result["action"], result["table_context"]
    else:
        question, topic, action, table_context = intro_question(req.target_role), "intro", "intro", None

    interview.record_turn(session, "assistant", question, topic)
    interview.update_topic_tracking(session, action, topic)
    interview.set_last_table_context(session, table_context)

    return {
        "session_id": session["session_id"],
        "opening_monologue": opening_monologue,
        "question": question,
        "topic": topic,
        "action": action,
        "table_context": table_context,
        "remaining_seconds": interview.remaining_seconds(session),
        "duration_seconds": session["duration_seconds"],
    }


MAX_AUDIO_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB -- generously covers even a long spoken answer at typical voice bitrates


@app.post("/api/interview/stt")
async def api_interview_stt(file: UploadFile = File(...), session_id: str | None = Form(default=None), x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """
    Transcribes one recorded answer to text.

    session_id, when it resolves to a session this user owns, is the gate --
    the same reasoning as api_interview_answer's: mark_interview_trial_used()
    already flipped interview_trial_used=True at /start, so re-running
    _require_paid_or_trial() here would 402 every trial candidate's very
    first spoken answer (confirmed live: voice input was completely broken
    for every free-trial interview, immediately after it started -- trial
    candidates could only ever use the typed-answer fallback). Session
    ownership is the correct, sufficient gate for continuing a session that
    was already validly started under the cap. Only falls back to the
    paid/trial check when no valid session_id is given at all (defensive
    default for a caller that somehow omits it, not the normal path).

    Also lets a transcription failure count toward the same consecutive-
    failure/connection-issue threshold as /api/interview/answer, since a
    flaky mic/network is arguably the most likely real "connection issue"
    a candidate hits.
    """
    user_id = auth.resolve_user_id(authorization, x_user_id)

    session = None
    if session_id:
        candidate_session = interview.get_session(session_id)
        if candidate_session and candidate_session["user_id"] == user_id:
            session = candidate_session

    if session is None and not users_module.is_admin(user_id):
        u = users_module.get_usage(user_id)
        _require_paid_or_trial(u)

    audio_bytes = await file.read(MAX_AUDIO_UPLOAD_BYTES + 1)
    if len(audio_bytes) > MAX_AUDIO_UPLOAD_BYTES:
        raise HTTPException(413, "Recording too large.")
    try:
        text = stt.transcribe(audio_bytes, file.filename or "answer.webm")
    except RuntimeError as e:
        detail = f"Voice transcription unavailable right now ({e})."
        if session:
            interview.record_failure(session)
            if session["consecutive_failures"] >= interview.CONNECTION_ISSUE_THRESHOLD:
                return JSONResponse(status_code=502, content={
                    "detail": detail,
                    "connection_issue": True,
                    "consecutive_failures": session["consecutive_failures"],
                })
        raise HTTPException(502, detail)
    if session:
        interview.reset_failures(session)
    return {"text": text}


MAX_TTS_CHARS = 2000  # generously covers even a long interviewer turn -- no legitimate question/monologue should exceed this


@app.post("/api/interview/tts")
def api_interview_tts(req: InterviewTtsRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """
    Synthesizes one interviewer line to speech. Same gate as the rest of
    the interview (Pro tier or the one free trial) -- see api_interview_stt's
    docstring for why no separate quota is needed.
    """
    user_id = auth.resolve_user_id(authorization, x_user_id)
    u = users_module.get_usage(user_id)
    if not users_module.is_admin(user_id):
        _require_paid_or_trial(u)

    text = req.text.strip()
    if not text:
        raise HTTPException(400, "text must not be empty.")
    if len(text) > MAX_TTS_CHARS:
        raise HTTPException(413, "Text too long to synthesize.")

    try:
        audio_bytes = tts.synthesize(text)
    except RuntimeError as e:
        raise HTTPException(502, f"Voice synthesis unavailable right now ({e}).")

    llm._log_usage({"user_id": user_id, "problem_id": "mock-interview-tts", "model": tts.TTS_MODEL, "characters": len(text)})
    return Response(content=audio_bytes, media_type="audio/mpeg")


@app.post("/api/interview/answer")
def api_interview_answer(req: InterviewAnswerRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    # No _require_paid_or_trial() re-check here: mark_interview_trial_used()
    # already flips interview_trial_used=True at /start, so re-checking it
    # here would 402 every trial user on their very first answer. Session
    # ownership (below) is the correct, sufficient gate for continuing a
    # session that was already validly started.
    session = interview.get_session(req.session_id)
    if not session or session["user_id"] != user_id:
        raise HTTPException(404, "Interview session not found")
    if session["ended"]:
        raise HTTPException(400, "This interview has already ended")

    interview.record_turn(session, "user", req.answer_text)

    if interview.is_time_up(session):
        return {
            "time_up": True,
            "session_id": session["session_id"],
            "remaining_seconds": 0,
        }

    if session.get("awaiting_history_pref"):
        # This reply answers the monologue's "focus on past weak areas, or
        # start fresh?" question, not a real interview question -- generate
        # the actual first question now instead of running interview_turn
        # against a non-existent "topic". Mirrors api_interview_start's
        # skip_intro/INTRO_QUESTION branch exactly, since this IS that
        # branch, just deferred until the candidate answered.
        session["awaiting_history_pref"] = False
        target_role = session.get("target_role") or "Data Analyst"
        topics_list = role_topics.topics_for_role(target_role)
        use_history = llm.classify_history_preference(user_id=user_id, answer_text=req.answer_text)
        profile = session.get("candidate_profile") or {}
        if use_history:
            topic_history = interview.get_topic_history(user_id, topics=topics_list)
            if topic_history:
                profile = llm.analyze_candidate_profile(
                    user_id=user_id, resume_text=session["resume_text"],
                    target_role=target_role, topic_history=topic_history,
                )
                session["candidate_profile"] = profile

        if session["skip_intro"]:
            # Same forced_topic fix as api_interview_start's skip_intro
            # branch, and for the same reason -- this branch's comment
            # claimed to mirror that one "exactly", but QA found it was
            # never actually given the fix: a returning candidate (the
            # only ones who ever reach this awaiting_history_pref branch)
            # requesting skip_intro on their 2nd+ interview still got the
            # "tell me about your experience" opener the forced_topic fix
            # exists specifically to prevent.
            opening_topic = (profile.get("recommended_topics") or topics_list)[0]
            try:
                result = llm.interview_turn(
                    user_id=user_id, topics=topics_list, resume_text=session["resume_text"],
                    conversation=[], forced_topic=opening_topic, persona=session["persona"],
                    target_role=target_role, candidate_profile=profile,
                )
            except Exception as e:
                raise HTTPException(502, f"AI interviewer unavailable right now ({e}).")
            question, topic, action, table_context = result["question"], result["topic"], result["action"], result["table_context"]
        else:
            question, topic, action, table_context = intro_question(target_role), "intro", "intro", None

        interview.record_turn(session, "assistant", question, topic)
        interview.update_topic_tracking(session, action, topic)
        interview.set_last_table_context(session, table_context)

        return {
            "time_up": False,
            "session_id": session["session_id"],
            "question": question,
            "topic": topic,
            "action": action,
            "table_context": table_context,
            "remaining_seconds": interview.remaining_seconds(session),
        }

    # session["target_role"] can be missing/None only for an interview that
    # was already in progress at the exact moment this migration deployed --
    # falls back to Data Analyst's topic set rather than a hard crash for
    # that narrow window.
    topics_list = role_topics.topics_for_role(session.get("target_role") or "Data Analyst")
    forced_topic = (
        interview.next_topic(session, topics_list)
        if interview.topic_cap_reached(session)
        else None
    )

    try:
        result = llm.interview_turn(
            user_id=user_id,
            topics=topics_list,
            resume_text=session["resume_text"],
            conversation=session["conversation"],
            current_topic=session["current_topic"],
            topic_turn_count=session["current_topic_turns"],
            forced_topic=forced_topic,
            persona=session["persona"],
            target_role=session.get("target_role") or "Data Analyst",
            candidate_profile=session.get("candidate_profile"),
        )
        # Whether this pass actually moved to a different topic -- NOT
        # result["action"] != "switch_topic", which trusts a label already
        # confirmed unreliable elsewhere this session (update_topic_tracking
        # keys off topic-string equality for the same reason). Confirmed
        # live: a candidate's clear "I honestly have no idea" correctly set
        # candidate_stuck=True, but the model's first pass ALSO claimed
        # action="switch_topic" while keeping the identical topic string --
        # trusting that label skipped the immediate re-run entirely, so the
        # "stuck" candidate got a same-topic rephrase instead of the
        # guaranteed-fresh-topic this backstop exists to provide.
        is_real_switch = result["topic"] != session["current_topic"]
        hint_cap_hit = (
            result.get("offer_hint")
            and session.get("hint_used_this_topic")
            and not is_real_switch
        )
        if (result.get("candidate_stuck") and not is_real_switch) or hint_cap_hit:
            # Two triggers for the same immediate-re-run pattern: a real
            # interviewer moves on after ONE clear "I don't know" (don't
            # rephrase and ask again), and doesn't offer a second hint on
            # the same topic either. Re-run right away with a forced
            # switch rather than deferring to next turn, so the candidate
            # never sees a same-topic rephrase after either signal.
            result = llm.interview_turn(
                user_id=user_id,
                topics=topics_list,
                resume_text=session["resume_text"],
                conversation=session["conversation"],
                current_topic=session["current_topic"],
                topic_turn_count=session["current_topic_turns"],
                forced_topic=interview.next_topic(session, topics_list),
                persona=session["persona"],
                target_role=session.get("target_role") or "Data Analyst",
                candidate_profile=session.get("candidate_profile"),
            )
    except Exception as e:
        # Roll back the user turn we just recorded so a client retry after a
        # transient failure doesn't leave a duplicate in the transcript.
        interview.remove_last_turn(session)
        interview.record_failure(session)
        detail = f"AI interviewer unavailable right now ({e})."
        if session["consecutive_failures"] >= interview.CONNECTION_ISSUE_THRESHOLD:
            # Repeated trouble, not just one blip -- give the frontend
            # enough to proactively offer retry/pause/end instead of just
            # erroring again on the next attempt too.
            return JSONResponse(status_code=502, content={
                "detail": detail,
                "connection_issue": True,
                "consecutive_failures": session["consecutive_failures"],
            })
        raise HTTPException(502, detail)

    interview.reset_failures(session)
    interview.record_turn(session, "assistant", result["question"], result["topic"])
    interview.update_topic_tracking(session, result["action"], result["topic"], result.get("candidate_stuck", False), result.get("offer_hint", False))
    interview.set_last_table_context(session, result["table_context"])

    return {
        "time_up": False,
        "session_id": session["session_id"],
        "question": result["question"],
        "topic": result["topic"],
        "action": result["action"],
        "table_context": result["table_context"],
        "remaining_seconds": interview.remaining_seconds(session),
    }


@app.get("/api/interview/session/{session_id}")
def api_interview_resume(session_id: str, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """
    Lets the frontend reconnect to an in-progress interview after a page
    reload, browser crash, or anything else that wiped its local state --
    the session itself lives in Postgres, not the browser, so as long as
    the interview hasn't ended it can always be picked back up from here.
    """
    user_id = auth.resolve_user_id(authorization, x_user_id)
    session = interview.get_session(session_id)
    if not session or session["user_id"] != user_id:
        raise HTTPException(404, "Interview session not found")

    question_turn = interview.last_question(session)
    return {
        "session_id": session["session_id"],
        "target_role": session.get("target_role"),
        "persona": session["persona"],
        "ended": session["ended"],
        "feedback": session["feedback"],
        "conversation": session["conversation"],
        "question": question_turn["content"] if question_turn else None,
        "topic": question_turn["topic"] if question_turn else None,
        "table_context": session["last_table_context"],
        "remaining_seconds": interview.remaining_seconds(session),
        "duration_seconds": session["duration_seconds"],
        "time_up": interview.is_time_up(session),
    }


@app.post("/api/interview/end")
def api_interview_end(req: InterviewEndRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    session = interview.get_session(req.session_id)
    if not session or session["user_id"] != user_id:
        raise HTTPException(404, "Interview session not found")

    if session["ended"] and session["feedback"] is not None:
        return {"feedback": session["feedback"], "conversation": session["conversation"]}

    target_role = session.get("target_role") or "Data Analyst"
    if session["feedback"] is None:
        topics_list = role_topics.topics_for_role(target_role)
        topic_history = interview.get_topic_history(user_id, topics=topics_list)
        try:
            result = llm.interview_feedback(
                user_id=user_id, conversation=session["conversation"],
                target_role=target_role, topic_history=topic_history,
            )
            feedback = result["report"]
        except Exception as e:
            raise HTTPException(502, f"Couldn't generate feedback report right now ({e}).")
        # Only on freshly-generated feedback -- record_topic_history must
        # run exactly once per interview, not again on every idempotent
        # re-call of this endpoint against already-existing feedback.
        interview.record_topic_history(user_id, session["session_id"], feedback.get("topic_scores", []))
        users_module.record_activity(user_id, "interview_end", {
            "session_id": session["session_id"], "target_role": target_role, "score": feedback.get("score"),
        })
    else:
        feedback = session["feedback"]

    interview.mark_ended(session, feedback)
    return {"feedback": feedback, "conversation": session["conversation"]}


class GenerateBatchRequest(BaseModel):
    count: int = 5
    topics: list[str] | None = None  # defaults to all gradeable topics if omitted
    track: str = "sql"  # "sql" | "python"


def _require_admin(request: Request):
    """
    Accepts either of two independent credentials:
    - the static X-Admin-Token shared secret (the original scheme --
      kept as a bootstrap/fallback path, e.g. for the one-time
      grant-admin call below before any real admin account exists), or
    - a verified Clerk session (Authorization: Bearer <token>) belonging
      to a user with is_admin=True in the `users` table.
    Real per-account authentication is the intended long-term path; the
    static token isn't being ripped out in the same change that
    introduces it, since that would lock out admin access the moment
    this deploys if the grant-admin step hasn't happened yet.
    """
    x_admin_token = request.headers.get("x-admin-token")
    # hmac.compare_digest instead of != -- plain string comparison short-
    # circuits on the first differing byte, which leaks how many
    # characters of the token you got right via response-time
    # differences. Doesn't matter much on Render's jittery network, but
    # it's a one-line fix for a real (if low-severity) timing side channel.
    if ADMIN_TOKEN and x_admin_token and hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        return

    authorization = request.headers.get("authorization")
    x_user_id = request.headers.get("x-user-id")
    user_id = auth.resolve_user_id(authorization, x_user_id)
    if users_module.is_admin(user_id):
        return

    raise HTTPException(403, "Invalid or missing admin credentials.")


@app.post("/api/admin/problems/generate-batch")
def api_admin_generate_batch(req: GenerateBatchRequest, request: Request):
    """
    Drafts a new batch of practice problems via the LLM and stores the
    ones that pass validation as pending_review -- nothing here ever goes
    live without a human approving it via /approve below. This is what the
    weekly cron job (render.yaml) calls once 45 days have elapsed since
    the last batch; it's also callable by hand for an out-of-cycle batch.
    """
    _require_admin(request)
    if req.track not in ("sql", "python", "case"):
        raise HTTPException(400, "track must be 'sql', 'python', or 'case'")
    is_python = req.track == "python"
    is_case = req.track == "case"
    if is_case:
        target_topics = req.topics or case_topics.CASE_TOPICS
    else:
        target_topics = req.topics or (py_topics.PY_GRADEABLE_TOPICS if is_python else topics.GRADEABLE_TOPICS)

    # Statistics and pandas/numpy are topic vocabularies that ride the
    # Python track's E2B grading (see stats_topics.py/data_lib_topics.py),
    # not separate tracks -- so which system prompt to use is decided by
    # which topics were actually requested, not by req.track alone. Only
    # kicks in when the caller explicitly asked for topics entirely
    # within one of these vocabularies; a mixed or omitted `topics` list
    # falls back to the general Python Cookbook prompt.
    python_system_prompt = None
    if is_python and req.topics:
        if all(t in stats_topics.STATS_TOPICS for t in req.topics):
            python_system_prompt = llm.STATS_PYTHON_BATCH_SYSTEM_PROMPT
        elif all(t in data_lib_topics.DATA_LIBRARY_TOPICS for t in req.topics):
            python_system_prompt = llm.DATA_LIB_PYTHON_BATCH_SYSTEM_PROMPT

    # Same swap-in pattern for the Business Case track's two lenses (DA/DE
    # -- see case_topics.py): defaults to the DA-flavored prompt unless
    # the caller explicitly asked for topics entirely within the DE list.
    case_system_prompt = None
    if is_case and req.topics and all(t in case_topics.CASE_DE_TOPICS for t in req.topics):
        case_system_prompt = llm.CASE_DE_BATCH_SYSTEM_PROMPT

    try:
        if is_case:
            result = llm.generate_case_batch(
                user_id="admin",
                topics=target_topics,
                count=req.count,
                existing_titles=problems_module.list_existing_titles(),
                system_prompt=case_system_prompt,
            )
        elif is_python:
            result = llm.generate_python_problem_batch(
                user_id="admin",
                topics=target_topics,
                count=req.count,
                existing_titles=problems_module.list_existing_titles(),
                system_prompt=python_system_prompt,
            )
        else:
            result = llm.generate_problem_batch(
                user_id="admin",
                topics=target_topics,
                count=req.count,
                existing_titles=problems_module.list_existing_titles(),
            )
    except Exception as e:
        raise HTTPException(502, f"Couldn't generate problems right now ({e}).")

    inserted, skipped = [], []
    for draft in result["problems"]:
        try:
            problem_id = problems_module.insert_pending_draft(draft, track=req.track)
            inserted.append(problem_id)
        except problems_module.InvalidDraftProblem as e:
            skipped.append({"title": draft.get("title", "<untitled>"), "reason": str(e)})

    problems_module.mark_batch_generated()
    return {"inserted": inserted, "skipped": skipped, "usage": result["usage"]}


@app.get("/api/admin/problems/pending")
def api_admin_list_pending(request: Request):
    _require_admin(request)
    return {"problems": problems_module.list_pending_problems()}


@app.get("/api/admin/users/summary")
def api_admin_users_summary(request: Request):
    _require_admin(request)
    return users_module.get_admin_summary()


@app.get("/api/admin/stats/solved-by-category")
def api_admin_solved_by_category(request: Request):
    """Platform-wide solved-problem counts by category (sql/python/
    pandas/numpy), for the chart at the top of the admin Users page."""
    _require_admin(request)
    return problems_module.get_platform_solved_breakdown()


@app.get("/api/admin/users")
def api_admin_list_users(request: Request):
    _require_admin(request)
    return {"users": users_module.list_all_users()}


@app.get("/api/admin/users/{user_id}/history")
def api_admin_user_history(user_id: str, request: Request):
    _require_admin(request)
    return {"history": problems_module.get_user_submission_history(user_id)}


@app.get("/api/admin/users/{user_id}/activity")
def api_admin_user_activity(user_id: str, request: Request):
    """General site-activity timeline for one user (sign-ins aside --
    those aren't observable server-side under the current Clerk-on-the-
    frontend setup): track views, interview start/end, resume actions,
    plan changes. Submissions have their own, more detailed endpoint
    above; this is everything else, see users.record_activity."""
    _require_admin(request)
    return {"activity": users_module.get_activity(user_id)}


@app.get("/api/admin/problems/live")
def api_admin_list_live(request: Request):
    """Full live-problem bank for the admin staging-area page -- lets an
    admin find and unpublish an already-live problem (e.g. a duplicate-
    concept one found during a content audit), not just gate new drafts
    before they first go live."""
    _require_admin(request)
    return {"problems": problems_module.list_live_problems_full()}


class ReclassifyRequest(BaseModel):
    track: str
    ids: list[str]


@app.post("/api/admin/problems/reclassify-topics")
def api_admin_reclassify_topics(req: ReclassifyRequest, request: Request):
    """
    Audits an explicit batch of already-live problems for topic
    mislabeling (see llm.reclassify_topics_batch) and relabels any whose
    real code doesn't match their current topic. Takes an explicit id
    list (not "do the whole track") so a full sweep can be driven in
    small, resumable, monitorable batches from the caller side, the same
    pattern already used for content generation and approval this
    session -- not because a single large call couldn't work, but
    because it's proven more resilient to timeouts/retries in practice.

    Registered here (before the GET /{problem_id} route below) rather
    than near the other POST /{problem_id}/... actions -- Starlette
    matches path SHAPE before checking method across all routes with
    that shape, so a literal path with the same segment count as
    /{problem_id} has to come before it or it 405s (a "reclassify-topics"
    request matches the {problem_id} pattern first and finds only GET
    registered there).
    """
    _require_admin(request)
    if req.track not in ("sql", "python"):
        raise HTTPException(400, "track must be 'sql' or 'python'")
    batch = []
    for pid in req.ids:
        p = problems_module.get_problem(pid)
        if p and p.get("track", "sql") == req.track:
            batch.append(p)
    if not batch:
        return {"checked": 0, "relabeled": []}

    if req.track == "sql":
        buckets = {"sql": (topics.GRADEABLE_TOPICS, batch)}
    else:
        # Cross-contamination guard: an LLM asked to pick from one giant
        # merged list (general + stats + pandas + numpy) reliably reaches
        # for the "fancier"-sounding specific topic based on loose scenario
        # association -- a function that counts things per day "sounds"
        # statistics-y even with zero statistical content. A prompt
        # instruction saying "don't do that" wasn't reliable in practice
        # (verified: it kept doing it anyway). Bucketing by actual code
        # content FIRST, in plain Python, then only offering the LLM
        # topics from within that one bucket, makes the mistake
        # structurally impossible rather than just discouraged.
        pandas_batch, numpy_batch, stats_batch, general_batch = [], [], [], []
        stats_markers = (
            "statistics.", "scipy.stats", "np.mean(", "np.std(", "np.var(",
            "mean(", "median(", "variance(", "stdev(", "correlation",
            "p_value", "confidence_interval", "z_score", "t_test", "pvalue",
        )
        for p in batch:
            # The import usually lives in test_code, not
            # canonical_solution -- the solution function itself often
            # just operates on a DataFrame/array passed in as a
            # parameter without needing to import the library at all
            # (e.g. `arr[arr > threshold]` needs no import to work on
            # whatever numpy array test_code hands it).
            code = (p.get("canonical_solution") or "") + "\n" + (p.get("test_code") or "")
            if "import pandas" in code or re.search(r"\bpd\.", code):
                pandas_batch.append(p)
            elif "import numpy" in code or re.search(r"\bnp\.", code):
                numpy_batch.append(p)
            elif any(m in code for m in stats_markers):
                stats_batch.append(p)
            else:
                general_batch.append(p)
        buckets = {
            "pandas": (data_lib_topics.DATA_LIBRARY_TOPICS, pandas_batch),
            "numpy": (data_lib_topics.DATA_LIBRARY_TOPICS, numpy_batch),
            "stats": (stats_topics.STATS_TOPICS, stats_batch),
            "general": (py_topics.PY_GRADEABLE_TOPICS, general_batch),
        }

    relabeled = []
    total_checked = 0
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}
    for bucket_name, (allowed_topics, bucket_problems) in buckets.items():
        if not bucket_problems:
            continue
        try:
            result = llm.reclassify_topics_batch(
                user_id="admin", problems=bucket_problems, allowed_topics=allowed_topics, track=req.track,
            )
        except Exception as e:
            raise HTTPException(502, f"Couldn't reclassify right now ({e}).")
        by_id = {p["id"]: p for p in bucket_problems}
        for r in result["results"]:
            pid = r.get("id")
            new_topic = r.get("topic")
            if pid not in by_id or new_topic not in allowed_topics:
                continue
            old_topic = by_id[pid]["topic"]
            if new_topic != old_topic:
                problems_module.set_problem_topic(pid, new_topic)
                relabeled.append({"id": pid, "old": old_topic, "new": new_topic, "bucket": bucket_name})
        total_checked += len(bucket_problems)
        for k in usage_totals:
            usage_totals[k] += result["usage"].get(k, 0)
    return {"checked": total_checked, "relabeled": relabeled, "usage": usage_totals}


class AskPhoenixTestRequest(BaseModel):
    problem_id: str
    question: str
    conversation: list[dict] = []


@app.post("/api/admin/ask-phoenix-test")
def api_admin_ask_phoenix_test(req: AskPhoenixTestRequest, request: Request):
    """Admin-only, bypasses the Pro-tier gate -- for manually exercising
    Ask Phoenix with an arbitrary question while debugging/tuning its
    system prompt, without needing a real paid test account."""
    _require_admin(request)
    p = problems_module.get_problem(req.problem_id)
    if not p:
        raise HTTPException(404, "Problem not found")
    try:
        result = llm.ask_phoenix(user_id="admin", problem=p, current_query=None, conversation=req.conversation, question=req.question)
    except Exception as e:
        raise HTTPException(502, f"Ask Phoenix unavailable right now ({e}).")
    return {"answer": result["answer"], "usage": result["usage"]}


class AuditBatchRequest(BaseModel):
    ids: list[str]


def _audit_ask_phoenix_questions(track: str) -> tuple[str, str]:
    """Fixed normal/edge-case questions sent to Ask Phoenix for every
    audited problem, so results are comparable across the whole bank
    rather than per-problem improvised prompts. Deliberately track-aware
    (SQL vs Python framing) but not problem-specific -- a generic-but-real
    student question is exactly what exercises the GUIDE-mode persona; a
    problem-specific question would need its own LLM call to generate,
    doubling cost for no real gain in signal."""
    if track == "python":
        return (
            "Can you help me understand the general approach here, and "
            "what edge cases I should be thinking about?",
            "What if the input is empty, or has just one element -- would "
            "my approach still need to handle that correctly?",
        )
    return (
        "What's the general approach to solve this problem?",
        "What if there are no matching rows at all -- does my query still "
        "need to handle that case?",
    )


def _run_audit_for_problem(p: dict) -> dict:
    """
    Full content-quality audit pipeline for one problem, any track --
    objective correctness where one exists (reusing the exact same
    execution paths production grading uses), two real Ask Phoenix
    exchanges for SQL/Python (skipped for Case, which doesn't offer Ask
    Phoenix at all), and one LLM judge call covering FAANG-grade quality /
    topic alignment / description sufficiency / difficulty calibration /
    sample-I/O sanity / (SQL+Python only) Ask Phoenix quality -- see
    llm.audit_problem_quality. Shared by audit-batch (explicit re-audit of
    already-live problems) and the approve endpoint (automatic audit the
    moment a problem goes live) so both paths stay in lockstep rather
    than drifting into two slightly-different audit implementations.

    Raises on failure -- callers decide how to handle that (audit-batch
    surfaces it as a per-id error entry; approve treats it as non-fatal
    since the problem's correctness was already validated at draft-insert
    time and an audit hiccup shouldn't block a real approval).
    """
    track = p.get("track", "sql")
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}

    # Dedup is now a standing part of every audit, not just a one-time
    # insert-time gate -- re-checked against the CURRENT bank every time
    # (see problems.check_duplicate), so it also catches drift after the
    # fact (e.g. a hand-edited title/content that happens to converge
    # with another problem later). Deterministic, not LLM-judged, so a
    # real hit always forces needs_fix rather than being just one more
    # noisy opinion among several.
    content_field = "canonical_sql" if track == "sql" else "canonical_solution" if track == "python" else "case_prompt"
    dup_result = problems_module.check_duplicate(p["id"], track, p["title"], p.get(content_field))

    if track == "case":
        allowed_topics = case_topics.CASE_TOPICS
        correctness = {"passed": None, "detail": "N/A -- no single verifiable answer for this track"}
        judge_result = llm.audit_problem_quality(
            user_id="admin", problem=p, allowed_topics=allowed_topics, correctness=correctness,
        )
        for k in usage_totals:
            usage_totals[k] += judge_result["usage"].get(k, 0)
        verdict = judge_result["verdict"]
        verdict["duplicate_check"] = dup_result
        if not dup_result["ok"]:
            verdict["overall_verdict"] = "needs_fix"
        return {
            "id": p["id"], "title": p["title"], "track": track,
            "difficulty": p["difficulty"], "topic": p["topic"],
            "correctness": correctness, "ask_phoenix": None,
            "verdict": verdict, "usage": usage_totals,
        }

    if track == "python":
        allowed_topics = py_topics.PY_GRADEABLE_TOPICS + stats_topics.STATS_TOPICS + data_lib_topics.DATA_LIBRARY_TOPICS
        try:
            run_result = pysandbox.run_python_submission(student_code=p["canonical_solution"], test_code=p["test_code"])
            discriminates = pysandbox.test_code_discriminates(test_code=p["test_code"], function_signature=p["function_signature"])
            correctness = {
                "passed": bool(run_result["passed"] and discriminates),
                "detail": run_result.get("error") or (None if discriminates else "test_code does not discriminate a wrong answer (vacuous test)"),
            }
        except Exception as e:
            correctness = {"passed": False, "detail": f"Execution error: {e}"}
    else:
        allowed_topics = topics.GRADEABLE_TOPICS
        try:
            cols, rows, _truncated = sandbox.compute_expected_output(p)
            correctness = {
                "passed": bool(cols) and len(rows) > 0,
                "detail": None if (cols and len(rows) > 0) else "canonical_sql produced no rows or no columns",
            }
        except Exception as e:
            correctness = {"passed": False, "detail": f"DuckDB error: {e}"}

    normal_q, edge_q = _audit_ask_phoenix_questions(track)
    normal_result = llm.ask_phoenix(user_id="admin", problem=p, current_query=None, conversation=[], question=normal_q)
    edge_result = llm.ask_phoenix(user_id="admin", problem=p, current_query=None, conversation=[], question=edge_q)
    for k in usage_totals:
        usage_totals[k] += normal_result["usage"].get(k, 0) + edge_result["usage"].get(k, 0)

    judge_result = llm.audit_problem_quality(
        user_id="admin",
        problem=p,
        allowed_topics=allowed_topics,
        correctness=correctness,
        ask_phoenix_normal={"question": normal_q, "answer": normal_result["answer"]},
        ask_phoenix_edge={"question": edge_q, "answer": edge_result["answer"]},
    )
    for k in usage_totals:
        usage_totals[k] += judge_result["usage"].get(k, 0)

    verdict = judge_result["verdict"]
    verdict["duplicate_check"] = dup_result
    if not dup_result["ok"]:
        verdict["overall_verdict"] = "needs_fix"

    return {
        "id": p["id"],
        "title": p["title"],
        "track": track,
        "difficulty": p["difficulty"],
        "topic": p["topic"],
        "correctness": correctness,
        "ask_phoenix": {
            "normal": {"question": normal_q, "answer": normal_result["answer"]},
            "edge": {"question": edge_q, "answer": edge_result["answer"]},
        },
        "verdict": verdict,
        "usage": usage_totals,
    }


@app.post("/api/admin/problems/audit-batch")
def api_admin_audit_batch(req: AuditBatchRequest, request: Request):
    """
    Full content-quality audit for an explicit batch of already-live
    problems (see _run_audit_for_problem). Purely diagnostic on its own --
    never writes anything back to a problem; the caller reviews the
    report and decides fixes separately. Also runs automatically, one
    problem at a time, whenever a draft is approved (see the approve
    endpoint below) -- this endpoint remains for re-auditing already-live
    problems in bulk (e.g. after a prompt/taxonomy change) or re-checking
    specific ids.

    Explicit id list (not "audit everything"), same resumable-batch
    pattern as reclassify-topics -- a 279-problem sweep needs to be driven
    in small chunks from the caller side, not as one giant request.
    """
    _require_admin(request)
    results = []
    for pid in req.ids:
        p = problems_module.get_problem(pid)
        if not p:
            results.append({"id": pid, "error": "not found"})
            continue
        try:
            results.append(_run_audit_for_problem(p))
        except Exception as e:
            results.append({"id": pid, "title": p.get("title"), "track": p.get("track"), "error": str(e)})
    return {"checked": len(results), "results": results}


@app.post("/api/admin/problems/{problem_id}/unpublish")
def api_admin_unpublish(problem_id: str, request: Request):
    _require_admin(request)
    ok = problems_module.unpublish_problem(problem_id)
    if not ok:
        raise HTTPException(404, "Live problem not found.")
    return {"id": problem_id, "status": "archived"}


@app.post("/api/admin/problems/{problem_id}/republish")
def api_admin_republish(problem_id: str, request: Request):
    _require_admin(request)
    ok = problems_module.republish_problem(problem_id)
    if not ok:
        raise HTTPException(404, "Archived problem not found.")
    return {"id": problem_id, "status": "live"}


class SetTopicRequest(BaseModel):
    topic: str


@app.post("/api/admin/problems/{problem_id}/set-topic")
def api_admin_set_topic(problem_id: str, req: SetTopicRequest, request: Request):
    """Manual override for a single problem's topic -- e.g. when the
    automated reclassify-topics pass itself proposes a wrong label (it's
    an LLM call, not infallible) and a human reviewer needs to correct
    it directly rather than re-running the same fallible classifier."""
    _require_admin(request)
    p = problems_module.get_problem(problem_id)
    if not p:
        raise HTTPException(404, "Problem not found")
    allowed_topics = (
        topics.GRADEABLE_TOPICS if p.get("track", "sql") == "sql"
        else py_topics.PY_GRADEABLE_TOPICS + stats_topics.STATS_TOPICS + data_lib_topics.DATA_LIBRARY_TOPICS
    )
    if req.topic not in allowed_topics:
        raise HTTPException(400, f"'{req.topic}' is not a valid topic for this problem's track.")
    problems_module.set_problem_topic(problem_id, req.topic)
    return {"id": problem_id, "topic": req.topic}


class MergeTopicsRequest(BaseModel):
    old_topics: list[str]
    new_topic: str


@app.post("/api/admin/problems/merge-topics")
def api_admin_merge_topics(req: MergeTopicsRequest, request: Request):
    """One-off bulk relabel: every problem currently on any of
    `old_topics` moves to `new_topic` in a single statement -- for
    collapsing an overly granular taxonomy (e.g. several NumPy/Pandas
    chapter-level topics) into one coarser label, without touching each
    problem individually via set-topic."""
    _require_admin(request)
    if not req.old_topics:
        raise HTTPException(400, "old_topics must be a non-empty list.")
    changed = problems_module.merge_topics(req.old_topics, req.new_topic)
    return {"old_topics": req.old_topics, "new_topic": req.new_topic, "changed": changed}


class BackfillTestCasesRequest(BaseModel):
    ids: list[str]
    count: int = problems_module.SQL_HIDDEN_TEST_CASE_COUNT


@app.post("/api/admin/problems/backfill-test-cases")
def api_admin_backfill_test_cases(req: BackfillTestCasesRequest, request: Request):
    """
    Backfills hidden, adversarially-constructed seed datasets for existing
    live SQL problems that predate this feature (grading was previously
    single-dataset for all of them -- see llm.generate_discriminating_test_cases
    for what "adversarially-constructed" means here). Explicit id list,
    never "all" in one call -- same resumable-batch pattern as
    merge-topics/audit-batch, since the full live SQL bank is too much for
    one request/timeout budget.

    Clears any existing problem_test_cases rows for a given id first, so
    re-running this on an already-backfilled problem (e.g. after a manual
    seed_sql edit) replaces rather than piles on top of stale datasets.
    Refreshes _EXPECTED_CACHE immediately per problem so real grading
    picks up the new datasets without a restart.
    """
    _require_admin(request)
    if not req.ids:
        raise HTTPException(400, "ids must be a non-empty list.")
    results = []
    for pid in req.ids:
        p = problems_module.get_problem(pid)
        if not p or p.get("track", "sql") != "sql":
            results.append({"id": pid, "error": "not found or not a SQL-track problem"})
            continue
        try:
            problems_module.clear_test_case_seeds(pid)
            gen_result = llm.generate_discriminating_test_cases(user_id="admin", problem=p, count=req.count)
            for case in gen_result["validated"]:
                problems_module.add_test_case_seed(pid, case["seed_sql"], case["defeats_wrong_query"])
            refreshed = problems_module.get_problem(pid)
            _EXPECTED_CACHE[pid] = _compute_expected_cache_entry(refreshed)
            results.append({
                "id": pid, "title": p["title"],
                "requested": req.count, "validated": len(gen_result["validated"]),
                "usage": gen_result["usage"],
            })
        except Exception as e:
            results.append({"id": pid, "title": p.get("title"), "error": str(e)})
    return {"checked": len(results), "results": results}


class SetDescriptionRequest(BaseModel):
    description: str


@app.post("/api/admin/problems/{problem_id}/set-description")
def api_admin_set_description(problem_id: str, req: SetDescriptionRequest, request: Request):
    """Manual override for a single problem's description -- for cases
    where generation prompt instructions alone aren't reliable enough
    (verified: e.g. requiring Files-and-I/O problems to show concrete
    example file content kept getting ignored even after two regenerate
    attempts) and a human reviewer needs to write correct prose by hand
    rather than keep hoping the model complies."""
    _require_admin(request)
    p = problems_module.get_problem(problem_id)
    if not p:
        raise HTTPException(404, "Problem not found")
    problems_module.set_problem_description(problem_id, req.description)
    return {"id": problem_id, "description": req.description}


class SetDifficultyRequest(BaseModel):
    difficulty: str


@app.post("/api/admin/problems/{problem_id}/set-difficulty")
def api_admin_set_difficulty(problem_id: str, req: SetDifficultyRequest, request: Request):
    """Manual override for a single problem's difficulty label -- same
    shape as set-topic, for applying the content-quality audit's
    difficulty-calibration suggestions (llm.audit_problem_quality) after
    human review."""
    _require_admin(request)
    p = problems_module.get_problem(problem_id)
    if not p:
        raise HTTPException(404, "Problem not found")
    if req.difficulty not in ("easy", "medium", "hard"):
        raise HTTPException(400, "difficulty must be 'easy', 'medium', or 'hard'.")
    problems_module.set_problem_difficulty(problem_id, req.difficulty)
    return {"id": problem_id, "difficulty": req.difficulty}


class SetFreeRequest(BaseModel):
    is_free: bool


@app.post("/api/admin/problems/{problem_id}/set-free")
def api_admin_set_free(problem_id: str, req: SetFreeRequest, request: Request):
    """Manual toggle for a single problem's free-tier availability --
    same shape as set-topic/set-difficulty, for curating the free sample
    one problem at a time (the SQL track's curated set is currently a
    hardcoded id list in problems.py; Python/Case don't have one yet)."""
    _require_admin(request)
    p = problems_module.get_problem(problem_id)
    if not p:
        raise HTTPException(404, "Problem not found")
    problems_module.set_problem_free(problem_id, req.is_free)
    return {"id": problem_id, "is_free": req.is_free}


class PatchContentRequest(BaseModel):
    description: str | None = None
    canonical_solution: str | None = None
    test_code: str | None = None
    canonical_sql: str | None = None
    seed_sql: str | None = None
    schema_sql: str | None = None


@app.post("/api/admin/problems/{problem_id}/patch-content")
def api_admin_patch_content(problem_id: str, req: PatchContentRequest, request: Request):
    """
    Fixes a genuine, verified content bug found by the audit pipeline
    (llm.audit_problem_quality) that goes beyond a plain description
    rewrite -- e.g. a canonical_solution with a hardcoded magic number
    standing in for a value that can't actually be derived from the
    stated inputs, or a SQL query that structurally can't produce what
    the description promises. Only the fields actually passed are
    changed; everything else on the problem stays as-is.

    Verifies the MERGED (not-yet-committed) content against the exact
    same objective-correctness check the audit and real grading use
    BEFORE writing anything -- a patch that doesn't pass its own test is
    rejected outright rather than landing a live problem in a broken
    state. Refreshes the SQL expected-output cache immediately for SQL
    patches, since a stale cache would grade real submissions against
    the OLD content.
    """
    _require_admin(request)
    p = problems_module.get_problem(problem_id)
    if not p:
        raise HTTPException(404, "Problem not found")
    track = p.get("track", "sql")
    updates = {k: v for k, v in req.dict().items() if v is not None}
    if not updates:
        return {"id": problem_id, "updated_fields": [], "verified": True}
    merged = {**p, **updates}

    if track == "python":
        try:
            result = pysandbox.run_python_submission(student_code=merged["canonical_solution"], test_code=merged["test_code"])
        except Exception as e:
            raise HTTPException(400, f"Verification error: {e}")
        if not result["passed"]:
            raise HTTPException(400, f"Patched content fails its own test_code: {result.get('error')}")
    else:
        try:
            cols, rows, _truncated = sandbox.compute_expected_output(merged)
        except Exception as e:
            raise HTTPException(400, f"Patched SQL doesn't execute cleanly: {e}")
        if not cols:
            raise HTTPException(400, "Patched SQL produced no columns.")

    problems_module.patch_problem_content(problem_id, **updates)

    if track == "sql" and any(k in updates for k in ("canonical_sql", "seed_sql", "schema_sql")):
        refreshed = problems_module.get_problem(problem_id)
        _EXPECTED_CACHE[problem_id] = _compute_expected_cache_entry(refreshed)

    return {"id": problem_id, "updated_fields": list(updates.keys()), "verified": True}


@app.get("/api/admin/problems/{problem_id}")
def api_admin_get_problem(problem_id: str, request: Request):
    """
    Full problem detail (any status, bypassing the Pro-tier paywall) for
    content-quality review -- e.g. auditing the live bank for duplicate/
    overlapping concepts. Unlike GET /api/problems/{problem_id}, this
    never checks is_free/tier since it's admin-only.
    """
    _require_admin(request)
    p = problems_module.get_problem(problem_id)
    if not p:
        raise HTTPException(404, "Problem not found")
    return p


@app.get("/api/admin/problems/{problem_id}/check-test-discriminates")
def api_admin_check_test_discriminates(problem_id: str, request: Request):
    """Runs an already-live Python problem's test_code against a
    deliberately wrong stub (see pysandbox.test_code_discriminates) --
    for auditing the existing bank for the same vacuous-test defect the
    insert-time validation now catches for new drafts."""
    _require_admin(request)
    p = problems_module.get_problem(problem_id)
    if not p or p.get("track") != "python":
        raise HTTPException(404, "Python problem not found.")
    discriminates = pysandbox.test_code_discriminates(
        test_code=p["test_code"], function_signature=p["function_signature"],
    )
    return {"id": problem_id, "discriminates": discriminates}


def _compute_examples_for_problem(problem_id: str, p: dict) -> dict | list | None:
    """
    Computes and stores the real sample input/output shown to students on
    the problem page. For SQL this is just the already-cached real
    canonical_sql result (first 3 rows) against the real seed data -- for
    Python it actually re-runs test_code in E2B with the target function
    instrumented, capturing real (args, result) pairs from real passing
    assertions (see pysandbox.extract_examples). Never a separately
    hand-written example, so it can't drift from what the problem
    actually does. No-op (returns None) for track='case', which has no
    sample-I/O concept. Shared by the compute-examples endpoint and the
    approve endpoint (which calls this automatically before running the
    content-quality audit, so a fresh approval's audit isn't spuriously
    flagged for an example that just hasn't been computed yet).
    """
    if p.get("track") == "python":
        examples = pysandbox.extract_examples(
            canonical_solution=p["canonical_solution"],
            test_code=p["test_code"],
            function_signature=p["function_signature"],
        )
        problems_module.set_problem_examples(problem_id, examples)
        return examples

    if p.get("track") == "case":
        return None

    if problem_id in _EXPECTED_CACHE:
        cols, rows = _EXPECTED_CACHE[problem_id][0]  # primary dataset -- the one shown to students
    else:
        cols, rows, _ = sandbox.compute_expected_output(p)
    example = {"columns": cols, "rows": [[sandbox._normalize_cell(v) for v in row] for row in rows[:3]]}
    problems_module.set_problem_examples(problem_id, example)
    return example


@app.post("/api/admin/problems/{problem_id}/compute-examples")
def api_admin_compute_examples(problem_id: str, request: Request):
    _require_admin(request)
    p = problems_module.get_problem(problem_id)
    if not p:
        raise HTTPException(404, "Problem not found")
    examples = _compute_examples_for_problem(problem_id, p)
    return {"id": problem_id, "track": p.get("track", "sql"), "examples": examples}


@app.post("/api/admin/problems/{problem_id}/approve")
def api_admin_approve(problem_id: str, request: Request):
    _require_admin(request)
    ok = problems_module.approve_problem(problem_id)
    if not ok:
        raise HTTPException(404, "Pending problem not found.")
    # Warm this problem's expected-output cache immediately -- otherwise
    # the first submission against it would KeyError until next restart.
    # Python problems are graded live against test_code, not a cached
    # expected output, so there's nothing to warm for those.
    approved = problems_module.get_problem(problem_id)
    if approved.get("track", "sql") == "sql":
        _EXPECTED_CACHE[problem_id] = _compute_expected_cache_entry(approved)

    # Compute real sample I/O before auditing (not after) -- otherwise
    # every fresh approval's audit would spuriously flag "no example yet"
    # for something that just hasn't been computed, rather than something
    # actually wrong with the problem.
    try:
        _compute_examples_for_problem(problem_id, approved)
        approved = problems_module.get_problem(problem_id)
    except Exception:
        pass  # nice-to-have; a failure here shouldn't block approval or the audit below

    # Content-quality audit is now standard for every addition to the
    # bank -- run it the moment a problem goes live, rather than as an
    # occasional separate manual sweep, so quality issues surface
    # immediately instead of being found weeks later in a bulk re-audit.
    # Non-fatal: an audit hiccup (e.g. a transient LLM error) never
    # blocks the approval itself -- the problem's objective correctness
    # was already validated at draft-insert time (see
    # problems.insert_pending_draft); this is an additional quality
    # signal on top of that, not the gate a draft has to clear to ship.
    try:
        audit = _run_audit_for_problem(approved)
    except Exception as e:
        audit = {"error": str(e)}
    return {"id": problem_id, "status": "live", "audit": audit}


@app.post("/api/admin/problems/{problem_id}/reject")
def api_admin_reject(problem_id: str, request: Request):
    _require_admin(request)
    ok = problems_module.reject_problem(problem_id)
    if not ok:
        raise HTTPException(404, "Pending problem not found.")
    return {"id": problem_id, "status": "rejected"}


@app.get("/api/admin/cadence")
def api_admin_cadence(request: Request):
    _require_admin(request)
    return {"last_batch_generated_at": problems_module.get_last_batch_generated_at()}


class SetTierRequest(BaseModel):
    user_id: str
    tier: str


@app.post("/api/admin/set-tier")
def api_admin_set_tier(req: SetTierRequest, request: Request):
    """Manually flips a user's tier -- for testing, and for handling a
    refund/dispute by hand later since there's no self-serve downgrade."""
    _require_admin(request)
    if req.tier not in ("free", "paid"):
        raise HTTPException(400, "tier must be 'free' or 'paid'")
    users_module.set_tier(req.user_id, req.tier)
    return {"user_id": req.user_id, "tier": req.tier}


class SetAdminRequest(BaseModel):
    user_id: str
    is_admin: bool


@app.post("/api/admin/set-admin")
def api_admin_set_admin(req: SetAdminRequest, request: Request):
    """
    Grants or revokes admin rights for a target user -- same shape as
    /api/admin/set-tier. Gated by the normal _require_admin() (static
    token, or an existing Clerk admin), so only someone who is already an
    admin (or holds the bootstrap token) can designate anyone else as one.
    Deliberately NOT self-service: there is no "grant myself admin" call a
    signed-in-but-unprivileged account can make on its own.

    req.user_id accepts an email, a username, or a raw user id (the old
    "look up the target's user_id via /api/whoami and paste it" flow still
    works) -- resolved via users.find_user_id, which requires an existing
    matching account. Rejects with 404 rather than silently creating a
    fresh admin-flagged user row for a typo'd identifier that matches no
    one.
    """
    _require_admin(request)
    resolved_id = users_module.find_user_id(req.user_id)
    if not resolved_id:
        raise HTTPException(404, f"No user found matching '{req.user_id}' (checked as a user id, email, and username).")
    users_module.set_admin(resolved_id, req.is_admin)
    return {"user_id": resolved_id, "is_admin": req.is_admin}


@app.get("/api/admin/admins")
def api_admin_list_admins(request: Request):
    _require_admin(request)
    return {"admins": users_module.list_admins()}


@app.get("/api/whoami")
def api_whoami(x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    """Returns the caller's own resolved identity -- lets the owner sign in
    once and read off their own user_id to grant themselves admin via
    /api/admin/set-admin, without exposing anything sensitive (this
    reveals nothing about anyone but the caller themselves)."""
    return {"user_id": auth.resolve_user_id(authorization, x_user_id)}
