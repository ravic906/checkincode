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

import topics

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

def _ask_phoenix_system_prompt(track: str = "sql") -> str:
    is_python = track == "python"
    subject = "Python" if is_python else "SQL"
    unit = "function" if is_python else "query"
    unit_cap = "Function" if is_python else "Query"
    example_snippet = (
        "\"a list comprehension filters and transforms in one pass\""
        if is_python else "\"LAG lets you look back one row\""
    )
    final_thing = "complete, final function" if is_python else "one assembled final SELECT statement"
    off_topic_examples = "general trivia, other programming languages, personal questions"

    return (
        f"You are Phoenix, a patient {subject} tutor helping a candidate "
        "preparing for job interviews. The student is looking "
        f"at a specific {subject} practice problem and can ask you anything "
        "about it at any point -- how to approach it, what a concept means, "
        f"why their in-progress {unit} might be wrong, or general {subject} "
        "concepts directly relevant to it. They may not have submitted (or "
        "even attempted) an answer yet.\n\n"
        "There are exactly two modes here, and you must pick the right one "
        "-- getting this wrong in either direction is a real failure, not "
        "just an unhelpful answer:\n\n"
        f"MODE 1 -- GUIDE (the default). Applies to \"how do I solve this?\", "
        "\"walk me through it\", \"what am I missing?\", or any question "
        f"that doesn't unambiguously ask for the finished {unit}. In this "
        f"mode, do NOT give away the solution -- and that means more than "
        f"just not writing one {final_thing}. Handing over every "
        "individual piece one at a time is the SAME thing as giving the "
        f"answer, just split up. Describe what a function/concept does "
        f"and why it's relevant IN WORDS (e.g. {example_snippet}), not as "
        "a literal ready-to-use snippet for THIS problem's specific case.\n\n"
        f"MODE 2 -- COMPLY FULLY. Applies the moment they unambiguously ask "
        "for the finished answer -- phrases like \"just give me the full "
        "answer\", \"show me the solution\", \"write it out for me\", "
        "\"give me the code\", or similar direct requests. When this "
        f"happens you MUST write the complete, correct {unit} for THIS "
        "problem, in full -- do not redirect back to guidance, do not "
        "partially comply, do not repeat Mode 1's restraint out of an "
        "abundance of caution. Refusing a direct, explicit request like "
        "this is just as wrong as giving away the answer unprompted; "
        f"honor it plainly. The same applies if they explicitly ask you to "
        f"review, fix, or correct {subject} code they themselves already "
        "wrote (e.g. \"what's wrong with my code?\", \"fix this for me\") "
        "-- point at what's off directly, including showing corrected "
        "code, since they're asking about their own work, not asking you "
        "to solve the problem for them from scratch. But if they just "
        "share their in-progress code alongside an open question without "
        "explicitly asking for a fix (e.g. \"why doesn't this work?\" with "
        f"no further ask), that's still MODE 1 -- describe the gap in "
        "words and let them apply the fix themselves.\n\n"
        "Keep answers conversational and to the point -- a few short "
        "paragraphs at most, like a tutor actually talking to a student, "
        "not a structured document. Don't reach for markdown headers, "
        "multiple sections, or a numbered step-by-step build-up by "
        "default; use a short list only when it genuinely helps, not as "
        "the default shape of every answer.\n\n"
        f"Only answer questions about this specific {subject} problem, "
        f"their {unit}, or general {subject} concepts directly relevant to "
        f"it. If a question is unrelated to {subject} or this problem "
        f"(e.g. {off_topic_examples}), politely decline in one sentence "
        "and redirect the student back to the problem at hand -- do not "
        "answer the off-topic question."
    )


def _build_ask_phoenix_context(problem: dict, current_query: str | None) -> str:
    if problem.get("track") == "python":
        parts = [
            f"Problem: {problem['title']}",
            f"Description: {problem['description']}",
            f"Starter code:\n{problem['starter_code']}",
        ]
        if current_query and current_query.strip():
            parts.append(f"Student's current in-progress code (not yet submitted):\n{current_query}")
        return "\n\n".join(parts)

    parts = [
        f"Problem: {problem['title']}",
        f"Description: {problem['description']}",
        f"Schema:\n{problem['schema_sql']}",
    ]
    if current_query and current_query.strip():
        parts.append(f"Student's current in-progress query (not yet submitted):\n{current_query}")
    return "\n\n".join(parts)


def _log_usage(entry: dict):
    entry["logged_at"] = time.time()
    with open(USAGE_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _call_chat(*, user_id: str, problem_id: str, messages: list[dict], max_tokens: int = 500, json_mode: bool = False, timeout: int = 30, temperature: float = 0.3) -> dict:
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
        "temperature": temperature,
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
        timeout=timeout,
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
        "finish_reason": data["choices"][0].get("finish_reason"),
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
        },
    }


def _call_chat_complete(*, max_continuations: int = 2, **kwargs) -> dict:
    """
    Like _call_chat, but guarantees the reply is never silently cut off by
    max_tokens: if finish_reason is "length", automatically asks the model
    to continue and stitches the replies together, rather than just hoping
    a bigger max_tokens is enough. Plain-text replies only -- concatenating
    two truncated JSON fragments wouldn't parse, so json_mode callers
    should keep using _call_chat_with_retry instead.
    """
    messages = list(kwargs.pop("messages"))
    reply_parts = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}
    for _ in range(max_continuations + 1):
        result = _call_chat_with_retry(messages=messages, **kwargs)
        reply_parts.append(result["reply"])
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            usage_total[key] += result["usage"].get(key, 0)
        usage_total["estimated_cost_usd"] += result["usage"].get("estimated_cost_usd", 0.0)
        if result.get("finish_reason") != "length":
            break
        messages = messages + [
            {"role": "assistant", "content": result["reply"]},
            {"role": "user", "content": "Continue exactly where you left off -- no repetition, no re-introduction."},
        ]
    usage_total["estimated_cost_usd"] = round(usage_total["estimated_cost_usd"], 6)
    return {"reply": "".join(reply_parts), "usage": usage_total}


def _call_chat_with_retry(*, max_retries: int = 2, retry_delay_seconds: float = 0.6, rate_limit_delay_seconds: float = 5.0, **kwargs) -> dict:
    """
    Wraps _call_chat with a few retries for json_mode calls specifically --
    observed in practice to occasionally fail with a 400 from Groq's own
    structured-output validator (empty generation, a misfired tool call,
    truncation) even when the request itself is well-formed. These read as
    transient generation hiccups rather than a real prompt problem (retrying
    the identical request has succeeded every time so far), so retry a
    couple of times before letting the error surface to the user.

    A 429 (provider TPM rate limit) is a different kind of transient error
    -- it needs several seconds for the token bucket to refill, not the
    short delay above meant for generation hiccups, so it gets its own
    longer backoff and retries regardless of json_mode (a rate limit hits
    every call type, not just structured-output ones).
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return _call_chat(**kwargs)
        except RuntimeError as e:
            last_error = e
            if attempt == max_retries:
                raise
            if str(e).startswith("429"):
                time.sleep(rate_limit_delay_seconds)
            elif kwargs.get("json_mode"):
                time.sleep(retry_delay_seconds)
            else:
                raise
    raise last_error


def ask_phoenix(
    *,
    user_id: str,
    problem: dict,
    current_query: str | None,
    conversation: list[dict],
    question: str,
) -> dict:
    """
    Open-ended contextual help about a problem -- unlike the old
    submission-triggered explanation flow, this can be called at any point
    while a student is looking at a problem, whether or not they've
    submitted anything yet. `conversation` is prior turns in this chat as
    [{"role": "assistant"|"user", "content": ...}, ...], empty on the first
    message. Returns {"answer": str, "usage": {...}}.
    """
    context = _build_ask_phoenix_context(problem, current_query)
    messages = [
        {"role": "system", "content": _ask_phoenix_system_prompt(problem.get("track", "sql"))},
        {"role": "user", "content": context},
    ]
    messages.extend(conversation)
    messages.append({"role": "user", "content": question})

    result = _call_chat_complete(user_id=user_id, problem_id=problem["id"], messages=messages, max_tokens=900)
    return {"answer": result["reply"], "usage": result["usage"]}


PERSONA_TONE = {
    "friendly": (
        "Adopt a warm, encouraging tone -- acknowledge good answers "
        "explicitly, give the candidate room to think, and soften "
        "follow-ups on gaps (e.g. \"no worries, let's come at it from "
        "another angle\").\n\n"
    ),
    "neutral": "",  # today's existing tone, unchanged
    "strict": (
        "Adopt a terse, no-frills tone typical of a tough technical panel "
        "round -- minimal encouragement, move on quickly from vague "
        "answers, press harder on follow-ups. Still stay composed and "
        "professional throughout -- terse and demanding, never rude, "
        "impatient, or dismissive.\n\n"
    ),
}


def _interview_system_prompt(
    topics: list[str],
    resume_text: str | None,
    current_topic: str | None = None,
    topic_turn_count: int = 0,
    max_turns_per_topic: int = 3,
    forced_topic: str | None = None,
    persona: str = "neutral",
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
        "candidate applying to a data/analytics role. Ask ONE "
        "question at a time, in natural spoken language -- no markdown, no "
        "bullet points, no code blocks, since your question will be read "
        "aloud by text-to-speech. Keep each question to 1-3 sentences.\n\n"
        "Maintain a gentle, calm, patient demeanor throughout, regardless "
        "of persona below -- interviews are stressful enough for the "
        "candidate. Never sound rushed, impatient, or dismissive, even "
        "when correcting a wrong answer or moving on from one they "
        "couldn't answer.\n\n"
        "Before asking your next question (in that same `question` field, "
        "since that's the only thing spoken aloud), open with a brief, "
        "natural acknowledgment of what the candidate just said. Vary your "
        "phrasing every time like a real person would -- never lean on the "
        "same handful of stock phrases (\"got it\", \"no worries\") turn "
        "after turn, that reads as scripted. Match the warmth of your "
        "reaction to the moment: a wrong technical answer just needs a "
        "brief neutral acknowledgment before moving on, but a nervous or "
        "personal moment (e.g. declining to introduce themselves) deserves "
        "genuine reassurance, not the same clipped phrase you'd use for a "
        "wrong JOIN. One short, natural reaction -- not a summary or "
        "restatement of their answer. Skip this only for: the very first "
        "question of the interview (nothing to acknowledge yet), and "
        "transcription-glitch repeat-requests (see below), which must stay "
        "as just the repeat-request itself.\n\n"
        "If the candidate declines or can't answer the opening "
        "\"introduce yourself\" question, that's not a technical gap -- "
        "respond with extra warmth (nerves are normal) and ease into the "
        "easiest, most approachable SQL question you can think of (a plain "
        "single-table SELECT, nothing with multiple conditions or "
        "unfamiliar concepts yet), not a moderately complex one. Save "
        "harder scenarios for once they've found their footing.\n\n"
        f"{PERSONA_TONE.get(persona, '')}"
        f"{resume_block}"
        f"{topic_budget_block}"
        f"Topics to cover across the interview: {', '.join(topics)}.\n\n"
        "The `topic` field in your JSON response MUST be exactly one of "
        "those topic names (exact spelling), never an invented or "
        "paraphrased label -- this is what's used to track how long you've "
        "spent on each topic, so an inconsistent label breaks that "
        "tracking. On follow_up/probe, reuse the SAME topic string you (or "
        "the topic_budget note above) were already given for the current "
        "topic.\n\n"
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
        "- switch_topic: their answer was sufficient, they are clearly "
        "stuck after a follow-up/probe already, OR their answer is a "
        "genuine non-attempt like \"I don't know\" (see candidate_stuck "
        "below) -- a real interviewer moves on right away after someone "
        "gives up, they don't ask the same thing again. Move to a new "
        "topic from the list above that has not been covered yet.\n\n"
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
        "Set `candidate_stuck` to true when their most recent answer was a "
        "genuine non-attempt -- \"I don't know\", \"I'm not sure\", \"no "
        "idea\", or similar giving-up, as opposed to a wrong-but-attempted "
        "answer. This is tracked separately from your action/topic choice "
        "and used to force a topic change even if you pick follow_up, so "
        "set it honestly regardless of what action you choose.\n\n"
        "Respond with ONLY a JSON object, no other text, no markdown code "
        'fences: {"action": "follow_up"|"probe"|"switch_topic", "topic": '
        '"<topic name>", "question": "<your next spoken question>", '
        '"candidate_stuck": true|false, '
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
    persona: str = "neutral",
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
        topics, resume_text, current_topic, topic_turn_count, forced_topic=forced_topic, persona=persona,
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
            "candidate_stuck": bool(parsed.get("candidate_stuck", False)),
            "usage": result["usage"],
        }
    except (json.JSONDecodeError, KeyError):
        return {
            "action": "switch_topic",
            "topic": forced_topic or topics[0],
            "question": result["reply"],
            "table_context": None,
            "candidate_stuck": False,
            "usage": result["usage"],
        }


FEEDBACK_SYSTEM_PROMPT = (
    "You are an experienced SQL interviewer writing a feedback report after "
    "a mock interview. Review the full transcript and produce a structured, "
    "honest but encouraging assessment for the candidate.\n\n"
    f"topics_to_study MUST only contain values from this exact list (use "
    f"the exact spelling): {', '.join(topics.ALL_TOPICS)}.\n\n"
    "Respond with ONLY a JSON object, no other text, no markdown code "
    'fences: {"overall_summary": "<2-3 sentence overall impression>", '
    '"score": <integer 0-100, your holistic assessment of interview performance>, '
    '"strengths": ["<point>", ...], "weaknesses": ["<point>", ...], '
    '"topics_to_study": ["<topic, from the list above>", ...], '
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
            "score": None,
            "strengths": [],
            "weaknesses": [],
            "topics_to_study": [],
            "rough_level": "intermediate",
        }
    return {"report": report, "usage": result["usage"]}


PROBLEM_BATCH_SYSTEM_PROMPT = (
    "You write practice SQL problems for a platform helping candidates "
    "prep for job interviews, in the style of the SQL Cookbook "
    "(Molinaro) -- realistic scenarios, and seed data that includes NULLs "
    "and/or duplicate rows where it makes the problem meaningfully harder "
    "(not just for the sake of it), like real analyst data.\n\n"
    "Calibrate every problem to what a real technical interviewer would "
    "actually ask, not generic textbook filler. Concretely:\n"
    "- easy = a fair warm-up/screening-round question -- tests real "
    "understanding of a core clause (WHERE, ORDER BY, basic aggregation), "
    "not a trivial \"select all columns\" exercise.\n"
    "- medium = a typical mid-round question -- combines joins/"
    "aggregation/subqueries with a realistic business framing, and has at "
    "least one non-obvious edge case (NULLs, duplicates, an ambiguous "
    "requirement) a candidate could plausibly miss.\n"
    "- hard = a senior-round question -- genuine analytical/window-"
    "function/hierarchical-query thinking (not just obscure syntax "
    "trivia), the kind that separates candidates who deeply understand "
    "SQL from those who've memorized patterns.\n"
    "Calibrate to the bar set by FAANG-caliber (Meta, Amazon, Apple, "
    "Google, Netflix, and peers like Uber/Airbnb/Stripe) SQL screens for "
    "Data Analyst, Business Intelligence, and Data Engineer roles alike, "
    "not a generic bootcamp exercise -- these three roles share the same "
    "core SQL bar even though what they build on top of it differs. That "
    "bar leans heavily on: cohort retention/churn, funnel drop-off "
    "between steps, running/rolling metrics (7-day active users, moving "
    "averages), period-over-period growth, ranking within groups "
    "(top-N per category, nth-highest), sessionization from raw event "
    "logs, deduplication of noisy real-world data (the kind a DE pipeline "
    "has to guard against upstream), star-schema fact/dimension joins and "
    "KPI rollups (the kind a BI report is built on), and slowly-changing-"
    "dimension-style historical lookups -- draw hard problems from this "
    "pool rather than inventing artificial syntax puzzles. If you "
    "wouldn't expect an actual FAANG interviewer to plausibly ask a "
    "version of this question for one of these three roles, don't "
    "include it.\n\n"
    "Every `description` MUST be fully unambiguous about WHAT is being "
    "asked -- exact inputs, exact expected output, and every edge case "
    "that matters (how to handle NULLs, ties, empty results, etc.) --  a "
    "candidate should never have to guess your intent. At the same time, "
    "it must give ZERO hint about HOW to solve it: never name the clause/"
    "function/technique the solution needs (e.g. don't write \"use a "
    "window function\" or \"watch out for how JOINs handle duplicates\" "
    "in the description), and never hint that a gotcha is present at all, "
    "even generically (no \"be careful here\" or \"this is trickier than "
    "it looks\"). State requirements plainly and neutrally, as a real "
    "interviewer would pose the question, and let the candidate discover "
    "the approach and any pitfalls entirely on their own.\n\n"
    "Variety is NON-NEGOTIABLE, not a nice-to-have -- it matters as much "
    "as correctness. Generic employees/departments/orders/products "
    "scenarios are the model's own default comfort zone and MUST NOT "
    "dominate the batch. Concretely: rotate through this domain list "
    "in order, assigning each successive problem in the batch the next "
    "domain (wrapping around if the batch is longer than the list) -- "
    "e-commerce, healthcare, banking/fintech, logistics/shipping, SaaS "
    "subscriptions, education, hospitality/travel, marketplaces/gig-"
    "economy, gaming/entertainment, telecom, real estate, insurance. At "
    "most ONE problem in the entire batch may use a generic employees/"
    "departments/company-org-chart scenario -- everything else must come "
    "from a genuinely different domain, with domain-appropriate table/"
    "column names and entities (not the same 'orders'/'customers' shape "
    "reskinned with a different label). Also vary the specific scenario "
    "and table shape even within the same topic and same domain. Do not "
    "reuse a scenario, table shape, or phrasing you've already used "
    "earlier in this same batch. IMPORTANT: this domain is the business "
    "setting for the scenario/description/table names ONLY -- it is "
    "NEVER the value of the `topic` JSON field, which must always be "
    "exactly one of the given topics from the taxonomy list (e.g. "
    "'Working with Strings'), never a domain name like 'e-commerce'.\n\n"
    "Include a meaningful fraction (roughly a third) as deliberate 'trick' "
    "problems -- ones that look straightforward but have a real gotcha a "
    "candidate would plausibly get wrong in an interview: a NOT IN list "
    "that silently returns zero rows because it contains a NULL, a JOIN "
    "that quietly duplicates rows before an aggregate runs over them, "
    "COUNT(column) vs COUNT(*) disagreeing because of NULLs, integer "
    "division truncating when the candidate expected a decimal, a LEFT "
    "JOIN condition placed in WHERE instead of ON silently turning it into "
    "an INNER JOIN, GROUP BY with a non-aggregated column that only some "
    "engines allow, or similar real gotchas -- not artificially obscure "
    "puzzles, just the traps that actually bite people. Tag these with "
    "\"trap\" in addition to their normal tags, and make sure the "
    "description doesn't give the gotcha away -- the student should only "
    "discover it by getting the wrong answer and thinking through why.\n\n"
    "Every problem MUST be gradeable by running a single read-only query "
    "and diffing its output -- this is a hard platform constraint, not a "
    "style preference. That means:\n"
    "- `canonical_sql` MUST be exactly one SELECT or WITH...SELECT "
    "statement. Never INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, or "
    "any other statement that mutates data or schema.\n"
    "- `schema_sql` (CREATE TABLE statements) and `seed_sql` (INSERT "
    "statements) set up the fixed starting data; `canonical_sql` only "
    "ever reads it.\n"
    "- SQL must be valid DuckDB syntax (DuckDB is close to Postgres/SQL "
    "standard, but confirm functions like split_part, date_trunc, "
    "information_schema.columns, MOD, RANK()/window functions, and "
    "WITH RECURSIVE are used the DuckDB way).\n"
    "- Any date-based problem must use fixed literal dates (e.g. DATE "
    "'2024-07-15'), never CURRENT_DATE/NOW(), since the expected output "
    "is cached once and must stay correct indefinitely.\n\n"
    "Respond with ONLY a JSON object, no other text, no markdown code "
    'fences: {"problems": [{"title": "...", "difficulty": '
    '"easy"|"medium"|"hard", "topic": "<one of the given topics, exactly '
    'as written>", "tags": ["...", ...], "description": "...", '
    '"schema_sql": "...", "seed_sql": "...", "canonical_sql": "...", '
    '"order_matters": true|false}, ...]}'
)


def generate_problem_batch(*, user_id: str, topics: list[str], count: int, existing_titles: list[str] | None = None) -> dict:
    """
    Drafts `count` new practice problems spread across `topics` (DML is
    never one of them -- see topics.GRADEABLE_TOPICS). Returns
    {"problems": [...], "usage": {...}}. Callers MUST still run each
    draft's canonical_sql through sandbox.validate_student_sql before
    storing it -- this is a second, code-level check independent of
    whether the model actually followed the prompt. `existing_titles`
    (every problem already live or pending review) is fed back in so the
    model avoids re-deriving a scenario that's already in the bank --
    insert_pending_draft() also re-checks this with a code-level
    similarity threshold, since a prompt instruction alone isn't reliable
    enough to skip on its own.
    """
    user_prompt = (
        f"Draft {count} new practice problems spread across these topics "
        f"(cover each at least once if count allows): {', '.join(topics)}.\n"
        "Mix difficulties (easy/medium/hard) across the batch rather than "
        "making them all one level."
    )
    if existing_titles:
        titles_block = "\n".join(f"- {t}" for t in existing_titles)
        user_prompt += (
            "\n\nThese problems already exist -- do not draft anything "
            "that's the same scenario under a different name:\n" + titles_block
        )
    messages = [
        {"role": "system", "content": PROBLEM_BATCH_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    # Scale the completion-token ceiling with batch size so a small batch
    # isn't needlessly capped and a large one isn't cut off mid-JSON. The
    # FAANG-calibrated prompt (richer schemas/seed data, trap-problem
    # rationale baked into scenario design) produces noticeably longer
    # per-problem output than the original prompt did, so the floor and
    # per-problem multiplier here are both higher than what worked before.
    last_parse_error = None
    for attempt in range(3):
        result = _call_chat_with_retry(
            user_id=user_id, problem_id="admin-problem-batch", messages=messages,
            max_tokens=min(8000, max(1500, count * 500)), json_mode=True,
            timeout=120, temperature=0.85,
        )
        try:
            parsed = _parse_json_reply(result["reply"])
            return {"problems": parsed.get("problems", []), "usage": result["usage"]}
        except (json.JSONDecodeError, KeyError) as e:
            last_parse_error = e
    raise RuntimeError(f"Model didn't return valid JSON after 3 attempts ({last_parse_error}).")


PYTHON_PROBLEM_BATCH_SYSTEM_PROMPT = (
    "You write practice Python problems for a platform helping candidates "
    "prep for job interviews, in the style of the Python Cookbook "
    "(Beazley/Jones) -- realistic scenarios, not abstract puzzles for their "
    "own sake.\n\n"
    "Calibrate every problem to what a real technical interviewer would "
    "actually ask, not generic textbook filler. Concretely:\n"
    "- easy = a fair warm-up/screening-round question -- tests real "
    "understanding of a core concept (list/dict operations, string "
    "methods, basic iteration), not \"print hello world\" trivia.\n"
    "- medium = a typical mid-round question -- combines 2-3 concepts, "
    "has a realistic business framing, and has at least one non-obvious "
    "edge case a candidate could plausibly miss.\n"
    "- hard = a senior-round question -- genuine algorithmic or design "
    "thinking (not just obscure syntax trivia), the kind that separates "
    "candidates who deeply understand Python from those who've memorized "
    "patterns.\n"
    "Calibrate to the bar set by FAANG-caliber (Meta, Amazon, Apple, "
    "Google, Netflix, and peers like Uber/Airbnb/Stripe) coding screens "
    "for Data Analyst, Business Intelligence, and Data Engineer roles "
    "alike -- these lean toward practical data-wrangling and correctness-"
    "under-edge-cases (parsing/cleaning messy records, grouping and "
    "aggregating nested structures, merging overlapping intervals, "
    "deduplicating near-identical entries, efficient counting/frequency "
    "problems, validating/reshaping semi-structured records the way a "
    "DE pipeline ingest step would) rather than pure LeetCode-style "
    "algorithm-contest puzzles (no need for advanced graph algorithms, "
    "dynamic programming on exotic state spaces, or competitive-"
    "programming-only tricks). If you wouldn't expect an actual FAANG "
    "interviewer to plausibly ask a version of this question for one of "
    "these three roles, don't include it.\n\n"
    "Every `description` MUST be fully unambiguous about WHAT is being "
    "asked -- exact inputs, exact expected output/return value, and every "
    "edge case that matters (empty input, duplicates, None values, etc.) "
    "-- a candidate should never have to guess your intent. At the same "
    "time, it must give ZERO hint about HOW to solve it: never name the "
    "concept/technique the solution needs (e.g. don't write \"use a "
    "generator\" or \"watch out for mutable default arguments\" in the "
    "description), and never hint that a gotcha is present at all, even "
    "generically (no \"be careful here\" or \"this is trickier than it "
    "looks\"). State requirements plainly and neutrally, as a real "
    "interviewer would pose the question, and let the candidate discover "
    "the approach and any pitfalls entirely on their own.\n\n"
    "Variety is NON-NEGOTIABLE, not a nice-to-have. Rotate through this "
    "domain list in order, assigning each successive problem in the "
    "batch the next domain (wrapping around if needed): e-commerce, "
    "healthcare, banking/fintech, logistics/shipping, SaaS "
    "subscriptions, education, hospitality/travel, marketplaces/gig-"
    "economy, gaming/entertainment, telecom. Use domain-appropriate "
    "entities and naming for each, not a reskinned copy of the same "
    "generic shape. Also vary the specific scenario even within the "
    "same topic and domain. Do not reuse a scenario or phrasing you've "
    "already used earlier in this same batch. IMPORTANT: this domain is "
    "the business setting for the scenario/description/naming ONLY -- "
    "it is NEVER the value of the `topic` JSON field, which must always "
    "be exactly one of the given topics from the taxonomy list, never a "
    "domain name like 'e-commerce'.\n\n"
    "Include a meaningful fraction (roughly a third) as deliberate 'trick' "
    "problems -- ones that look straightforward but have a real gotcha a "
    "candidate would plausibly get wrong in an interview: a mutable "
    "default argument holding state across calls, a closure over a loop "
    "variable that late-binds instead of capturing the value at each "
    "iteration, `is` vs `==` behaving unexpectedly on small cached "
    "integers, a shallow copy sharing a nested list/dict, `list * n` "
    "creating multiple references to the SAME inner list, off-by-one "
    "errors in slicing, or similar real gotchas -- not artificially "
    "obscure puzzles, just the traps that actually bite people. Tag these "
    "with \"trap\" in addition to their normal tags, and make sure the "
    "description doesn't give the gotcha away -- the student should only "
    "discover it by getting the wrong answer and thinking through why.\n\n"
    "Every problem MUST be gradeable by executing the student's function "
    "definition followed by a battery of assert statements -- this is a "
    "hard platform constraint, not a style preference. That means:\n"
    "- `function_signature` names exactly the one function the student "
    "must define (e.g. `merge_intervals`). `starter_code` is what's shown "
    "in the editor: the function signature plus a docstring describing "
    "the task, and a `pass` body -- no hints at the solution.\n"
    "- `test_code` is a plain Python script of `assert` statements only -- "
    "no `unittest`/`pytest` framework, no imports beyond Python's standard "
    "library, and it must call the function under EXACTLY the name in "
    "`function_signature`. Include at least 4-6 assertions covering "
    "normal cases and at least one edge case (empty input, single "
    "element, boundary value, etc.).\n"
    "- `canonical_solution` is a full, correct implementation of the "
    "function that would pass every assertion in `test_code` -- it is "
    "never shown to students, only used to validate the problem itself "
    "before it's accepted.\n"
    "- The function must have no side effects the sandbox might not allow "
    "-- no real network calls, no real file writes outside what the "
    "problem itself is about, no subprocess/os-level calls -- pure "
    "input-to-output logic only.\n\n"
    "Respond with ONLY a JSON object, no other text, no markdown code "
    'fences: {"problems": [{"title": "...", "difficulty": '
    '"easy"|"medium"|"hard", "topic": "<one of the given topics, exactly '
    'as written>", "tags": ["...", ...], "description": "...", '
    '"starter_code": "...", "function_signature": "...", '
    '"test_code": "...", "canonical_solution": "..."}, ...]}'
)


STATS_PYTHON_BATCH_SYSTEM_PROMPT = (
    "You write practice statistics/probability problems for a data-"
    "analytics interview-prep platform. Every problem is COMPUTATIONAL: "
    "the student writes a Python function that computes a specific "
    "statistical quantity from given data, verified by executing it "
    "against assert-based test cases -- same contract as a Python coding "
    "problem, just with statistical content (e.g. \"write a function "
    "that computes a two-sample t-test p-value,\" \"calculate a "
    "confidence interval for a conversion rate,\" \"detect Simpson's "
    "paradox in this grouped data\").\n\n"
    "Calibrate every problem to what a real data-analyst interview would "
    "actually ask. Concretely:\n"
    "- easy = a fair warm-up -- compute a mean/variance/standard "
    "deviation correctly, including a real subtlety (e.g. sample vs. "
    "population variance), not just calling a one-line library function.\n"
    "- medium = combines a statistical concept with a realistic "
    "analytics scenario (A/B test read-out, funnel conversion rate, "
    "cohort retention) and has at least one non-obvious edge case (small "
    "sample size, unequal variances, missing data).\n"
    "- hard = genuine statistical reasoning under a real complication "
    "(confounding, multiple comparisons, non-normal data, Simpson's "
    "paradox), not just a harder formula.\n"
    "Calibrate to the bar set by FAANG-caliber (Meta, Amazon, Apple, "
    "Google, Netflix, and peers like Uber/Airbnb/Stripe) experimentation "
    "and metrics rounds for Data Analyst, Data Scientist, and Business "
    "Intelligence roles alike -- these lean heavily on A/B test read-outs "
    "(is this lift real, is the sample size adequate, is novelty effect a "
    "risk), metric-movement root-causing, guardrail-metric trade-offs, "
    "dashboard/KPI anomaly detection (is this metric swing real signal "
    "or noise -- the kind a BI or DE data-quality-monitoring role cares "
    "about), and correctly reasoning about statistical power and "
    "multiple-comparison corrections, not just textbook formula recall. "
    "If you wouldn't expect an actual FAANG interviewer to plausibly ask "
    "a version of this question for one of these roles, don't include "
    "it.\n\n"
    "Every `description` MUST be fully unambiguous about WHAT is asked "
    "-- exact inputs, exact expected return value/format, and every edge "
    "case that matters -- but must give ZERO hint about HOW to solve it: "
    "never name the statistical test/formula/library function the "
    "solution needs, and never hint that a subtlety or gotcha is present "
    "even generically. State the scenario plainly and let the candidate "
    "figure out which test/approach applies.\n\n"
    "You MAY use Python's standard library (`statistics`, `math`, "
    "`random` for reproducible synthetic data) as well as `pandas` and "
    "`numpy`, all of which the sandbox has pre-installed -- prefer the "
    "standard library for a genuinely simple calculation, but don't "
    "avoid pandas/numpy where a real analyst would naturally reach for "
    "them (e.g. operating on a DataFrame/Series rather than raw lists). "
    "Avoid `scipy`, which is not guaranteed to be available.\n\n"
    "Variety is NON-NEGOTIABLE, not a nice-to-have. Rotate through this "
    "domain list in order, assigning each successive problem in the "
    "batch the next domain (wrapping around if needed): e-commerce, "
    "healthcare, banking/fintech, logistics/shipping, SaaS "
    "subscriptions, education, hospitality/travel, marketplaces/gig-"
    "economy, gaming/entertainment, telecom. Use domain-appropriate "
    "entities and column names for each, not a reskinned copy of the "
    "same generic shape. Also vary the specific scenario and data shape "
    "even within the same topic and domain. Do not reuse a scenario or "
    "phrasing already used earlier in this same batch. IMPORTANT: this "
    "domain is the business setting for the scenario/description/naming "
    "ONLY -- it is NEVER the value of the `topic` JSON field, which must "
    "always be exactly one of the given topics from the taxonomy list, "
    "never a domain name like 'e-commerce'.\n\n"
    "`function_signature` names exactly the one function the student "
    "must define. `starter_code` is the function signature plus a "
    "docstring describing the task and a `pass` body -- no hints at the "
    "solution. `test_code` is plain `assert` statements only (no "
    "`unittest`/`pytest`), calling the function under EXACTLY the name "
    "in `function_signature`, with at least 4-6 assertions covering "
    "normal cases and at least one edge case. `canonical_solution` is a "
    "full, correct implementation that passes every assertion -- never "
    "shown to students, only used to validate the problem itself.\n\n"
    "Respond with ONLY a JSON object, no other text, no markdown code "
    'fences: {"problems": [{"title": "...", "difficulty": '
    '"easy"|"medium"|"hard", "topic": "<one of the given topics, exactly '
    'as written>", "tags": ["...", ...], "description": "...", '
    '"starter_code": "...", "function_signature": "...", '
    '"test_code": "...", "canonical_solution": "..."}, ...]}'
)


DATA_LIB_PYTHON_BATCH_SYSTEM_PROMPT = (
    "You write practice pandas/numpy problems for a data-analytics "
    "interview-prep platform. Every problem is COMPUTATIONAL: the student "
    "writes a Python function that takes a pandas DataFrame/Series or "
    "numpy array as input and returns a specific transformed result, "
    "verified by executing it against assert-based test cases -- same "
    "contract as a Python coding problem, just scoped specifically to "
    "pandas/numpy idioms rather than general-purpose Python.\n\n"
    "Calibrate every problem to what a real data-analyst/data-engineer "
    "interview would actually ask. Concretely:\n"
    "- easy = a fair warm-up -- filtering rows, selecting columns, a "
    "basic groupby-aggregate, or a simple elementwise numpy operation, "
    "with a real subtlety (e.g. NaN handling, dtype coercion) rather "
    "than a one-line library call with no thought required.\n"
    "- medium = a typical mid-round question -- combines groupby/merge/"
    "pivot or numpy broadcasting with a realistic analytics scenario, "
    "and has at least one non-obvious edge case (missing data, "
    "duplicate keys, misaligned indices, mismatched shapes).\n"
    "- hard = genuine data-wrangling depth -- multi-step groupby-apply "
    "chains, merging on multiple/fuzzy keys, reshaping between wide and "
    "long formats, or vectorizing a computation that a candidate might "
    "instinctively reach for a slow Python loop to solve -- the kind "
    "that separates candidates who really know pandas/numpy from those "
    "who've only used `.head()` and `.describe()`.\n"
    "Calibrate to the bar set by FAANG-caliber (Meta, Amazon, Apple, "
    "Google, Netflix, and peers like Uber/Airbnb/Stripe) coding screens "
    "for Data Analyst, Business Intelligence, and Data Engineer roles "
    "alike -- these lean on real data-wrangling tasks (cleaning messy "
    "columns, joining and reshaping tables, computing grouped metrics, "
    "handling missing/duplicate data at scale) rather than abstract "
    "numerical-computing puzzles. If you wouldn't expect an actual FAANG "
    "interviewer to plausibly ask a version of this question for one of "
    "these three roles, don't include it.\n\n"
    "Every `description` MUST be fully unambiguous about WHAT is asked "
    "-- exact input shape/columns, exact expected return value/format, "
    "and every edge case that matters (empty input, NaNs, duplicate "
    "index values, mismatched dtypes) -- but must give ZERO hint about "
    "HOW to solve it: never name the pandas/numpy method the solution "
    "needs (e.g. don't write \"use groupby\" or \"watch out for how "
    "merge handles duplicate keys\" in the description), and never hint "
    "that a gotcha is present at all, even generically. State the task "
    "plainly and let the candidate figure out the right approach.\n\n"
    "Include a meaningful fraction (roughly a third) as deliberate "
    "'trick' problems -- ones that look straightforward but have a real "
    "gotcha a candidate would plausibly get wrong: a merge silently "
    "duplicating rows on a non-unique join key, `NaN != NaN` breaking an "
    "equality-based filter, a groupby dropping NaN keys by default, "
    "chained indexing triggering a SettingWithCopyWarning / silently not "
    "mutating the original frame, index misalignment silently "
    "introducing NaNs during an arithmetic operation between two "
    "Series, integer vs. float dtype coercion when NaNs are introduced, "
    "or similar real gotchas -- not artificially obscure puzzles, just "
    "the traps that actually bite people. Tag these with \"trap\" in "
    "addition to their normal tags, and make sure the description "
    "doesn't give the gotcha away.\n\n"
    "Variety is NON-NEGOTIABLE, not a nice-to-have. Rotate through this "
    "domain list in order, assigning each successive problem in the "
    "batch the next domain (wrapping around if needed): e-commerce, "
    "healthcare, banking/fintech, logistics/shipping, SaaS "
    "subscriptions, education, hospitality/travel, marketplaces/gig-"
    "economy, gaming/entertainment, telecom. Use domain-appropriate "
    "entities and naming for each, not a reskinned copy of the same "
    "generic shape. Also vary the specific scenario even within the "
    "same topic and domain. Do not reuse a scenario or phrasing already "
    "used earlier in this same batch. IMPORTANT: this domain is the "
    "business setting for the scenario/description/naming ONLY -- it is "
    "NEVER the value of the `topic` JSON field, which must always be "
    "exactly one of the given topics from the taxonomy list, never a "
    "domain name like 'e-commerce'.\n\n"
    "Use `pandas` for DataFrame/Series-shaped problems (topic 'Pandas "
    "DataFrames') and `numpy` for array-shaped problems (topic 'NumPy "
    "Arrays') -- both are pre-installed in the grading sandbox. Avoid "
    "`scipy`, which is not guaranteed to be available. The test data "
    "itself should be constructed inside `test_code` (e.g. building a "
    "small DataFrame/array literal), not loaded from an external file.\n\n"
    "`function_signature` names exactly the one function the student "
    "must define. `starter_code` is the function signature plus a "
    "docstring describing the task and a `pass` body -- no hints at the "
    "solution. `test_code` is plain `assert` statements only (no "
    "`unittest`/`pytest`), importing pandas/numpy itself as needed, "
    "calling the function under EXACTLY the name in `function_signature`, "
    "with at least 4-6 assertions covering normal cases and at least one "
    "edge case -- when comparing DataFrames/Series/arrays for equality, "
    "use the appropriate comparison (e.g. `.equals(...)`, "
    "`np.array_equal(...)`, or converting to a plain Python structure "
    "first) rather than a bare `==`, which doesn't behave like a normal "
    "boolean in these libraries. `canonical_solution` is a full, correct "
    "implementation that passes every assertion -- never shown to "
    "students, only used to validate the problem itself.\n\n"
    "Respond with ONLY a JSON object, no other text, no markdown code "
    'fences: {"problems": [{"title": "...", "difficulty": '
    '"easy"|"medium"|"hard", "topic": "<one of the given topics, exactly '
    'as written>", "tags": ["...", ...], "description": "...", '
    '"starter_code": "...", "function_signature": "...", '
    '"test_code": "...", "canonical_solution": "..."}, ...]}'
)


def generate_python_problem_batch(*, user_id: str, topics: list[str], count: int, existing_titles: list[str] | None = None, system_prompt: str | None = None) -> dict:
    """
    Python-track equivalent of generate_problem_batch() -- same shape,
    same validation contract (callers MUST still run each draft's
    canonical_solution/test_code through pysandbox.run_python_submission
    before storing it, same as insert_pending_draft() already does).

    `system_prompt` defaults to PYTHON_PROBLEM_BATCH_SYSTEM_PROMPT, but
    callers can swap in a different one (e.g. STATS_PYTHON_BATCH_SYSTEM_PROMPT)
    for a differently-flavored batch while reusing this exact same
    call/retry/parse machinery -- statistics isn't a separate track or a
    separate generation function, just a different prompt over the same
    "write a Python function + assert-based tests" contract.
    """
    system_prompt = system_prompt or PYTHON_PROBLEM_BATCH_SYSTEM_PROMPT
    user_prompt = (
        f"Draft {count} new practice problems spread across these topics "
        f"(cover each at least once if count allows): {', '.join(topics)}.\n"
        "Mix difficulties (easy/medium/hard) across the batch rather than "
        "making them all one level."
    )
    if existing_titles:
        titles_block = "\n".join(f"- {t}" for t in existing_titles)
        user_prompt += (
            "\n\nThese problems already exist -- do not draft anything "
            "that's the same scenario under a different name:\n" + titles_block
        )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    # Python problems carry more content per item than SQL's (starter_code
    # + function_signature + several test_code assertions + a full
    # canonical_solution, vs. SQL's schema/seed/one-query trio), so this
    # needs a more generous per-problem token budget than the SQL batch
    # formula.
    #
    # json_mode is deliberately OFF here, unlike every other structured
    # call in this file -- Groq's strict JSON response_format reproducibly
    # failed ("Failed to validate JSON", empty failed_generation) on this
    # specific prompt/schema even after the token budget was ruled out as
    # the cause, while the same mode works fine for the SQL batch prompt.
    # Likely cause: Python test_code/canonical_solution values routinely
    # mix single and double quotes within the same string (docstrings,
    # f-strings) in a way SQL's schema/seed/query values rarely do,
    # apparently harder for Groq's constrained-JSON backend to handle for
    # this payload shape. Falling back to the plain "respond with ONLY
    # JSON" instruction plus _parse_json_reply's existing defensive
    # parsing (already strips code fences) avoids the provider-side
    # failure entirely.
    # Without json_mode enforcing strict validity provider-side, the model
    # occasionally slips on its own JSON escaping -- most often an
    # unescaped quote inside a Python docstring/f-string value prematurely
    # closing a JSON string. This is a stochastic generation error (a
    # fresh sample usually just works), not a deterministic one, so retry
    # the whole call a couple of times on a parse failure before giving up
    # -- unlike _call_chat_with_retry's retries, which only cover HTTP-level
    # failures, not "the call succeeded but produced malformed JSON".
    last_parse_error = None
    for attempt in range(3):
        result = _call_chat_with_retry(
            user_id=user_id, problem_id="admin-python-problem-batch", messages=messages,
            max_tokens=min(6000, max(1500, count * 600)), json_mode=False,
            # Lower than the SQL batch's 0.85 -- Python's canonical_solution
            # has to actually pass its own test_code, and higher
            # temperature was producing noticeably more self-inconsistent
            # code (the model's own solution failing its own asserts)
            # without a corresponding gain in scenario variety worth that
            # trade for code correctness the way it was for SQL scenarios.
            timeout=120, temperature=0.75,
        )
        try:
            parsed = _parse_json_reply(result["reply"])
            return {"problems": parsed.get("problems", []), "usage": result["usage"]}
        except (json.JSONDecodeError, KeyError) as e:
            last_parse_error = e
    raise RuntimeError(f"Model didn't return valid JSON after 3 attempts ({last_parse_error}).")
