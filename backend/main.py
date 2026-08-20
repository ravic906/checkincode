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

from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import duckdb

import problems as problems_module
from problems import get_problem, list_problems_summary
import sandbox
import llm
import interview
import resume_parser
import db
import topics
import auth
import users as users_module
import payments

app = FastAPI(title="SQL Practice MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FREE_DAILY_SUBMISSIONS = 20
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# Precompute expected output for every problem once at startup so grading
# doesn't re-run the canonical query on every submission.
_EXPECTED_CACHE = {}


@app.on_event("startup")
def _startup():
    if db.DATABASE_URL:
        db.init_schema()
        problems_module.seed_if_empty()
        problems_module.mark_free_problems()
        for p in problems_module.list_all_live_problems():
            cols, rows, _ = sandbox.compute_expected_output(p)
            _EXPECTED_CACHE[p["id"]] = (cols, rows)


class SubmitRequest(BaseModel):
    problem_id: str
    query: str


class AskPhoenixRequest(BaseModel):
    problem_id: str
    current_query: str | None = None
    conversation: list[dict] = []
    question: str


class InterviewStartRequest(BaseModel):
    mode: str  # "personalized" | "generic"
    resume_text: str | None = None
    skip_intro: bool = False
    duration_minutes: int = 45
    persona: str = "neutral"  # "friendly" | "neutral" | "strict"


class InterviewAnswerRequest(BaseModel):
    session_id: str
    answer_text: str


class InterviewEndRequest(BaseModel):
    session_id: str


@app.get("/api/topics")
def api_topics():
    """Exposes the topic taxonomy so the frontend doesn't have to hand-
    duplicate topics.py's lists as a drift-prone JS array -- used to decide
    which interview-feedback "topics to study" pills can link into the
    practice bank (only GRADEABLE_TOPICS have matching problems)."""
    return {"gradeable": topics.GRADEABLE_TOPICS, "all": topics.ALL_TOPICS}


@app.get("/api/problems")
def api_list_problems(
    difficulty: str | None = None,
    tag: str | None = None,
    topic: str | None = None,
    x_user_id: str = Header(default=None),
    authorization: str | None = Header(default=None),
):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    tier = users_module.get_usage(user_id)["tier"]
    problems = list_problems_summary(difficulty=difficulty, tag=tag, topic=topic, user_id=user_id)
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

    con = duckdb.connect(":memory:", config={"enable_external_access": False})
    try:
        con.execute(p["schema_sql"])
        con.execute(p["seed_sql"])
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
    finally:
        con.close()

    return {
        "id": p["id"],
        "title": p["title"],
        "difficulty": p["difficulty"],
        "topic": p["topic"],
        "tags": p["tags"],
        "description": p["description"],
        "schema_sql": p["schema_sql"].strip(),
        "sample_tables": sample_tables,
    }


@app.get("/api/usage")
def api_usage(x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    u = users_module.get_usage(user_id)
    return {
        "user_id": user_id,
        "tier": u["tier"],
        "submissions_today": u["submissions"],
        "free_daily_submissions": FREE_DAILY_SUBMISSIONS,
        "interview_trial_used": u["interview_trial_used"],
    }


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


class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@app.post("/api/payments/create-order")
def api_payments_create_order(x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    if not user_id.startswith("clerk:"):
        raise HTTPException(401, "Sign in before upgrading -- Pro is tied to your account, not an anonymous browser id.")
    try:
        return payments.create_order(user_id)
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
    users_module.set_tier(user_id, "paid")
    return {"user_id": user_id, "tier": "paid"}


def _preview(columns, rows, limit=10):
    return {"columns": columns, "rows": rows[:limit]}


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

    result = {
        "correct": False,
        "error": None,
        "actual_preview": None,
        "expected_preview": None,
    }

    try:
        columns, rows, truncated = sandbox.run_query_against_problem(problem, req.query)
    except (sandbox.SqlValidationError, sandbox.SqlTimeoutError) as e:
        result["error"] = str(e)
    except duckdb.Error as e:
        result["error"] = f"SQL error: {e}"
    else:
        expected_columns, expected_rows = _EXPECTED_CACHE[problem["id"]]
        is_correct, diff = sandbox.compare_results(
            expected_columns, expected_rows, columns, rows, problem["order_matters"]
        )
        result["correct"] = is_correct
        result["actual_preview"] = _preview(columns, [[sandbox._normalize_cell(v) for v in r] for r in rows])
        if not is_correct:
            result["error"] = diff

    problems_module.record_submission(user_id, problem["id"], result["correct"])

    if not result["correct"]:
        expected_columns, expected_rows = _EXPECTED_CACHE[problem["id"]]
        result["expected_preview"] = _preview(
            expected_columns, [[sandbox._normalize_cell(v) for v in r] for r in expected_rows]
        )

    return result


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


INTRO_QUESTION = (
    "Let's get started. Could you briefly introduce yourself and walk me "
    "through your experience working with SQL and databases?"
)


FREE_TRIAL_DURATION_SECONDS = 10 * 60


def _require_paid_or_trial(u: dict) -> bool:
    """Returns True if this request is consuming the user's free interview
    trial (caller must then mark it used and cap the session's duration);
    False if they're paid (no trial needed); raises 402 if neither paid nor
    trial-eligible."""
    if u["tier"] == "paid":
        return False
    if not u["interview_trial_used"]:
        return True
    raise HTTPException(
        402,
        "You've used your free interview trial. Upgrade to Pro (₹199/mo) "
        "for unlimited mock interviews.",
    )


MAX_RESUME_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB -- a resume has no business being bigger


@app.post("/api/interview/parse-resume")
async def api_parse_resume(file: UploadFile = File(...), x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    u = users_module.get_usage(user_id)
    _require_paid_or_trial(u)  # trial consumption only happens at /start, not here

    file_bytes = await file.read(MAX_RESUME_UPLOAD_BYTES + 1)
    if len(file_bytes) > MAX_RESUME_UPLOAD_BYTES:
        raise HTTPException(413, "Resume file too large -- 5 MB max.")
    try:
        text = resume_parser.extract_text(file.filename, file_bytes)
    except resume_parser.UnsupportedResumeFormat as e:
        raise HTTPException(400, str(e))
    return {"resume_text": text}


@app.post("/api/interview/start")
def api_interview_start(req: InterviewStartRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    u = users_module.get_usage(user_id)
    is_trial = _require_paid_or_trial(u)

    if req.mode not in ("personalized", "generic"):
        raise HTTPException(400, "mode must be 'personalized' or 'generic'")
    if req.mode == "personalized" and not req.resume_text:
        raise HTTPException(400, "resume_text is required for a personalized interview")
    if req.persona not in ("friendly", "neutral", "strict"):
        raise HTTPException(400, "persona must be 'friendly', 'neutral', or 'strict'")

    session = interview.create_session(
        user_id=user_id,
        mode=req.mode,
        resume_text=req.resume_text,
        skip_intro=req.skip_intro,
        duration_seconds=req.duration_minutes * 60,
        is_trial=is_trial,
        persona=req.persona,
    )
    if is_trial:
        # Mark the trial used now, at session creation, not at completion --
        # otherwise an abandoned-and-restarted interview would let a free
        # user get multiple trials for the cost of one.
        users_module.mark_interview_trial_used(user_id)

    if req.skip_intro:
        try:
            result = llm.interview_turn(
                user_id=user_id,
                topics=interview.GENERIC_TOPICS,
                resume_text=req.resume_text,
                conversation=[],
                persona=session["persona"],
            )
        except Exception as e:
            raise HTTPException(502, f"AI interviewer unavailable right now ({e}).")
        question, topic, action, table_context = result["question"], result["topic"], result["action"], result["table_context"]
    else:
        question, topic, action, table_context = INTRO_QUESTION, "intro", "intro", None

    interview.record_turn(session, "assistant", question, topic)
    interview.update_topic_tracking(session, action, topic)
    interview.set_last_table_context(session, table_context)

    return {
        "session_id": session["session_id"],
        "question": question,
        "topic": topic,
        "action": action,
        "table_context": table_context,
        "remaining_seconds": interview.remaining_seconds(session),
        "duration_seconds": session["duration_seconds"],
    }


@app.post("/api/interview/answer")
def api_interview_answer(req: InterviewAnswerRequest, x_user_id: str = Header(default=None), authorization: str | None = Header(default=None)):
    user_id = auth.resolve_user_id(authorization, x_user_id)
    u = users_module.get_usage(user_id)
    _require_paid_or_trial(u)  # trial consumption only happens at /start, not here

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

    forced_topic = (
        interview.next_topic(session, interview.GENERIC_TOPICS)
        if interview.topic_cap_reached(session)
        else None
    )

    try:
        result = llm.interview_turn(
            user_id=user_id,
            topics=interview.GENERIC_TOPICS,
            resume_text=session["resume_text"],
            conversation=session["conversation"],
            current_topic=session["current_topic"],
            topic_turn_count=session["current_topic_turns"],
            forced_topic=forced_topic,
            persona=session["persona"],
        )
    except Exception as e:
        # Roll back the user turn we just recorded so a client retry after a
        # transient failure doesn't leave a duplicate in the transcript.
        interview.remove_last_turn(session)
        raise HTTPException(502, f"AI interviewer unavailable right now ({e}).")

    interview.record_turn(session, "assistant", result["question"], result["topic"])
    interview.update_topic_tracking(session, result["action"], result["topic"])
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
        "mode": session["mode"],
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

    if session["feedback"] is None:
        try:
            result = llm.interview_feedback(user_id=user_id, conversation=session["conversation"])
            feedback = result["report"]
        except Exception as e:
            raise HTTPException(502, f"Couldn't generate feedback report right now ({e}).")
    else:
        feedback = session["feedback"]

    interview.mark_ended(session, feedback)
    return {"feedback": feedback, "conversation": session["conversation"]}


class GenerateBatchRequest(BaseModel):
    count: int = 5
    topics: list[str] | None = None  # defaults to all gradeable topics if omitted


def _require_admin(x_admin_token: str | None):
    # hmac.compare_digest instead of != -- plain string comparison short-
    # circuits on the first differing byte, which leaks how many
    # characters of the token you got right via response-time
    # differences. Doesn't matter much on Render's jittery network, but
    # it's a one-line fix for a real (if low-severity) timing side channel.
    if not ADMIN_TOKEN or not x_admin_token or not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(403, "Invalid or missing admin token.")


@app.post("/api/admin/problems/generate-batch")
def api_admin_generate_batch(req: GenerateBatchRequest, x_admin_token: str = Header(default=None)):
    """
    Drafts a new batch of practice problems via the LLM and stores the
    ones that pass validation as pending_review -- nothing here ever goes
    live without a human approving it via /approve below. This is what the
    weekly cron job (render.yaml) calls once 45 days have elapsed since
    the last batch; it's also callable by hand for an out-of-cycle batch.
    """
    _require_admin(x_admin_token)
    target_topics = req.topics or topics.GRADEABLE_TOPICS

    try:
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
            problem_id = problems_module.insert_pending_draft(draft)
            inserted.append(problem_id)
        except problems_module.InvalidDraftProblem as e:
            skipped.append({"title": draft.get("title", "<untitled>"), "reason": str(e)})

    problems_module.mark_batch_generated()
    return {"inserted": inserted, "skipped": skipped, "usage": result["usage"]}


@app.get("/api/admin/problems/pending")
def api_admin_list_pending(x_admin_token: str = Header(default=None)):
    _require_admin(x_admin_token)
    return {"problems": problems_module.list_pending_problems()}


@app.post("/api/admin/problems/{problem_id}/approve")
def api_admin_approve(problem_id: str, x_admin_token: str = Header(default=None)):
    _require_admin(x_admin_token)
    ok = problems_module.approve_problem(problem_id)
    if not ok:
        raise HTTPException(404, "Pending problem not found.")
    # Warm this problem's expected-output cache immediately -- otherwise
    # the first submission against it would KeyError until next restart.
    approved = problems_module.get_problem(problem_id)
    cols, rows, _ = sandbox.compute_expected_output(approved)
    _EXPECTED_CACHE[problem_id] = (cols, rows)
    return {"id": problem_id, "status": "live"}


@app.post("/api/admin/problems/{problem_id}/reject")
def api_admin_reject(problem_id: str, x_admin_token: str = Header(default=None)):
    _require_admin(x_admin_token)
    ok = problems_module.reject_problem(problem_id)
    if not ok:
        raise HTTPException(404, "Pending problem not found.")
    return {"id": problem_id, "status": "rejected"}


@app.get("/api/admin/cadence")
def api_admin_cadence(x_admin_token: str = Header(default=None)):
    _require_admin(x_admin_token)
    return {"last_batch_generated_at": problems_module.get_last_batch_generated_at()}


class SetTierRequest(BaseModel):
    user_id: str
    tier: str


@app.post("/api/admin/set-tier")
def api_admin_set_tier(req: SetTierRequest, x_admin_token: str = Header(default=None)):
    """Manually flips a user's tier -- for testing, and for handling a
    refund/dispute by hand later since there's no self-serve downgrade."""
    _require_admin(x_admin_token)
    if req.tier not in ("free", "paid"):
        raise HTTPException(400, "tier must be 'free' or 'paid'")
    users_module.set_tier(req.user_id, req.tier)
    return {"user_id": req.user_id, "tier": req.tier}
