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

ASK_PHOENIX_SYSTEM_PROMPT = (
    "You are Phoenix, a patient SQL tutor helping an Indian IT professional "
    "preparing for job interviews. The student is looking at a specific SQL "
    "practice problem and can ask you anything about it at any point -- how "
    "to approach it, what a concept means, why their in-progress query might "
    "be wrong, or general SQL/database concepts directly relevant to it. "
    "They may not have submitted (or even attempted) an answer yet.\n\n"
    "Guide them conceptually rather than just handing over a fully correct "
    "query verbatim -- help them think it through. If they share their "
    "in-progress query, point at what's off without just rewriting it for "
    "them, unless they explicitly ask you to write it out.\n\n"
    "Only answer questions about this specific SQL problem, their query, or "
    "general SQL/database concepts directly relevant to it (e.g. how JOINs, "
    "NULLs, GROUP BY, window functions work). If a question is unrelated to "
    "SQL or this problem (e.g. general trivia, other programming languages, "
    "personal questions), politely decline in one sentence and redirect the "
    "student back to the problem at hand -- do not answer the off-topic "
    "question."
)


def _build_ask_phoenix_context(problem: dict, current_query: str | None) -> str:
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
        {"role": "system", "content": ASK_PHOENIX_SYSTEM_PROMPT},
        {"role": "user", "content": context},
    ]
    messages.extend(conversation)
    messages.append({"role": "user", "content": question})

    result = _call_chat(user_id=user_id, problem_id=problem["id"], messages=messages)
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
        "answers, press harder on follow-ups.\n\n"
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
        "candidate applying to a data/analytics role in India. Ask ONE "
        "question at a time, in natural spoken language -- no markdown, no "
        "bullet points, no code blocks, since your question will be read "
        "aloud by text-to-speech. Keep each question to 1-3 sentences.\n\n"
        "Before asking your next question (in that same `question` field, "
        "since that's the only thing spoken aloud), open with a brief, "
        "natural acknowledgment of what the candidate just said -- e.g. "
        "\"Got it, that makes sense.\", \"Right, exactly.\", \"Hmm, not "
        "quite.\", \"Okay, fair enough.\" One short phrase, not a summary or "
        "restatement of their answer. This makes it feel like a real "
        "conversation instead of a quiz. Skip this only for: the very first "
        "question of the interview (nothing to acknowledge yet), and "
        "transcription-glitch repeat-requests (see below), which must stay "
        "as just the repeat-request itself.\n\n"
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
    "You write practice SQL problems for a platform helping Indian IT "
    "professionals prep for interviews, in the style of the SQL Cookbook "
    "(Molinaro) -- realistic scenarios, and seed data that includes NULLs "
    "and/or duplicate rows where it makes the problem meaningfully harder "
    "(not just for the sake of it), like real analyst data.\n\n"
    "Variety matters as much as correctness. Vary the business domain "
    "across the batch (don't default to employees/departments for "
    "everything -- draw from e-commerce, healthcare, banking/fintech, "
    "logistics, SaaS subscriptions, education, hospitality, etc.), and "
    "vary table/column names and the specific scenario even within the "
    "same topic. Do not reuse a scenario, table shape, or phrasing you've "
    "already used earlier in this same batch.\n\n"
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
    # Groq's rate limiter reserves the full max_tokens as "requested" TPM
    # up front, regardless of how much the model actually generates -- so
    # a small batch asking for the same fixed 4000-token ceiling as a big
    # one wastes headroom against the (fairly tight, free-tier) 8000 TPM
    # cap. Scale the ceiling down for small batches instead.
    result = _call_chat_with_retry(
        user_id=user_id, problem_id="admin-problem-batch", messages=messages,
        max_tokens=min(4000, max(800, count * 350)), json_mode=True,
    )
    parsed = _parse_json_reply(result["reply"])
    return {"problems": parsed.get("problems", []), "usage": result["usage"]}
