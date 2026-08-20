"""
Single isolated entry point for all LLM calls.

Everything provider-specific lives in this one file: swap OpenAI-compatible
endpoints (Groq, Gemini's OpenAI-compat layer, Together, local vLLM, etc.)
by changing env vars only -- no other file should ever import an LLM SDK
directly.

Every call is appended to usage_log.jsonl as one JSON object per line, so
cost-per-student and cost-per-problem can be computed later with a simple
`pandas.read_json(..., lines=True)` or `duckdb.sql("select * from
read_json_auto('usage_log.jsonl')")`.
"""

import os
import json
import time
from pathlib import Path

import requests

LLM_API_BASE = os.environ.get("LLM_API_BASE", "https://api.groq.com/openai/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-20b")

USAGE_LOG_PATH = Path(__file__).parent / "usage_log.jsonl"

# Rough cost table, USD per 1M tokens, for back-of-envelope cost tracking.
# Update to match whatever provider/model you actually configure.
COST_PER_1M_TOKENS = {
    "prompt": 0.05,
    "completion": 0.08,
}

SYSTEM_PROMPT = (
    "You are a patient SQL tutor helping an Indian IT professional preparing "
    "for job interviews. A student submitted a wrong SQL query. Explain, in "
    "plain simple language, what mistake they made and how to think about "
    "fixing it. Do NOT just hand them the corrected query verbatim -- guide "
    "them conceptually, in 3-6 sentences. Be encouraging but direct about "
    "the error.\n\n"
    "The student may ask follow-up questions. Only answer questions about "
    "this specific SQL problem, their query, or general SQL/database "
    "concepts directly relevant to it (e.g. how JOINs, NULLs, GROUP BY, "
    "window functions work). If a follow-up is unrelated to SQL or this "
    "problem (e.g. general trivia, other programming languages, personal "
    "questions), politely decline in one sentence and redirect the student "
    "back to the SQL problem at hand -- do not answer the off-topic "
    "question."
)


def _build_user_prompt(problem: dict, student_query: str, expected_preview: dict, actual_preview: dict, error: str | None) -> str:
    parts = [
        f"Problem: {problem['title']}",
        f"Description: {problem['description']}",
        f"Student's query:\n{student_query}",
    ]
    if error:
        parts.append(f"The query failed with this database error:\n{error}")
    else:
        parts.append(f"Expected output (columns + first rows): {json.dumps(expected_preview)}")
        parts.append(f"Student's actual output (columns + first rows): {json.dumps(actual_preview)}")
    parts.append(
        "Explain what's wrong with the student's query and what concept "
        "they should revisit."
    )
    return "\n\n".join(parts)


def _log_usage(entry: dict):
    entry["logged_at"] = time.time()
    with open(USAGE_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _call_chat(*, user_id: str, problem_id: str, messages: list[dict], max_tokens: int = 500, json_mode: bool = False) -> dict:
    """
    Shared low-level call: posts `messages` to the configured
    OpenAI-compatible chat completions endpoint, logs usage, and returns
    {"reply": str, "usage": {...}}. Raises RuntimeError if LLM_API_KEY isn't
    configured or the HTTP call fails -- callers should catch this and
    degrade gracefully rather than 500ing the request.

    `json_mode` requests the provider's structured-output mode (OpenAI-
    compatible `response_format: {"type": "json_object"}`) for callers that
    parse the reply as JSON -- without it, some models (e.g. Groq's
    gpt-oss) can misinterpret "respond with only a JSON object" instructions
    as a request to invoke a tool, which the API then rejects.
    """
    if not LLM_API_KEY:
        raise RuntimeError(
            "LLM_API_KEY is not set. Configure LLM_API_BASE / LLM_API_KEY / "
            "LLM_MODEL env vars to enable the AI tutor."
        )

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    resp = requests.post(
        f"{LLM_API_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} error from LLM provider: {resp.text[:500]}")
    data = resp.json()

    reply = data["choices"][0]["message"]["content"].strip()
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
    estimated_cost = (
        prompt_tokens / 1_000_000 * COST_PER_1M_TOKENS["prompt"]
        + completion_tokens / 1_000_000 * COST_PER_1M_TOKENS["completion"]
    )

    _log_usage({
        "user_id": user_id,
        "problem_id": problem_id,
        "model": LLM_MODEL,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": round(estimated_cost, 6),
    })

    return {
        "reply": reply,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
        },
    }


def _call_chat_with_retry(*, max_retries: int = 2, retry_delay_seconds: float = 0.6, **kwargs) -> dict:
    """
    Wraps _call_chat with a few retries for json_mode calls specifically --
    observed in practice to occasionally fail with a 400 from Groq's own
    structured-output validator (empty generation, a misfired tool call,
    truncation) even when the request itself is well-formed. These read as
    transient generation hiccups rather than a real prompt problem (retrying
    the identical request has succeeded every time so far), so retry a
    couple of times before letting the error surface to the user.
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return _call_chat(**kwargs)
        except RuntimeError as e:
            last_error = e
            if not kwargs.get("json_mode") or attempt == max_retries:
                raise
            time.sleep(retry_delay_seconds)
    raise last_error


def get_explanation(
    *,
    user_id: str,
    problem: dict,
    student_query: str,
    expected_preview: dict,
    actual_preview: dict,
    error: str | None = None,
) -> dict:
    """
    Explains a wrong (or erroring) submission. Returns:
        {"explanation": str, "usage": {...}}
    """
    user_prompt = _build_user_prompt(problem, student_query, expected_preview, actual_preview, error)
    result = _call_chat(
        user_id=user_id,
        problem_id=problem["id"],
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    return {"explanation": result["reply"], "usage": result["usage"]}


def ask_followup(
    *,
    user_id: str,
    problem: dict,
    student_query: str,
    expected_preview: dict,
    actual_preview: dict,
    error: str | None,
    conversation: list[dict],
    question: str,
) -> dict:
    """
    Continues the tutoring conversation with a free-form follow-up question
    from the student. `conversation` is the prior turns as
    [{"role": "assistant"|"user", "content": ...}, ...], starting with the
    initial explanation as the first assistant turn. Returns:
        {"answer": str, "usage": {...}}
    """
    initial_context = _build_user_prompt(problem, student_query, expected_preview, actual_preview, error)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": initial_context},
    ]
    messages.extend(conversation)
    messages.append({"role": "user", "content": question})

    result = _call_chat(user_id=user_id, problem_id=problem["id"], messages=messages)
    return {"answer": result["reply"], "usage": result["usage"]}


def _interview_system_prompt(
    topics: list[str],
    resume_text: str | None,
    current_topic: str | None = None,
    topic_turn_count: int = 0,
    max_turns_per_topic: int = 3,
    forced_topic: str | None = None,
) -> str:
    resume_block = ""
    if resume_text:
        resume_block = (
            "The candidate's resume/background (use it to ground your topic "
            "choices and follow-ups in their actual experience, but still "
            "cover core SQL fundamentals -- don't just ask about their resume "
            "verbatim):\n"
            f"{resume_text[:4000]}\n\n"
        )

    topic_budget_block = ""
    if forced_topic:
        # Cap already reached -- this is not a judgment call, it's a direct
        # instruction. Leaving it as "decide whether to switch" repeatedly
        # let the model just keep following up past the limit.
        topic_budget_block = (
            f"The topic budget for '{current_topic}' is used up. Do NOT ask "
            f"anything more about it. Your action MUST be \"switch_topic\" and "
            f"your topic MUST be exactly \"{forced_topic}\" -- write an "
            f"opening question for that new topic now.\n\n"
        )
    elif current_topic:
        topic_budget_block = (
            f"You are currently on '{current_topic}' -- {topic_turn_count} "
            f"question(s) asked so far, {max_turns_per_topic} max before "
            "you must move to a new topic. Keep that budget in mind when "
            "choosing follow_up/probe vs switch_topic.\n\n"
        )

    return (
        "You are conducting a live, spoken SQL technical interview for a "
        "candidate applying to a data/analytics role in India. Ask ONE "
        "question at a time, in natural spoken language -- no markdown, no "
        "bullet points, no code blocks, since your question will be read "
        "aloud by text-to-speech. Keep each question to 1-3 sentences.\n\n"
        f"{resume_block}"
        f"{topic_budget_block}"
        f"Topics to cover across the interview: {', '.join(topics)}.\n\n"
        "Whenever your question refers to a table (e.g. 'suppose you have a "
        "table called orders...'), you MUST invent a concrete schema and a "
        "handful of sample rows for it, and put them in `table_context` -- "
        "never leave the candidate to imagine column names or data on their "
        "own, they need to actually see it on screen. Reuse the SAME table "
        "(set table_context to null) for follow_up or probe questions still "
        "about that table; only invent a new table_context when you "
        "switch_topic to a scenario that needs a different table, or for "
        "the very first question.\n\n"
        "After the candidate's most recent answer, decide exactly one of:\n"
        "- follow_up: there is a specific gap, vagueness, or mistake in "
        "their answer -- ask a targeted follow-up on the SAME topic to "
        "clarify or correct it. A repeat-request for a suspected "
        "transcription glitch (see below) is also a follow_up on the SAME "
        "topic, with table_context left null.\n"
        "- probe: their answer was solid and there is room to go deeper on "
        "the SAME topic (edge cases, performance, trade-offs, real-world "
        "scenarios).\n"
        "- switch_topic: their answer was sufficient, or they are clearly "
        "stuck after a follow-up/probe already -- move to a new topic from "
        "the list above that has not been covered yet.\n\n"
        "Only ask about SQL, databases, and data engineering concepts. If "
        "the candidate goes off-topic, gently redirect back to the "
        "interview.\n\n"
        "The candidate's answers come from speech-to-text, which can "
        "occasionally produce actual gibberish (word salad, mid-word cuts, "
        "sounds transcribed as random unrelated words). A short-but-coherent "
        "answer like \"yeah that's all\", \"no\", or \"I'm not sure\" is a "
        "REAL, complete answer, not a transcription glitch -- never treat "
        "brevity alone as a reason to ask them to repeat themselves. Only "
        "suspect a transcription glitch when the text is genuinely "
        "unintelligible (not just short or unhelpful).\n\n"
        "If you do suspect a transcription glitch, your ENTIRE response "
        "must be asking them to repeat -- e.g. \"Sorry, I didn't quite catch "
        "that, could you say that again?\" -- and NOTHING else. Never combine "
        "a repeat-request with a new question in the same turn; that asks "
        "two things at once and breaks the one-question-at-a-time rule. "
        "Wait for their next answer before asking anything new. Never just "
        "repeat your previous question verbatim, though -- vary your "
        "wording.\n\n"
        "Respond with ONLY a JSON object, no other text, no markdown code "
        'fences: {"action": "follow_up"|"probe"|"switch_topic", "topic": '
        '"<topic name>", "question": "<your next spoken question>", '
        '"table_context": null | {"table_name": "<name>", "schema": '
        '"<CREATE TABLE ... statement as one line>", "sample_rows": '
        '"<a small markdown table of 3-6 example rows, columns separated '
        'by | >"}}'
    )


def _parse_json_reply(reply: str) -> dict:
    """Models sometimes wrap JSON in ```json fences despite instructions not to -- strip those before parsing."""
    text = reply.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def interview_turn(
    *,
    user_id: str,
    topics: list[str],
    resume_text: str | None,
    conversation: list[dict],
    current_topic: str | None = None,
    topic_turn_count: int = 0,
    forced_topic: str | None = None,
) -> dict:
    """
    Decides the next interview question. `conversation` is the full turn
    history so far as [{"role": "assistant"|"user", "content": ...}, ...]
    (may be empty for the very first question). `current_topic`/
    `topic_turn_count` let the prompt enforce a max-turns-per-topic budget.
    `forced_topic`, when set, means the caller has already decided (based on
    MAX_TURNS_PER_TOPIC) that the topic MUST switch now -- the model isn't
    asked to judge that, only to phrase the new topic's opening question;
    the action/topic in the return value are hard-set to match rather than
    trusting the model to have echoed them back correctly, since it's been
    observed to keep returning follow_up past its instructed limit when
    that decision was left to its judgment. Returns:
        {"action": str, "topic": str, "question": str, "usage": {...}}
    Falls back to a generic switch_topic question if the model's reply isn't
    valid JSON, so a single malformed response doesn't break the interview.
    """
    messages = [{"role": "system", "content": _interview_system_prompt(
        topics, resume_text, current_topic, topic_turn_count, forced_topic=forced_topic,
    )}]
    if conversation:
        # Chat APIs only accept {role, content} -- strip our extra "topic" bookkeeping field.
        messages.extend({"role": t["role"], "content": t["content"]} for t in conversation)
    else:
        messages.append({"role": "user", "content": "Begin the interview with the first question."})

    result = _call_chat_with_retry(user_id=user_id, problem_id="mock-interview", messages=messages, max_tokens=700, json_mode=True)
    try:
        parsed = _parse_json_reply(result["reply"])
        return {
            "action": "switch_topic" if forced_topic else parsed.get("action", "switch_topic"),
            "topic": forced_topic or parsed.get("topic", topics[0]),
            "question": parsed["question"],
            "table_context": parsed.get("table_context"),
            "usage": result["usage"],
        }
    except (json.JSONDecodeError, KeyError):
        return {
            "action": "switch_topic",
            "topic": forced_topic or topics[0],
            "question": result["reply"],
            "table_context": None,
            "usage": result["usage"],
        }


FEEDBACK_SYSTEM_PROMPT = (
    "You are an experienced SQL interviewer writing a feedback report after "
    "a mock interview. Review the full transcript and produce a structured, "
    "honest but encouraging assessment for the candidate.\n\n"
    "Respond with ONLY a JSON object, no other text, no markdown code "
    'fences: {"overall_summary": "<2-3 sentence overall impression>", '
    '"strengths": ["<point>", ...], "weaknesses": ["<point>", ...], '
    '"topics_to_study": ["<topic>", ...], '
    '"rough_level": "beginner"|"intermediate"|"advanced"}'
)


def interview_feedback(*, user_id: str, conversation: list[dict]) -> dict:
    """
    Generates the end-of-interview feedback report from the full transcript.
    Returns {"report": {...parsed fields...}, "usage": {...}}.
    """
    transcript = "\n".join(f"{t['role'].upper()}: {t['content']}" for t in conversation)
    messages = [
        {"role": "system", "content": FEEDBACK_SYSTEM_PROMPT},
        {"role": "user", "content": f"Interview transcript:\n\n{transcript}"},
    ]
    result = _call_chat_with_retry(user_id=user_id, problem_id="mock-interview-feedback", messages=messages, max_tokens=1200, json_mode=True)
    try:
        report = _parse_json_reply(result["reply"])
    except json.JSONDecodeError:
        report = {
            "overall_summary": result["reply"],
            "strengths": [],
            "weaknesses": [],
            "topics_to_study": [],
            "rough_level": "intermediate",
        }
    return {"report": report, "usage": result["usage"]}
