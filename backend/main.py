"""
FastAPI backend for the SQL practice MVP.

Run with:
    uvicorn main:app --reload --port 8000

Env vars (see llm.py):
    LLM_API_BASE, LLM_API_KEY, LLM_MODEL

Tiering (MVP-simple, in-memory, resets on restart -- swap for a real DB /
auth system before launch):
    - Every request carries an `X-User-Id` header (frontend generates a
      random one and stores it in localStorage). No real auth in the MVP.
    - Free tier: FREE_DAILY_SUBMISSIONS submissions/day, FREE_DAILY_EXPLANATIONS
      AI explanations/day.
    - Paid tier: unlimited explanations. There's no real payment flow yet --
      POST /api/dev/upgrade flips a user to paid, standing in for whatever
      payment webhook (Razorpay etc.) would call it for real.
"""

import datetime
import uuid
from collections import defaultdict

from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import duckdb

from problems import PROBLEMS, get_problem, list_problems_summary
import sandbox
import llm
import interview
import resume_parser

app = FastAPI(title="SQL Practice MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FREE_DAILY_SUBMISSIONS = 20
FREE_DAILY_EXPLANATIONS = 3

# user_id -> {"tier": "free"|"paid", "date": "YYYY-MM-DD", "submissions": int, "explanations": int}
_USAGE = defaultdict(lambda: {"tier": "free", "date": None, "submissions": 0, "explanations": 0})

# Precompute expected output for every problem once at startup so grading
# doesn't re-run the canonical query on every submission.
_EXPECTED_CACHE = {}


@app.on_event("startup")
def _warm_expected_cache():
    for p in PROBLEMS:
        cols, rows, _ = sandbox.compute_expected_output(p)
        _EXPECTED_CACHE[p["id"]] = (cols, rows)


def _today():
    return datetime.date.today().isoformat()


def _get_usage(user_id: str):
    u = _USAGE[user_id]
    today = _today()
    if u["date"] != today:
        u["date"] = today
        u["submissions"] = 0
        u["explanations"] = 0
    return u


class SubmitRequest(BaseModel):
    problem_id: str
    query: str
    want_explanation: bool = True


class FollowupRequest(BaseModel):
    problem_id: str
    student_query: str
    expected_preview: dict
    actual_preview: dict | None = None
    error: str | None = None
    conversation: list[dict]
    question: str


class InterviewStartRequest(BaseModel):
    mode: str  # "personalized" | "generic"
    resume_text: str | None = None
    skip_intro: bool = False
    duration_minutes: int = 45


class InterviewAnswerRequest(BaseModel):
    session_id: str
    answer_text: str


class InterviewEndRequest(BaseModel):
    session_id: str


@app.get("/api/problems")
def api_list_problems(difficulty: str | None = None, tag: str | None = None):
    problems = list_problems_summary()
    if difficulty:
        problems = [p for p in problems if p["difficulty"] == difficulty]
    if tag:
        problems = [p for p in problems if tag in p["tags"]]
    return {"problems": problems}


@app.get("/api/problems/{problem_id}")
def api_get_problem(problem_id: str):
    p = get_problem(problem_id)
    if not p:
        raise HTTPException(404, "Problem not found")

    con = duckdb.connect(":memory:")
    try:
        con.execute(p["schema_sql"])
        con.execute(p["seed_sql"])
        table_names = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
        sample_tables = {}
        for t in table_names:
            cols = [d[0] for d in con.execute(f"SELECT * FROM {t} LIMIT 0").description]
            rows = con.execute(f"SELECT * FROM {t} LIMIT 15").fetchall()
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
        "tags": p["tags"],
        "description": p["description"],
        "schema_sql": p["schema_sql"].strip(),
        "sample_tables": sample_tables,
    }


@app.get("/api/usage")
def api_usage(x_user_id: str = Header(default=None)):
    user_id = x_user_id or str(uuid.uuid4())
    u = _get_usage(user_id)
    return {
        "user_id": user_id,
        "tier": u["tier"],
        "submissions_today": u["submissions"],
        "explanations_today": u["explanations"],
        "free_daily_submissions": FREE_DAILY_SUBMISSIONS,
        "free_daily_explanations": FREE_DAILY_EXPLANATIONS,
    }


@app.post("/api/dev/upgrade")
def api_dev_upgrade(x_user_id: str = Header(default=None)):
    """Stand-in for a real payment webhook. Flips the user to the paid tier."""
    if not x_user_id:
        raise HTTPException(400, "X-User-Id header required")
    u = _get_usage(x_user_id)
    u["tier"] = "paid"
    return {"user_id": x_user_id, "tier": "paid"}


def _preview(columns, rows, limit=10):
    return {"columns": columns, "rows": rows[:limit]}


@app.post("/api/submit")
def api_submit(req: SubmitRequest, x_user_id: str = Header(default=None)):
    user_id = x_user_id or str(uuid.uuid4())
    u = _get_usage(user_id)

    if u["submissions"] >= FREE_DAILY_SUBMISSIONS and u["tier"] == "free":
        raise HTTPException(
            429,
            f"Daily free submission limit ({FREE_DAILY_SUBMISSIONS}) reached. "
            "Upgrade to keep practicing today.",
        )

    problem = get_problem(req.problem_id)
    if not problem:
        raise HTTPException(404, "Problem not found")

    u["submissions"] += 1

    result = {
        "correct": False,
        "error": None,
        "actual_preview": None,
        "expected_preview": None,
        "explanation": None,
        "explanation_available": False,
        "llm_usage": None,
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

    if result["correct"]:
        return result

    # Wrong (or errored) -- only now do we consider calling the LLM.
    expected_columns, expected_rows = _EXPECTED_CACHE[problem["id"]]
    result["expected_preview"] = _preview(
        expected_columns, [[sandbox._normalize_cell(v) for v in r] for r in expected_rows]
    )

    can_explain = u["tier"] == "paid" or u["explanations"] < FREE_DAILY_EXPLANATIONS
    result["explanation_available"] = can_explain

    if not req.want_explanation:
        # Student has the AI tutor toggled off -- don't spend an LLM call
        # (or a quota slot) they didn't ask for.
        result["explanation"] = None
        return result

    if not can_explain:
        result["explanation"] = None
        return result

    try:
        llm_result = llm.get_explanation(
            user_id=user_id,
            problem=problem,
            student_query=req.query,
            expected_preview=result["expected_preview"],
            actual_preview=result["actual_preview"] or {},
            error=result["error"] if result["actual_preview"] is None else None,
        )
        result["explanation"] = llm_result["explanation"]
        result["llm_usage"] = llm_result["usage"]
        u["explanations"] += 1
    except Exception as e:
        result["explanation"] = None
        result["explanation_error"] = f"AI explanation unavailable right now ({e})."

    return result


@app.post("/api/ask-followup")
def api_ask_followup(req: FollowupRequest, x_user_id: str = Header(default=None)):
    """
    Lets a student ask a free-form follow-up question after an AI
    explanation, e.g. "why does NULL break GROUP BY like that?". Counts
    against the same daily AI-tutor quota as the initial explanation --
    it's the same cost category, just a different shape of question.
    """
    user_id = x_user_id or str(uuid.uuid4())
    u = _get_usage(user_id)

    problem = get_problem(req.problem_id)
    if not problem:
        raise HTTPException(404, "Problem not found")

    if u["tier"] != "paid" and u["explanations"] >= FREE_DAILY_EXPLANATIONS:
        raise HTTPException(
            429,
            f"Daily free AI tutor limit ({FREE_DAILY_EXPLANATIONS}) reached. "
            "Upgrade to keep asking questions today.",
        )

    try:
        llm_result = llm.ask_followup(
            user_id=user_id,
            problem=problem,
            student_query=req.student_query,
            expected_preview=req.expected_preview,
            actual_preview=req.actual_preview or {},
            error=req.error,
            conversation=req.conversation,
            question=req.question,
        )
        u["explanations"] += 1
        return {"answer": llm_result["answer"], "llm_usage": llm_result["usage"]}
    except Exception as e:
        raise HTTPException(502, f"AI tutor unavailable right now ({e}).")


INTRO_QUESTION = (
    "Let's get started. Could you briefly introduce yourself and walk me "
    "through your experience working with SQL and databases?"
)


def _require_paid(u: dict):
    if u["tier"] != "paid":
        raise HTTPException(
            402,
            "Mock interviews are a Pro feature (₹199/mo) -- the interview "
            "calls the AI continuously for up to 45 minutes, which costs a "
            "lot more than the occasional wrong-answer explanation.",
        )


@app.post("/api/interview/parse-resume")
async def api_parse_resume(file: UploadFile = File(...), x_user_id: str = Header(default=None)):
    user_id = x_user_id or str(uuid.uuid4())
    u = _get_usage(user_id)
    _require_paid(u)

    file_bytes = await file.read()
    try:
        text = resume_parser.extract_text(file.filename, file_bytes)
    except resume_parser.UnsupportedResumeFormat as e:
        raise HTTPException(400, str(e))
    return {"resume_text": text}


@app.post("/api/interview/start")
def api_interview_start(req: InterviewStartRequest, x_user_id: str = Header(default=None)):
    user_id = x_user_id or str(uuid.uuid4())
    u = _get_usage(user_id)
    _require_paid(u)

    if req.mode not in ("personalized", "generic"):
        raise HTTPException(400, "mode must be 'personalized' or 'generic'")
    if req.mode == "personalized" and not req.resume_text:
        raise HTTPException(400, "resume_text is required for a personalized interview")

    session = interview.create_session(
        user_id=user_id,
        mode=req.mode,
        resume_text=req.resume_text,
        skip_intro=req.skip_intro,
        duration_seconds=req.duration_minutes * 60,
    )

    if req.skip_intro:
        try:
            result = llm.interview_turn(
                user_id=user_id,
                topics=interview.GENERIC_TOPICS,
                resume_text=req.resume_text,
                conversation=[],
            )
        except Exception as e:
            raise HTTPException(502, f"AI interviewer unavailable right now ({e}).")
        question, topic, action, table_context = result["question"], result["topic"], result["action"], result["table_context"]
    else:
        question, topic, action, table_context = INTRO_QUESTION, "intro", "intro", None

    interview.record_turn(session, "assistant", question, topic)
    interview.update_topic_tracking(session, action, topic)

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
def api_interview_answer(req: InterviewAnswerRequest, x_user_id: str = Header(default=None)):
    user_id = x_user_id or str(uuid.uuid4())
    u = _get_usage(user_id)
    _require_paid(u)

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

    try:
        result = llm.interview_turn(
            user_id=user_id,
            topics=interview.GENERIC_TOPICS,
            resume_text=session["resume_text"],
            conversation=session["conversation"],
            current_topic=session["current_topic"],
            topic_turn_count=session["current_topic_turns"],
        )
    except Exception as e:
        # Roll back the user turn we just recorded so a client retry after a
        # transient failure doesn't leave a duplicate in the transcript.
        session["conversation"].pop()
        raise HTTPException(502, f"AI interviewer unavailable right now ({e}).")

    interview.record_turn(session, "assistant", result["question"], result["topic"])
    interview.update_topic_tracking(session, result["action"], result["topic"])

    return {
        "time_up": False,
        "session_id": session["session_id"],
        "question": result["question"],
        "topic": result["topic"],
        "action": result["action"],
        "table_context": result["table_context"],
        "remaining_seconds": interview.remaining_seconds(session),
    }


@app.post("/api/interview/end")
def api_interview_end(req: InterviewEndRequest, x_user_id: str = Header(default=None)):
    user_id = x_user_id or str(uuid.uuid4())
    session = interview.get_session(req.session_id)
    if not session or session["user_id"] != user_id:
        raise HTTPException(404, "Interview session not found")

    if session["feedback"] is None:
        try:
            result = llm.interview_feedback(user_id=user_id, conversation=session["conversation"])
            session["feedback"] = result["report"]
        except Exception as e:
            raise HTTPException(502, f"Couldn't generate feedback report right now ({e}).")

    session["ended"] = True
    return {"feedback": session["feedback"], "conversation": session["conversation"]}
