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

import sandbox

import topics
import role_topics

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


def _explain_topic_system_prompt(track: str, topic: str) -> str:
    subject = {"python": "Python", "case": "business-case interviewing"}.get(track, "SQL")
    return (
        f"You are Phoenix, a patient {subject} tutor. A candidate preparing for job "
        f"interviews wants to understand the CONCEPT of \"{topic}\" -- they reached "
        "you from their progress dashboard, not from a specific practice problem, "
        "usually because it's come up as a strength to reinforce or a weak spot to "
        "shore up. There is no single problem or solution being protected here, so "
        "unlike problem-specific help, teach freely: explain the concept clearly, "
        "walk through general example patterns, and answer follow-ups directly and "
        "completely, the way a real tutor would in a review session.\n\n"
        "Keep answers conversational -- a few short paragraphs or a short example "
        f"snippet where it genuinely helps, not a structured reference document. "
        f"Stay focused on {subject} and interview-relevant material; if asked "
        "something unrelated, politely decline in one sentence and redirect back "
        f"to \"{topic}\" or {subject} more broadly."
    )


def explain_topic(*, user_id: str, track: str, topic: str, conversation: list[dict], question: str) -> dict:
    """
    Open-ended concept explanation for a TOPIC, not a specific problem --
    reached from the progress dashboard's Strengths/Weaknesses/suggested-
    next-topic rows. Distinct from ask_phoenix's guide/comply mode split
    (which exists to protect one specific problem's answer): there's
    nothing to protect here, so this teaches the concept directly and
    completely from the first message.
    """
    messages = [{"role": "system", "content": _explain_topic_system_prompt(track, topic)}]
    messages.extend(conversation)
    messages.append({"role": "user", "content": question})

    result = _call_chat_complete(user_id=user_id, problem_id=f"topic:{track}:{topic}", messages=messages, max_tokens=900)
    return {"answer": result["reply"], "usage": result["usage"]}


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
        "answers, press harder on follow-ups.\n"
        "Your questions and follow-ups in this persona are BANNED from "
        "starting with, or containing anywhere, any of these phrases or "
        "close paraphrases of them: \"I appreciate your response\", "
        "\"that's a good thought\", \"that's completely okay\", \"no "
        "worries\", \"that's a great approach\", \"great job\", \"no "
        "problem at all\". If a gap or wrong answer needs to be named, "
        "name it flatly in one clause and move straight to the next "
        "question -- no acknowledgment sentence at all.\n"
        "  Neutral/friendly style (do NOT do this here): \"That's "
        "completely okay! Let's shift gears a bit. How would you...\"\n"
        "  Strict style (do this instead): \"That's not quite what I "
        "asked. Write the query.\" or simply drop straight into the next "
        "question with zero lead-in.\n"
        "This tone must be immediately, obviously distinguishable from "
        "the neutral default when read side by side -- not a mild "
        "variation of it. Still stay composed and professional throughout "
        "-- terse and demanding, never rude, impatient, or dismissive.\n\n"
    ),
}


def analyze_candidate_profile(*, user_id: str, resume_text: str | None, target_role: str, topic_history: dict | None = None) -> dict:
    """
    [Profile Analyzer] One-time call at /api/interview/start -- replaces the
    old per-turn silent-inference block in _interview_system_prompt (expensive
    and unreliable to redo identically every single turn) with a single
    structured result cached on the session for the interview's whole
    lifetime.

    When resume_text is None, skips the LLM call entirely and returns a
    generic default profile -- no wasted call on the common no-resume path.
    Never raises: any failure (network, malformed JSON) degrades to the same
    generic default rather than blocking the interview from starting at all,
    since this is a personalization enhancement, not the interview itself.

    Returns {"domain": str, "seniority": "junior"|"mid"|"senior",
    "key_skills": [str], "recommended_topics": [str, subset of
    role_topics.topics_for_role(target_role)], "opening_note": str}.
    """
    all_topics = role_topics.topics_for_role(target_role)
    default_profile = {
        "domain": "general",
        "seniority": "mid",
        "key_skills": [],
        "recommended_topics": all_topics[:7],
        "opening_note": f"Today we'll cover a mix of topics relevant to a {target_role} role.",
    }
    if not resume_text:
        if topic_history:
            # No resume, but a returning candidate -- weight
            # recommended_topics toward their weakest recent average score
            # with a cheap sort, no LLM call needed for this simple case
            # (topics with no history sort as if perfect, so they fill in
            # after genuinely weak ones rather than crowding them out).
            def _avg_score(topic):
                entries = topic_history.get(topic, [])
                return sum(e["score"] for e in entries) / len(entries) if entries else 100
            default_profile["recommended_topics"] = sorted(all_topics, key=_avg_score)[:7]
            default_profile["opening_note"] = (
                "Since you've interviewed before, we'll revisit a few areas "
                "you found tricky last time, alongside some new ground."
            )
        return default_profile

    history_block = ""
    if topic_history:
        weak = ", ".join(topic_history.keys())
        history_block = (
            f"\n\nThis candidate has interviewed before. Their weakest recent "
            f"topics were: {weak}. Weight recommended_topics toward "
            "rechecking those alongside covering new ground."
        )

    system_prompt = (
        "You are analyzing a candidate's resume ahead of a mock interview "
        f"for a {target_role} role. Extract a structured profile from the "
        "resume text below. Be concise and concrete -- this feeds directly "
        "into how the interview questions get framed.\n\n"
        f"Candidate's resume:\n{resume_text[:4000]}"
        f"{history_block}\n\n"
        "Respond with ONLY a JSON object, no other text, no markdown code "
        'fences: {"domain": "<apparent industry/domain, e.g. e-commerce, '
        "fintech, marketing, logistics, healthcare, SaaS, or 'general' if "
        'unclear>", "seniority": "junior"|"mid"|"senior", "key_skills": '
        '["<tool/technique strings pulled from the resume, e.g. dbt, '
        'Tableau, Python>"], "recommended_topics": ["<5-8 topics from this '
        f"exact list, ordered by relevance: {', '.join(all_topics)}>\"], "
        '"opening_note": "<one short sentence naming what this interview '
        "will focus on and why, e.g. 'Since you've spent two years on "
        "marketing analytics, we'll lean into metric design and SQL "
        'reporting.\' -- spoken verbatim in the opening, so keep it natural>"}'
    )
    try:
        result = _call_chat_with_retry(
            user_id=user_id, problem_id="mock-interview-profile",
            messages=[{"role": "system", "content": system_prompt}],
            max_tokens=500, json_mode=True,
        )
        parsed = _parse_json_reply(result["reply"])
        recommended = [t for t in parsed.get("recommended_topics", []) if t in all_topics]
        seniority = parsed.get("seniority")
        return {
            "domain": parsed.get("domain") or "general",
            "seniority": seniority if seniority in ("junior", "mid", "senior") else "mid",
            "key_skills": [s for s in parsed.get("key_skills", []) if isinstance(s, str)],
            "recommended_topics": recommended or all_topics[:7],
            "opening_note": parsed.get("opening_note") or default_profile["opening_note"],
        }
    except Exception:
        return default_profile


def classify_history_preference(*, user_id: str, answer_text: str | None) -> bool:
    """
    [Profile Analyzer] Interprets the candidate's spoken/typed reply to the
    opening monologue's "focus on past weak areas, or start fresh?" question
    (only asked when there's real interview_topic_history for this
    candidate -- see build_opening_monologue's ask_history_pref).

    Defaults to True (use history) for anything empty, garbled, or
    ambiguous: this is asked orally with no re-prompt loop, so silence or
    an unclear answer has to resolve to *something*, and the more useful
    default for a practice tool is to actually use the data it already has
    rather than silently discard it. Never raises -- any failure degrades
    to the same default rather than blocking the interview from proceeding.
    """
    text = (answer_text or "").strip()
    if not text:
        return True
    try:
        result = _call_chat_with_retry(
            user_id=user_id, problem_id="mock-interview-history-pref",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "The candidate was just asked, at the end of a mock interview's "
                        "opening greeting: \"Would you like me to check back on a few "
                        "areas you found tricky last time, or would you rather start "
                        "completely fresh today?\" Classify their reply below.\n\n"
                        'Respond with ONLY a JSON object: {"use_history": true|false}. '
                        "If the reply doesn't clearly answer the question (off-topic, "
                        'unclear, garbled transcription, etc.), respond {"use_history": true}.'
                    ),
                },
                {"role": "user", "content": text[:500]},
            ],
            max_tokens=30, json_mode=True,
        )
        parsed = _parse_json_reply(result["reply"])
        return bool(parsed.get("use_history", True))
    except Exception:
        return True


def build_opening_monologue(*, target_role: str, profile: dict, persona: str, ask_history_pref: bool = False) -> str:
    """
    [Interviewer] Static, role-templated opening -- greeting, settle-in,
    explain-it's-practice, and a short plan -- built from the profile
    analyzer's output rather than a separate LLM call. Mirrors why
    INTRO_QUESTION (main.py) is a plain constant today: the very first
    thing a candidate hears shouldn't depend on an LLM round-trip or vary
    unpredictably in tone/length -- only the plan sentence varies, and
    that's already produced cheaply by analyze_candidate_profile's
    opening_note, no extra LLM call needed here.

    This is always turn 1, spoken before -- and independently of --
    whichever question actually opens the interview proper (the hardcoded
    "introduce yourself" question, or a live-generated first question when
    skip_intro is set).

    ask_history_pref=True (only when this candidate has real
    interview_topic_history) appends an oral question asking whether to
    weight this session toward past weak areas or ignore that history --
    `profile` passed in here is deliberately the history-agnostic version,
    so this monologue never presupposes an answer the candidate hasn't
    given yet. See api_interview_start/api_interview_answer in main.py for
    where the candidate's spoken reply gets classified (classify_history_
    preference) and, if requested, folded into a second analyze_candidate_
    profile call before the real first question is asked.
    """
    # The settle-in line is the one sentence a candidate hears before any
    # question is asked, so it's the cheapest place to make persona actually
    # perceptible from turn 1 -- QA found this templated monologue silently
    # ignored the persona argument entirely, giving every persona (including
    # "strict") the same "take a breath, no pressure" framing.
    PERSONA_SETTLE_IN = {
        "friendly": (
            "Take a breath, there's no pressure here -- this is just "
            "practice, so treat any stumble as useful information, not a "
            "verdict."
        ),
        "neutral": (
            "This is a practice interview, so treat it as a chance to "
            "rehearse out loud, not a pass/fail test."
        ),
        "strict": (
            "This will run like a real technical panel round -- I'll move "
            "quickly and press on gaps, so treat it as practice for the "
            "real thing, not a casual chat."
        ),
    }
    settle_in = PERSONA_SETTLE_IN.get(persona, PERSONA_SETTLE_IN["neutral"])
    monologue = (
        f"Hi, thanks for joining -- welcome to your practice interview for "
        f"a {target_role} role. {settle_in} "
        f"{profile.get('opening_note', '')} We'll go through "
        "a few areas, and I'll follow up where it's useful."
    )
    if ask_history_pref:
        monologue += (
            " One quick thing first, since you've interviewed with us before: "
            "would you like me to check back on a few areas you found tricky "
            "last time, or would you rather start completely fresh today? "
            "Just tell me either way."
        )
    else:
        monologue += " Whenever you're ready, let's get started."
    return monologue


def _interview_system_prompt(
    topics: list[str],
    resume_text: str | None,
    current_topic: str | None = None,
    topic_turn_count: int = 0,
    max_turns_per_topic: int = 3,
    forced_topic: str | None = None,
    persona: str = "neutral",
    target_role: str = "Data Analyst",
    candidate_profile: dict | None = None,
) -> str:
    # [Profile Analyzer output consumed here] candidate_profile is computed
    # ONCE at /api/interview/start by analyze_candidate_profile() -- domain/
    # seniority/key_skills are stated directly rather than re-derived from
    # raw resume text every single turn (expensive to redo, and unreliable
    # to ask the model to silently re-infer identically turn after turn).
    # Falls back to the old raw-resume-dump behavior only if no profile was
    # computed (shouldn't happen in normal operation post-Phase-1, but
    # cheap insurance against a missing profile rather than a hard crash).
    resume_block = ""
    if candidate_profile:
        key_skills = ", ".join(candidate_profile.get("key_skills") or []) or "none specifically mentioned"
        resume_block = (
            "The candidate's profile has already been analyzed once -- trust "
            f"this, do not re-derive it yourself: domain/industry: "
            f"{candidate_profile.get('domain', 'general')}; apparent "
            f"seniority: {candidate_profile.get('seniority', 'mid')}; key "
            f"skills/tools mentioned: {key_skills}. Apply this mechanically, "
            "every question:\n"
            "- Every invented table_context (schema, sample rows, the "
            "scenario in your question) must be grounded in their actual "
            "domain -- an e-commerce background gets orders/customers/"
            "products tables, a marketing background gets campaigns/"
            "impressions/conversions, and so on. Never fall back to "
            "generic/unrelated placeholder tables (e.g. a plain "
            "'employees' or 'students' table) when the profile gives you a "
            "real domain to work with.\n"
            "- Calibrate difficulty to their seniority: junior/entry-level "
            "gets more foundational questions with follow_up used to build "
            "up gradually; senior gets harder opening questions and more "
            "probe (edge cases, performance, trade-offs) rather than "
            "hand-holding.\n"
            "- When natural, reference a concrete specific (a tool, project "
            "type, or their domain) in your acknowledgment or question "
            "framing -- not a vague nod like 'given your background'.\n"
            "Still cover the topics below regardless of domain -- this "
            "shapes the scenarios and difficulty, not which topics are in "
            "scope. Don't just ask about their resume verbatim.\n\n"
        )
    elif resume_text:
        resume_block = (
            "The candidate's resume/background is below. Before your first "
            "question, work out silently (do not narrate this): (1) their "
            "apparent domain/industry -- e.g. e-commerce, fintech, "
            "marketing, logistics, healthcare, SaaS; (2) their role focus "
            "-- analytics/reporting, data engineering/pipelines, or product "
            "analytics; (3) their apparent seniority from years of "
            "experience and title. Apply all three mechanically, every "
            "question:\n"
            "- Every invented table_context (schema, sample rows, the "
            "scenario in your question) must be grounded in their actual "
            "domain -- an e-commerce background gets orders/customers/"
            "products tables, a marketing background gets campaigns/"
            "impressions/conversions, and so on. Never fall back to "
            "generic/unrelated placeholder tables (e.g. a plain "
            "'employees' or 'students' table) when the resume gives you a "
            "real domain to work with.\n"
            "- Calibrate difficulty to their seniority: junior/entry-level "
            "or a short work history gets more foundational questions with "
            "follow_up used to build up gradually; senior or "
            "multi-year experience gets harder opening questions and more "
            "probe (edge cases, performance, trade-offs) rather than "
            "hand-holding.\n"
            "- When natural, reference a concrete specific from the resume "
            "(a tool, project type, or prior company's domain) in your "
            "acknowledgment or question framing -- not a vague nod like "
            "'given your background'.\n"
            "Still cover the topics below regardless of domain -- "
            "this shapes the scenarios and difficulty, not which "
            "topics are in scope. Don't just ask about their resume "
            "verbatim.\n\n"
            f"{resume_text[:4000]}\n\n"
        )

    topic_budget_block = ""
    if forced_topic and current_topic is None:
        # skip_intro's opening topic, forced explicitly by the caller
        # rather than left to the model's own judgment -- see main.py's
        # api_interview_start. Distinct wording from the budget-exhausted
        # case below: there's no real "budget used up" here, and the
        # opening monologue (not a prior topic) is what came before this.
        topic_budget_block = (
            "This is the opening real question of the interview. "
            "skip_intro was requested, meaning the opening monologue "
            "already handled greetings and introductions -- do NOT ask "
            "the candidate to introduce themselves, describe their "
            "background, or name tools/experience. Your action MUST be "
            f"\"switch_topic\" and your topic MUST be exactly "
            f"\"{forced_topic}\" -- ask one concrete, substantive question "
            "on that topic now, phrased as if this were already the "
            "second question of an interview in progress, not an "
            "opener.\n\n"
        )
    elif forced_topic:
        # Cap already reached -- this is not a judgment call, it's a direct
        # instruction. Leaving it as "decide whether to switch" repeatedly
        # let the model just keep following up past the limit.
        topic_budget_block = (
            f"The topic budget for '{current_topic}' is used up. Do NOT ask "
            f"anything more about it. Your action MUST be \"switch_topic\" and "
            f"your topic MUST be exactly \"{forced_topic}\" -- write an "
            f"opening question for that new topic now.\n\n"
        )
    elif current_topic is None:
        # skip_intro's very first live-generated turn -- confirmed live to
        # otherwise default to asking an introduce-yourself-style question
        # anyway (ignoring skip_intro's whole point) while being forced to
        # label it with a real topic name from the list below, since
        # nothing here told it not to. The opening monologue already
        # covered introductions when skip_intro was requested; there's no
        # separate intro turn to skip into.
        topic_budget_block = (
            "This is the very first question of the interview. skip_intro "
            "was requested, meaning the opening monologue already handled "
            "greetings and introductions -- do NOT ask the candidate to "
            "introduce themselves, describe their background, walk through "
            "their experience, or name tools/technologies they've used. "
            "Forbidden opener patterns, even rephrased: \"tell me about "
            "your experience/background\", \"walk me through your "
            "experience with X\", \"what tools have you worked with\". Go "
            "straight into a concrete, substantive question ON ONE OF THE "
            "TOPICS BELOW -- e.g. a specific SQL scenario, or a specific "
            "conceptual/case question -- as if this were the second "
            "question of an interview already in progress, not the "
            "opener.\n\n"
        )
    elif current_topic == "intro":
        topic_budget_block = (
            "You are on the opening \"introduce yourself\" question. This is a "
            "brief icebreaker, not a real interview topic with a multi-question "
            "budget -- at most one natural follow-up if the answer was "
            "genuinely too thin to work with (see the guidance above on when "
            "that's warranted), then move to the first real topic below.\n\n"
        )
    elif current_topic:
        topic_budget_block = (
            f"You are currently on '{current_topic}' -- {topic_turn_count} "
            f"question(s) asked so far, {max_turns_per_topic} max before "
            "you must move to a new topic. Keep that budget in mind when "
            "choosing follow_up/probe vs switch_topic.\n\n"
        )

    conceptual_in_scope = [t for t in topics if role_topics.is_conceptual(t)]
    conceptual_note = ""
    if conceptual_in_scope:
        conceptual_note = (
            "The following topics from the list below are conceptual/"
            f"business-discussion topics, not SQL query-technique topics: "
            f"{', '.join(conceptual_in_scope)}. Whenever the current or "
            "newly-chosen topic is one of these, do NOT invent a "
            "table_context -- set table_context to null always for these, "
            "and if the scenario needs supporting numbers (e.g. a metric "
            "dropped 15% last quarter), state them directly in the spoken "
            "question instead of a table.\n\n"
        )

    return (
        f"You are conducting a live, spoken interview for a candidate "
        f"applying to a {target_role} role. Ask ONE question at a time, in "
        "natural spoken language -- no markdown, no "
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
        "The opening \"introduce yourself\" question deserves judgment, not a fixed "
        "script. If the candidate declines or can't answer at all -- e.g. \"I'd "
        "rather not\" or \"I don't really know what to say\" -- that's not a "
        "technical gap -- respond with extra warmth (nerves are normal) and ease "
        "straight into the easiest, most approachable SQL question you can think "
        "of (a plain single-table SELECT, nothing with multiple conditions or "
        "unfamiliar concepts yet), not a moderately complex one. Save harder "
        "scenarios for once they've found their footing.\n\n"
        "If instead they do answer but it's too thin to actually tell you "
        "anything -- e.g. \"I've done some SQL stuff before\" or \"yeah I've "
        "worked with databases a bit\" -- pick follow_up on \"intro\" with ONE "
        "light, natural question to get just enough to calibrate on (what role or "
        "tools they used it in, or how long), the way a human interviewer would "
        "-- not an interrogation. Do not stack a second follow-up on top of that "
        "one; move to the first real SQL topic right after, whatever they say "
        "next.\n\n"
        "If their answer already gives you something concrete to calibrate on -- "
        "e.g. \"I've been a data analyst for two years, mostly Postgres and some "
        "dbt\" -- that's already enough. Acknowledge it and move straight to the "
        "first real SQL topic; do not follow up on the intro just to dig for more "
        "detail, that reads as stalling rather than interviewing.\n\n"
        f"{PERSONA_TONE.get(persona, '')}"
        f"{resume_block}"
        f"{topic_budget_block}"
        f"{conceptual_note}"
        f"Topics to cover across the interview: {', '.join(topics)}.\n\n"
        "The `topic` field in your JSON response MUST be exactly one of "
        "those topic names (exact spelling), never an invented or "
        "paraphrased label -- this is what's used to track how long you've "
        "spent on each topic, so an inconsistent label breaks that "
        "tracking. The one exception: when your action is follow_up and "
        "you are staying on the opening introduction itself (candidate "
        "hasn't given you enough yet), the topic field MUST be exactly "
        "\"intro\" instead. The moment your action is switch_topic -- "
        "including switching away from the intro to begin the technical "
        "portion -- the topic field MUST be a real topic name from the "
        "list above, never \"intro\", since the whole point of switching "
        "is to move onto one of those. On follow_up/probe on a real topic, "
        "reuse the SAME topic string you (or the topic_budget note above) "
        "were already given for the current topic.\n\n"
        "For SQL query-technique topics (not the conceptual topics noted "
        "above, if any): whenever your question refers to a table (e.g. "
        "'suppose you have a table called orders...'), you MUST invent a "
        "concrete schema and a handful of sample rows for it, and put them "
        "in `table_context` -- never leave the candidate to imagine column "
        "names or data on their own, they need to actually see it on "
        "screen. Reuse the SAME table (set table_context to null) for "
        "follow_up or probe questions still about that table; only invent "
        "a new table_context when you switch_topic to a scenario that "
        "needs a different table, or for the very first question.\n\n"
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
        "Only ask about topics from the list above -- SQL/database concepts "
        f"and the role-relevant analytical/business concepts appropriate to "
        f"a {target_role} interview. If the candidate goes off-topic, "
        "gently redirect back to the interview.\n\n"
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
        "When their answer was wrong or only partially correct -- not a "
        "genuine non-attempt, that's candidate_stuck above -- decide "
        "whether a hint would actually help before just asking a plain "
        "follow_up. If so, set `offer_hint` to true and phrase the "
        "follow_up's `question` to include ONE hint or a simpler restated "
        "version of the question, the way a good interviewer nudges "
        "someone back on track rather than just repeating the ask. Never "
        "offer a second hint on the same topic -- if they're still stuck "
        "after one hint, that's the same as giving up: move on with "
        "switch_topic rather than hinting again.\n\n"
        "If the candidate's answers are consistently very short, broken, "
        "or show signs of language difficulty -- not to be confused with "
        "candidate_stuck's genuine non-attempts -- simplify your own "
        "phrasing to plain, simple English, and focus your follow-ups on "
        "whether they grasp the underlying concept rather than precise "
        "terminology or exact phrasing.\n\n"
        "Respond with ONLY a JSON object, no other text, no markdown code "
        'fences: {"action": "follow_up"|"probe"|"switch_topic", "topic": '
        '"<topic name>", "question": "<your next spoken question>", '
        '"candidate_stuck": true|false, "offer_hint": true|false, '
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


def reclassify_topics_batch(*, user_id: str, problems: list[dict], allowed_topics: list[str], track: str) -> dict:
    """
    Audits a batch of already-live problems for topic mislabeling -- e.g.
    a plain `SELECT DISTINCT category` (no string functions at all)
    tagged "Working with Strings" just because the model that originally
    drafted it reached for that label without it actually fitting.
    Classification is based on the real canonical_sql/canonical_solution,
    never the title or scenario framing, which is what drifts from the
    real technique in the first place. Returns {"results": [{"id",
    "topic"}, ...], "usage": {...}} -- callers compare each returned
    topic against the problem's current one and only write an update
    where they actually differ.
    """
    blocks = []
    for p in problems:
        if track == "sql":
            code = p["canonical_sql"]
        else:
            # test_code carries the import (`import numpy as np`, etc.)
            # in most drafts, not canonical_solution -- the solution
            # function itself often just operates on whatever
            # array/DataFrame test_code hands it, with no import of its
            # own. Both are needed for an accurate call.
            code = f'{p["canonical_solution"]}\n\n# Test code (shows real inputs/imports):\n{p["test_code"]}'
        blocks.append(
            f'ID: {p["id"]}\nCurrent label: {p["topic"]}\nTitle: {p["title"]}\n'
            f'Code:\n{code}'
        )
    user_prompt = "\n\n---\n\n".join(blocks)
    system_prompt = (
        "You are auditing a practice-problem bank for a real, specific "
        "defect: some problems are labeled with a topic that doesn't "
        "actually match what the code tests -- e.g. a plain `SELECT "
        "DISTINCT category` (no string functions at all) mislabeled "
        "'Working with Strings' just because the model that drafted it "
        "reached for that label without it fitting. For each problem "
        "below, determine the ONE topic from the allowed list that most "
        "accurately describes the real technique in the code -- base "
        "this on the code itself, never the title or business framing, "
        "since those are exactly what drift from the real technique. If "
        "the current label is already correct, return it unchanged.\n\n"
        f"Allowed topics (choose exactly one per problem, copied "
        f"verbatim): {', '.join(allowed_topics)}\n\n"
    )
    if track == "python":
        system_prompt += (
            "This list mixes general Python topics with pandas, numpy, "
            "and statistics topics -- a real failure mode is reaching "
            "for the 'fancier'-sounding specific topic based on loose "
            "scenario association (a function that counts/groups things "
            "per day 'sounds' statistics-y) rather than the actual code. "
            "Hard rules, in order:\n"
            "1. NEVER choose a Pandas topic unless the code actually "
            "creates or operates on a pandas DataFrame/Series (imports "
            "pandas, uses `pd.`, `.groupby`, `.merge`, etc.).\n"
            "2. NEVER choose a NumPy topic unless the code actually "
            "creates or operates on a numpy array (imports numpy, uses "
            "`np.`).\n"
            "3. NEVER choose a statistics topic (Descriptive Statistics, "
            "Hypothesis Testing, etc.) unless the code computes an "
            "actual statistical measure -- mean, median, variance, "
            "standard deviation, a percentile/quantile, a correlation, "
            "a p-value/test statistic, a confidence interval, or a "
            "probability distribution. Counting items, grouping by a "
            "key, or tallying frequencies with plain dicts/sets is NOT "
            "statistics on its own -- that's Data Structures and "
            "Algorithms, or Data Encoding and Processing, whichever the "
            "code more literally does.\n"
            "4. Only after ruling out 1-3 does a general Python Cookbook "
            "topic (Data Structures and Algorithms, Strings and Text, "
            "Numbers/Dates/Times, Iterators and Generators, Files and "
            "I/O, Data Encoding and Processing, Functions, Classes and "
            "Objects, Metaprogramming, Modules and Packages, Testing/"
            "Debugging/Exceptions) apply.\n\n"
        )
    system_prompt += (
        "Respond with ONLY a JSON object, no other text: "
        '{"results": [{"id": "...", "topic": "<one allowed topic, exact>"}, ...]} '
        "-- exactly one entry per problem given."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    result = _call_chat_with_retry(
        user_id=user_id, problem_id="admin-topic-reclassify", messages=messages,
        max_tokens=min(3000, max(800, len(problems) * 120)), json_mode=True,
        timeout=90,
    )
    parsed = _parse_json_reply(result["reply"])
    return {"results": parsed.get("results", []), "usage": result["usage"]}


def audit_problem_quality(
    *,
    user_id: str,
    problem: dict,
    allowed_topics: list[str],
    correctness: dict,
    ask_phoenix_normal: dict | None = None,
    ask_phoenix_edge: dict | None = None,
) -> dict:
    """
    Full content-quality judge for one already-live problem -- unlike
    reclassify_topics_batch (topic-only, code-only), this looks at
    everything a human reviewer would: is the question actually
    interview/FAANG-grade, is the topic label right, does the description
    give students everything they need (the exact bug class found earlier
    -- "load a CSV" with no filename), is the stated difficulty realistic,
    does the stored sample input/output look sane, and (SQL/Python only)
    did Ask Phoenix handle a normal question and an edge-case question
    well for this specific problem.

    `correctness` is the caller's own objective pass/fail result (from
    pysandbox.run_python_submission / sandbox.compute_expected_output --
    never re-derived here, since that's a deterministic check an LLM
    shouldn't be asked to guess at). Pass {"passed": None, "detail": "..."}
    for track='case', which has no single verifiable answer to check.

    `ask_phoenix_normal`/`ask_phoenix_edge` are {"question": str,
    "answer": str} pairs already captured by calling llm.ask_phoenix()
    twice before this function runs. Omit both (leave as None) for
    track='case' -- Ask Phoenix is deliberately not offered on that track
    (the whole point of a case round is figuring it out yourself), so
    there's nothing to judge there; the prompt and response schema below
    are built dynamically to drop those two dimensions entirely rather
    than asking the judge to grade something that was never run.

    Returns {"verdict": {...}, "usage": {...}}. On a malformed reply,
    returns a "needs_fix" verdict with the raw reply in `notes` rather than
    raising -- one bad judge call should surface as a flagged-for-review
    row in the audit, not silently drop the problem from the report or
    crash the whole batch.
    """
    has_ask_phoenix = ask_phoenix_normal is not None and ask_phoenix_edge is not None
    is_case = problem.get("track") == "case"

    if problem.get("track") == "python":
        content_block = (
            f"Starter code:\n{problem.get('starter_code') or ''}\n\n"
            f"Function signature: {problem.get('function_signature')}\n\n"
            f"Canonical solution:\n{problem.get('canonical_solution')}\n\n"
            f"Test code:\n{problem.get('test_code')}"
        )
    elif problem.get("track") == "case":
        content_block = (
            f"Case prompt:\n{problem.get('case_prompt')}\n\n"
            f"Supporting context:\n{problem.get('case_context') or '(none)'}\n\n"
            f"Rubric points (internal, never shown to students):\n"
            + "\n".join(f"- {pt}" for pt in (problem.get('rubric_points') or [])) + "\n\n"
            f"Sample strong answer (internal, never shown to students):\n{problem.get('sample_strong_answer')}"
        )
    else:
        content_block = (
            f"Schema SQL:\n{problem.get('schema_sql')}\n\n"
            f"Seed SQL:\n{problem.get('seed_sql')}\n\n"
            f"Canonical SQL:\n{problem.get('canonical_sql')}"
        )

    if correctness.get("passed") is None:
        correctness_block = (
            "Objective correctness check: N/A -- this track has no single "
            "verifiable answer to execute."
        )
    else:
        correctness_block = (
            f"Objective correctness check (already run, not your judgment to make): "
            f"{'PASSED' if correctness.get('passed') else 'FAILED'}"
            f"{' -- ' + correctness['detail'] if correctness.get('detail') else ''}"
        )

    ask_phoenix_block = ""
    if has_ask_phoenix:
        ask_phoenix_block = (
            f"\n\n--- Ask Phoenix transcript 1 (normal question) ---\n"
            f"Student asked: {ask_phoenix_normal['question']}\n"
            f"Phoenix answered: {ask_phoenix_normal['answer']}\n\n"
            f"--- Ask Phoenix transcript 2 (edge-case question) ---\n"
            f"Student asked: {ask_phoenix_edge['question']}\n"
            f"Phoenix answered: {ask_phoenix_edge['answer']}"
        )

    user_prompt = (
        f"ID: {problem['id']}\n"
        f"Title: {problem['title']}\n"
        f"Stated difficulty: {problem['difficulty']}\n"
        f"Stated topic: {problem['topic']}\n"
        f"Tags: {', '.join(problem.get('tags') or [])}\n\n"
        f"Description shown to students:\n{problem['description']}\n\n"
        f"{content_block}\n\n"
        f"Sample input/output currently shown to students:\n{json.dumps(problem.get('examples'))}\n\n"
        f"{correctness_block}"
        f"{ask_phoenix_block}"
    )

    system_prompt = (
        "You are auditing one already-live problem on an interview-prep "
        "platform for real, specific defects a careful human reviewer "
        "would catch -- not a rubber stamp. Judge each dimension below on "
        "its own merits; do not let a good score on one inflate another.\n\n"
        "1. faang_style: would this question feel at home in a real "
        "FAANG/big-tech technical screen -- realistic scenario, a genuine "
        "technique being tested, not busywork or a trick of wording? "
        "A problem that's really just checking whether the student read "
        "carefully, with no real technical substance, fails this.\n\n"
        "2. topic_alignment: does the code (not the title or scenario "
        "framing) actually match the stated topic? Choose the single best "
        "topic from this allowed list, exact spelling, even if it's the "
        f"same as the stated one: {', '.join(allowed_topics)}\n\n"
        + (
            "3. description_sufficient -- READ THIS CAREFULLY, every case "
            "problem on this platform is intentionally open-ended and that "
            "is NOT a defect. Exactly this shape is a CORRECT, PASSING "
            "description, not something to flag: \"The company currently "
            "defines retention as the percentage of customers who renew "
            "after one year. They want a better metric. What changes would "
            "you suggest?\" -- notice it does NOT list which user behaviors, "
            "signals, or factors to consider; the candidate proposing those "
            "themselves is the entire skill this rubric exists to test "
            "(e.g. a rubric point like \"identifies relevant behaviors "
            "that indicate engagement\" only makes sense if the description "
            "never named them first). You are ONLY allowed to fail this for "
            "one of these two reasons -- if your objection is not clearly "
            "one of these two, you MUST set ok: true instead:\n"
            "  (a) the scenario references a specific system, metric, "
            "dataset, or term by name that is never explained anywhere "
            "(e.g. \"using our standard XR-score\" with no definition of "
            "what that is);\n"
            "  (b) the question itself is genuinely unclear about what's "
            "being asked (not what factors to weigh -- that's supposed to "
            "be open -- but literally what task the candidate is being "
            "asked to do).\n"
            "Any complaint of the form \"doesn't specify which factors/"
            "behaviors/metrics/approach to use\", \"lacks guidance on\", "
            "or \"doesn't provide criteria for\" is NEVER valid grounds to "
            "fail this field -- that is the open-endedness working as "
            "designed, not a gap. If that's your only objection, set ok: "
            "true.\n\n"
            if problem.get("track") == "case" else
            "3. description_sufficient: does the description give the student "
            "literally everything needed to solve it -- no unnamed referenced "
            "file/table/variable, no missing constraint, no ambiguity about "
            "what the output should look like? A description that assumes "
            "context the student can't see (e.g. \"load the CSV\" without ever "
            "naming it) fails this, even if the underlying problem is fine.\n\n"
        )
        + "4. difficulty_correct: is 'easy'/'medium'/'hard' realistic given "
        "what the canonical solution actually has to do? Suggest a "
        "replacement (exactly one of easy/medium/hard) only if you think "
        "the current label is wrong.\n\n"
        + (
            ""
            if is_case else
            "5. sample_io_sane -- READ THIS CAREFULLY. Every Python arg/result "
            "value here is the output of calling Python's own repr() on a "
            "real value, always stored as a JSON string, NEVER as a native "
            "JSON array/object/number. repr() output looks exactly like the "
            "underlying Python/library syntax, quotes, brackets and all -- "
            "e.g. all of the following are 100% CORRECT, ordinary repr() "
            "output and NONE of them are defects: \"[{'a': 1}, {'a': 2}]\" "
            "(a list of dicts, single-quoted, as a string), \"array([1, 2, "
            "3])\" (a numpy array), \"np.int64(5)\" or \"np.float64(2.5)\" "
            "(numpy scalar types), \"'2023-01-01'\" (a date kept as a plain "
            "string, quotes included), \"defaultdict(<class 'int'>, {'a': "
            "1})\" (a defaultdict). None of these are formatting mistakes -- "
            "they are simply what repr() produces for that value's actual "
            "Python type, and matching the real type (int vs float, str vs "
            "date object, etc.) is not something to second-guess here.\n\n"
            "You are ONLY allowed to fail sample_io_sane for one of these "
            "four reasons -- if your objection is not clearly one of these "
            "four, you MUST set ok: true instead:\n"
            "  (a) the example is completely empty/absent when the problem "
            "shape clearly could have produced one;\n"
            "  (b) the repr'd value is a raw memory address, i.e. literally "
            "contains the substring \"object at 0x\";\n"
            "  (c) the repr'd value is a bare, unconsumed generator, i.e. "
            "literally contains the substring \"<generator object\";\n"
            "  (d) the actual numbers/values shown are factually wrong given "
            "the description and canonical solution (e.g. a claimed sum that "
            "doesn't add up, or a count that's off) -- not a style, type, or "
            "quoting objection, an actual wrong-arithmetic objection.\n"
            "Any complaint about quote style, JSON validity, str-vs-int-vs-"
            "float typing, numpy/defaultdict/date-as-string reprs, or "
            "\"should be a list/dict not a string\" is NEVER valid grounds to "
            "fail this field -- if that's your only objection, set ok: true.\n\n"
        )
        + (
            "6. ask_phoenix_normal_ok / ask_phoenix_edge_ok: judge each Ask "
            "Phoenix transcript independently. Phoenix has two legitimate "
            "modes: GUIDE (describe concepts in words, never hand over the "
            "finished answer) for questions that don't explicitly ask for the "
            "full solution, and COMPLY FULLY (write the real, complete answer) "
            "the moment a question unambiguously asks for it (\"just give me "
            "the code/query\", \"show me the full answer\"). Either mode is "
            "correct behavior for the RIGHT kind of question -- only fail a "
            "transcript if Phoenix picked the wrong mode for what was actually "
            "asked, gave a wrong/misleading technical answer, or the answer "
            "doesn't actually address this specific problem.\n\n"
            if has_ask_phoenix else ""
        )
        + "Weigh all of the above into overall_verdict: \"pass\" only if "
        "there is no real issue worth a human's attention; \"needs_fix\" "
        "if anything above would genuinely need a human to look at it "
        "before shipping this to real candidates.\n\n"
        "Respond with ONLY a JSON object, no other text, exactly this shape: "
        '{"faang_style": {"ok": bool, "reason": "..."}, '
        '"topic_alignment": {"ok": bool, "suggested_topic": "<exact topic>"}, '
        '"description_sufficient": {"ok": bool, "reason": "..."}, '
        '"difficulty_correct": {"ok": bool, "suggested_difficulty": "easy|medium|hard"}, '
        + ('' if is_case else '"sample_io_sane": {"ok": bool, "reason": "..."}, ')
        + ('"ask_phoenix_normal_ok": {"ok": bool, "reason": "..."}, '
           '"ask_phoenix_edge_ok": {"ok": bool, "reason": "..."}, ' if has_ask_phoenix else '')
        + '"overall_verdict": "pass|needs_fix", '
        '"notes": "..."}'
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    result = _call_chat_with_retry(
        user_id=user_id, problem_id=f"audit-{problem['id']}", messages=messages,
        max_tokens=1200, json_mode=True, timeout=60,
    )
    try:
        verdict = _parse_json_reply(result["reply"])
    except (json.JSONDecodeError, KeyError):
        verdict = {"overall_verdict": "needs_fix", "notes": f"Judge reply did not parse as JSON: {result['reply'][:500]}"}
    return {"verdict": verdict, "usage": result["usage"]}


DISCRIMINATING_TEST_CASE_SYSTEM_PROMPT = (
    "You harden SQL interview-practice problems against a specific grading "
    "weakness: a query with fundamentally wrong logic can coincidentally "
    "produce the right rows on a single fixed dataset, and get graded "
    "correct even though it doesn't actually solve the problem in general.\n\n"
    "Given a problem's schema_sql, canonical_sql (the real correct "
    "solution), and description, your job is to identify plausible wrong "
    "approaches a candidate might take for THIS specific problem, drawn "
    "from real interview gotchas: a NOT IN list that silently returns zero "
    "rows because it contains a NULL, a JOIN that quietly duplicates rows "
    "before an aggregate runs over them, COUNT(column) vs COUNT(*) "
    "disagreeing because of NULLs, integer division truncating when a "
    "decimal was expected, a LEFT JOIN condition placed in WHERE instead of "
    "ON silently turning it into an INNER JOIN, GROUP BY with a wrong or "
    "missing column, a missing filter, the wrong aggregate function, an "
    "off-by-one date/rank boundary, or a similarly plausible mistake for "
    "this problem's specific logic.\n\n"
    "For each wrong approach, produce TWO things: (1) `seed_sql` -- a "
    "dataset (INSERT statements only, matching the given schema_sql "
    "exactly) specifically constructed so that the wrong approach and the "
    "real canonical_sql produce DIFFERENT results on it, while "
    "canonical_sql still produces a sensible, correct answer; (2) "
    "`wrong_query` -- the actual incorrect SQL a candidate making that "
    "mistake would write (one valid SELECT/WITH statement, syntactically "
    "correct DuckDB SQL, just logically wrong in the intended way).\n\n"
    "Every seed_sql must be substantively different data from every other "
    "one you produce, not just a relabeling -- vary row counts, which rows "
    "have NULLs, which values are duplicated, and boundary values, so each "
    "dataset is targeted at unmasking its own specific wrong approach.\n\n"
    "Respond with ONLY a JSON object, no other text, no markdown code "
    'fences: {"test_cases": [{"targets": "one-line description of the '
    'wrong approach", "seed_sql": "...", "wrong_query": "..."}, ...]}'
)


def generate_discriminating_test_cases(*, user_id: str, problem: dict, count: int) -> dict:
    """
    Produces up to `count` additional hidden seed datasets for a SQL
    problem, each adversarially constructed (by the LLM) and then
    self-validated (in-process, via sandbox.py -- no extra LLM call) to
    actually distinguish the real canonical_sql from a specific plausible
    wrong query. This is the SQL-track answer to "only one logic should
    solve them all": a wrong-but-plausible student query would have to
    coincidentally agree with canonical_sql on every one of these
    purpose-built datasets, not just one arbitrary fixed dataset.

    Validation, per candidate: run canonical_sql and wrong_query against
    {schema_sql, seed_sql} (the same in-process DuckDB path every real
    submission uses -- sandbox._execute). Accept only if canonical_sql
    executes cleanly and wrong_query's output differs from it under the
    same value-only diff (sandbox.compare_results) a real submission would
    get. Retries each rejected slot up to 2 times before giving up on it --
    a slot that never validates is simply omitted from the result rather
    than accepted with a dataset that doesn't do its job.

    Returns {"validated": [{"seed_sql": str, "defeats_wrong_query": str}, ...],
    "requested": count, "usage": {...}} -- `usage` is the token usage from
    the single LLM call that produced (and, on retries, re-produced) the
    candidates; callers decide what to do if len(validated) < requested
    (flag the problem for manual review rather than silently accepting
    weaker coverage than intended).
    """
    order_matters = problem.get("order_matters", False)
    user_prompt = (
        f"schema_sql:\n{problem['schema_sql']}\n\n"
        f"canonical_sql:\n{problem['canonical_sql']}\n\n"
        f"description:\n{problem.get('description', '')}\n\n"
        f"Produce exactly {count} (targets, seed_sql, wrong_query) triples."
    )
    messages = [
        {"role": "system", "content": DISCRIMINATING_TEST_CASE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    validated: list[dict] = []
    seen: set[tuple[str, str]] = set()  # (normalized seed_sql, wrong_query) already accepted
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}
    remaining = count
    attempts = 0
    while remaining > 0 and attempts < 3:
        attempts += 1
        try:
            result = _call_chat_with_retry(
                user_id=user_id, problem_id=f"discriminating-test-cases-{problem.get('id', 'draft')}",
                messages=messages, max_tokens=min(6000, max(1200, remaining * 700)),
                json_mode=True, temperature=0.8,
            )
            for k in total_usage:
                total_usage[k] += result["usage"].get(k, 0)
            candidates = _parse_json_reply(result["reply"]).get("test_cases", [])
        except Exception:
            break

        for c in candidates:
            if len(validated) >= count:
                break
            seed_sql = c.get("seed_sql")
            wrong_query = c.get("wrong_query")
            if not seed_sql or not wrong_query:
                continue
            # A retry re-sends the same prompt to fill the remaining slots
            # -- without this, a model that returns a near-identical
            # candidate on retry would pad `validated` with a duplicate
            # that adds zero real discriminative value instead of a
            # genuinely new dataset.
            dedup_key = (" ".join(seed_sql.split()).lower(), " ".join(wrong_query.split()).lower())
            if dedup_key in seen:
                continue
            case_problem = {"schema_sql": problem["schema_sql"], "seed_sql": seed_sql}
            try:
                expected_columns, expected_rows, _ = sandbox._execute(case_problem, problem["canonical_sql"])
            except Exception:
                continue  # canonical_sql itself failed on this dataset -- reject it
            try:
                wrong_columns, wrong_rows, _ = sandbox._execute(case_problem, wrong_query)
            except Exception:
                # The wrong query failing to execute at all is still a valid
                # "differs from expected" signal -- accept the dataset.
                validated.append({"seed_sql": seed_sql, "defeats_wrong_query": wrong_query})
                seen.add(dedup_key)
                continue
            is_correct, _diff = sandbox.compare_results(
                expected_columns, expected_rows, wrong_columns, wrong_rows, order_matters,
            )
            if not is_correct:
                validated.append({"seed_sql": seed_sql, "defeats_wrong_query": wrong_query})
                seen.add(dedup_key)

        remaining = count - len(validated)

    return {"validated": validated[:count], "requested": count, "usage": total_usage}


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
    target_role: str = "Data Analyst",
    candidate_profile: dict | None = None,
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
        {"action": str, "topic": str, "question": str, "candidate_stuck": bool,
         "offer_hint": bool, "table_context": dict | None, "usage": {...}}
    Falls back to a generic switch_topic question if the model's reply isn't
    valid JSON, so a single malformed response doesn't break the interview.
    """
    messages = [{"role": "system", "content": _interview_system_prompt(
        topics, resume_text, current_topic, topic_turn_count, forced_topic=forced_topic, persona=persona,
        target_role=target_role, candidate_profile=candidate_profile,
    )}]
    if conversation:
        # Chat APIs only accept {role, content} -- strip our extra "topic" bookkeeping field.
        messages.extend({"role": t["role"], "content": t["content"]} for t in conversation)
    else:
        messages.append({"role": "user", "content": "Begin the interview with the first question."})

    result = _call_chat_with_retry(user_id=user_id, problem_id="mock-interview", messages=messages, max_tokens=700, json_mode=True)
    try:
        parsed = _parse_json_reply(result["reply"])
        action = "switch_topic" if forced_topic else parsed.get("action", "switch_topic")
        topic = forced_topic or parsed.get("topic", topics[0])
        # "intro" is only ever a valid label while STAYING on the intro
        # (action == follow_up) -- despite the prompt spelling this out
        # explicitly, the model has been observed to still echo "intro"
        # back on a switch_topic action meant to move onto a real topic,
        # which would otherwise leave the session's topic tracking stuck
        # believing it's still on the intro indefinitely. Same "don't
        # trust the model's own bookkeeping" precedent as forced_topic
        # above and the candidate_stuck override in main.py.
        if action == "switch_topic" and topic not in topics:
            topic = topics[0]
        # Same "intro" mislabeling, different action: QA found the model
        # will sometimes echo topic="intro" on a follow_up turn even long
        # after skip_intro started the interview on a real topic (i.e.
        # current_topic was never actually "intro" this session). Left
        # uncorrected, update_topic_tracking (interview.py) sees that as a
        # genuine topic change -- current_topic flips to "intro",
        # topics_covered gets polluted with a non-real topic, and the
        # candidate gets a confused "let's go back to..." moment. "intro"
        # is only ever a legitimate follow_up label when the session is
        # actually still on the intro (current_topic is None/"intro").
        if action == "follow_up" and topic == "intro" and current_topic not in (None, "intro"):
            topic = current_topic
        return {
            "action": action,
            "topic": topic,
            "question": parsed["question"],
            "table_context": parsed.get("table_context"),
            "candidate_stuck": bool(parsed.get("candidate_stuck", False)),
            "offer_hint": bool(parsed.get("offer_hint", False)),
            "usage": result["usage"],
        }
    except (json.JSONDecodeError, KeyError):
        return {
            "action": "switch_topic",
            "topic": forced_topic or topics[0],
            "question": result["reply"],
            "table_context": None,
            "candidate_stuck": False,
            "offer_hint": False,
            "usage": result["usage"],
        }


_TREND_DIP_WORDS = ("dip", "drop", "decreas", "declin", "lower", "regress", "worse", "slip")
_TREND_IMPROVE_WORDS = ("improv", "increas", "better", "progress", "grew", "grow", "stronger", "gain")
_TREND_STEADY_WORDS = ("steady", "consisten", "similar", "held", "stable", "unchanged", "same")


def _validate_trend_note(trend_note: str | None, topic_scores: list[dict], topic_history: dict | None) -> str | None:
    """[Feedback Generator] Deterministic backstop on trend_note, same
    "don't trust a free-form claim for something structurally important"
    precedent as the topic_scores coverage filter above it -- confirmed
    live: the model claimed a topic's score "dipped since their last
    interview" when the actual score was IDENTICAL both times (a real,
    plausible-sounding but factually wrong claim, exactly the kind of
    thing the prompt's "grounded ONLY in these real numbers" instruction
    was meant to prevent but can't fully guarantee on its own).

    Computes the real score deltas for every topic this interview shares
    with topic_history, then checks trend_note's own wording for a
    dip/improve/steady claim that contradicts the actual net direction --
    nulls it out on any mismatch rather than trying to rewrite it, since a
    missing trend note is far less harmful than a wrong one."""
    if not trend_note or not topic_history:
        return trend_note
    deltas = [
        t["score"] - topic_history[t["topic"]][0]["score"]
        for t in topic_scores
        if t.get("topic") in topic_history and topic_history[t["topic"]]
    ]
    if not deltas:
        return trend_note  # nothing to check it against -- leave as the model wrote it
    net = sum(deltas)
    note_lower = trend_note.lower()
    claims_dip = any(w in note_lower for w in _TREND_DIP_WORDS)
    claims_improve = any(w in note_lower for w in _TREND_IMPROVE_WORDS)
    claims_steady = any(w in note_lower for w in _TREND_STEADY_WORDS)
    if (claims_dip and net >= 0) or (claims_improve and net <= 0) or (claims_steady and net != 0):
        return None
    return trend_note


def _feedback_system_prompt(target_role: str, topic_history: dict | None = None) -> str:
    """[Feedback Generator] Builds the feedback-report system prompt,
    scoped to the role's blended topic list (not the old SQL-only
    topics.ALL_TOPICS) and, when topic_history is available, grounded in
    real past scores so trend_note can never be a fabricated comparison."""
    all_topics = role_topics.topics_for_role(target_role)
    conceptual = [t for t in all_topics if role_topics.is_conceptual(t)]

    history_block = ""
    history_lines = []
    if topic_history:
        for topic, entries in topic_history.items():
            if entries:
                scores = ", ".join(str(e["score"]) for e in entries)
                history_lines.append(f"- {topic}: past score(s), most recent first: {scores}")
    if history_lines:
        history_block = (
            "\nThis candidate has interviewed before. Their past per-topic "
            "scores:\n" + "\n".join(history_lines) + "\nWhen a topic in THIS "
            "interview overlaps with one listed above, set trend_note to a "
            "short, gentle 1-2 sentence comparison (improved, dipped, or "
            "held steady) grounded ONLY in these real numbers -- never "
            "invent or imply a comparison for a topic with no history "
            "above. If nothing overlaps, set trend_note to null.\n\n"
        )
    else:
        history_block = (
            "\nThis is this candidate's first recorded interview -- there "
            "is no prior history to compare against. Set trend_note to "
            "null; never invent a \"since last time\" comparison.\n\n"
        )

    return (
        "You are an experienced interviewer writing a feedback report "
        f"after a mock interview for a {target_role} role. Review the full "
        "transcript and produce a structured, honest but encouraging "
        "assessment for the candidate.\n\n"
        f"topics_to_study, topic_scores[].topic, question_notes[].topic, "
        "and next_practice_plan[].topic MUST only contain values from this "
        f"exact list (use the exact spelling): {', '.join(all_topics)}. "
        "Only include topics that were actually covered in the transcript "
        "in topic_scores and question_notes -- never invent scores for a "
        "topic that was never asked about.\n"
        "overall_summary, strengths, and weaknesses are free text, but the "
        "same rule applies just as strictly: every specific claim in them "
        "(a topic worked on, a technique demonstrated, an answer given) "
        "must be something that genuinely happened in the transcript above "
        "-- verifiable against a real question-and-answer exchange, not "
        "just a question that was asked. If the transcript is short (few "
        "or even zero complete exchanges), say so plainly and keep these "
        "sections brief and general rather than inventing specific "
        "technical claims to fill space -- an interview that ended after "
        "one question should never read as if it covered several.\n"
        f"{history_block}"
        "For next_practice_plan[].track: use \"case\" for a topic that's "
        "conceptual/business-discussion rather than a SQL query-technique "
        f"topic ({', '.join(conceptual) if conceptual else 'none in this role'}), "
        "otherwise \"sql\".\n\n"
        "Respond with ONLY a JSON object, no other text, no markdown code "
        'fences: {"overall_summary": "<2-3 sentence overall impression>", '
        '"score": <integer 0-100, your holistic assessment of interview performance>, '
        '"strengths": ["<point>", ...], "weaknesses": ["<point>", ...], '
        '"topics_to_study": ["<topic, from the list above>", ...], '
        '"topic_scores": [{"topic": "<topic actually covered>", "score": '
        '<integer 0-100>, "note": "<1 short sentence>"}], '
        '"question_notes": [{"question": "<the interviewer\'s question, as '
        'asked>", "topic": "<topic>", "candidate_answer_summary": "<short '
        'paraphrase of what they said>", "assessment": "<1-2 sentences>", '
        '"better_sample_answer": "<a strong model answer to that specific '
        'question>"}], '
        '"next_practice_plan": [{"topic": "<topic>", "track": "sql"|"case", '
        '"reason": "<1 short sentence>"}], '
        '"trend_note": "<1-2 gentle sentences, or null>", '
        '"rough_level": "beginner"|"intermediate"|"advanced"}'
    )


def interview_feedback(*, user_id: str, conversation: list[dict], target_role: str = "Data Analyst", topic_history: dict | None = None) -> dict:
    """
    Generates the end-of-interview feedback report from the full transcript.
    Returns {"report": {...parsed fields...}, "usage": {...}}.
    """
    transcript = "\n".join(f"{t['role'].upper()}: {t['content']}" for t in conversation)
    messages = [
        {"role": "system", "content": _feedback_system_prompt(target_role, topic_history)},
        {"role": "user", "content": f"Interview transcript:\n\n{transcript}"},
    ]
    result = _call_chat_with_retry(user_id=user_id, problem_id="mock-interview-feedback", messages=messages, max_tokens=2500, json_mode=True)
    try:
        report = _parse_json_reply(result["reply"])
        # Defensive validation, same "don't fully trust a free-form JSON
        # reply for fields other code depends on structurally" precedent as
        # interview_turn's topic normalization -- record_topic_history()
        # needs every topic_scores entry to be a real topic with a real
        # score, not whatever the model happened to return.
        #
        # covered_topics (topic ever appears on an assistant turn) isn't
        # strict enough on its own: QA found that ending an interview right
        # after a question was ASKED but before the candidate answered it
        # still got a full topic_score/question_note back for it, complete
        # with a fabricated candidate_answer_summary ("The candidate wrote
        # a basic SELECT statement...") for a reply that was never given --
        # plus, separately, entire fabricated Q&A exchanges for topics never
        # asked about at all. answered_topics is the stricter check: a
        # topic only counts once a real USER turn actually follows an
        # assistant turn tagged with it.
        all_topics = role_topics.topics_for_role(target_role)
        answered_topics = set()
        _last_asked_topic = None
        for turn in conversation:
            if turn.get("role") == "assistant" and turn.get("topic"):
                _last_asked_topic = turn["topic"]
            elif turn.get("role") == "user" and _last_asked_topic:
                answered_topics.add(_last_asked_topic)
        report["topic_scores"] = [
            t for t in report.get("topic_scores", [])
            if isinstance(t, dict) and t.get("topic") in all_topics
            and t.get("topic") in answered_topics
            and isinstance(t.get("score"), (int, float))
        ]
        report["question_notes"] = [
            q for q in report.get("question_notes", [])
            if isinstance(q, dict) and q.get("topic") in answered_topics
        ]
        report.setdefault("next_practice_plan", [])
        report["trend_note"] = _validate_trend_note(report.get("trend_note"), report["topic_scores"], topic_history)
        # The prompt-level instruction above (keep short transcripts brief
        # and general) is a mitigation, not a guarantee -- confirmed live
        # that the model still returned a confident score (65/100) with a
        # full strengths/weaknesses list for an interview ended within
        # seconds, before the candidate answered anything at all. Unlike
        # topic_scores/question_notes, score/strengths/weaknesses/
        # overall_summary have no per-item topic to filter against, so
        # this is an all-or-nothing deterministic override: if literally
        # nothing was ever answered, force the report to say exactly
        # that instead of trusting the model not to fabricate a
        # narrative around silence.
        if not answered_topics:
            report["score"] = None
            report["strengths"] = []
            report["weaknesses"] = []
            report["overall_summary"] = (
                "The interview ended before any question was answered, so "
                "there's nothing yet to assess -- start a new session "
                "whenever you're ready to give it a real go."
            )
            report["rough_level"] = None
    except (json.JSONDecodeError, KeyError):
        report = {
            "overall_summary": result["reply"],
            "score": None,
            "strengths": [],
            "weaknesses": [],
            "topics_to_study": [],
            "topic_scores": [],
            "question_notes": [],
            "next_practice_plan": [],
            "trend_note": None,
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
    "Worked example of the domain/topic distinction: a healthcare-domain "
    "problem about patients with no recent appointments still has "
    "\"topic\": \"Advanced Searching\" (a real value from the given "
    "topics list) -- NEVER \"topic\": \"Healthcare\" or \"topic\": "
    "\"healthcare\", which are domain names, not topics, and will be "
    "rejected outright.\n\n"
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
    "input-to-output logic only.\n"
    "- If the function takes a file path / filename as a parameter (a "
    "'Files and I/O' problem), the description MUST show the exact "
    "content that file will contain as a concrete example (e.g. the "
    "literal CSV rows, or the literal text) -- never just describe its "
    "structure abstractly ('a CSV file with columns id, name, age'). "
    "Every other problem on this platform passes its input directly as "
    "a visible function argument; a file is the one case where the "
    "input is invisible unless the description spells it out, so a "
    "student has no way to know what their function will actually "
    "receive otherwise. The file's real content is whatever test_code "
    "creates -- the description's example must match that exactly.\n\n"
    "Worked example of the domain/topic distinction: a healthcare-domain "
    "problem about deduplicating patient records still has \"topic\": "
    "\"Data Structures and Algorithms\" (a real value from the given "
    "topics list) -- NEVER \"topic\": \"Healthcare\" or \"topic\": "
    "\"healthcare\", which are domain names, not topics, and will be "
    "rejected outright.\n\n"
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
    "Worked example of the domain/topic distinction: a healthcare-domain "
    "problem about a confidence interval for readmission rates still has "
    "\"topic\": \"Confidence Intervals & Estimation\" (a real value from "
    "the given topics list) -- NEVER \"topic\": \"Healthcare\" or "
    "\"topic\": \"healthcare\", which are domain names, not topics, and "
    "will be rejected outright.\n\n"
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
    "Use `pandas` for DataFrame/Series-shaped problems (topic 'Pandas') "
    "and `numpy` for array-shaped problems (topic 'NumPy') -- both are "
    "pre-installed in the grading sandbox. Avoid "
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
    "Worked example of the domain/topic distinction: a healthcare-domain "
    "problem about grouping patient visits still has \"topic\": "
    "\"Pandas\" (a real value from the given topics list) -- NEVER "
    "\"topic\": \"Healthcare\" or \"topic\": \"healthcare\", which are "
    "domain names, not topics, and will be rejected outright.\n\n"
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
    is_default_prompt = system_prompt is None
    system_prompt = system_prompt or PYTHON_PROBLEM_BATCH_SYSTEM_PROMPT
    # pandas/numpy (and stats problems reaching for them) have much more
    # fragile self-consistency than general Python -- DataFrame/array
    # equality is sensitive to float rounding, dtype (int32 vs int64),
    # and index alignment in ways a plain Python assert never has to
    # worry about, so the same 0.75 that works for general Python
    # produced a much higher canonical_solution-fails-its-own-test rate
    # here. Lower temperature for anything other than the default prompt.
    batch_temperature = 0.75 if is_default_prompt else 0.5
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
            timeout=120, temperature=batch_temperature,
        )
        try:
            parsed = _parse_json_reply(result["reply"])
            return {"problems": parsed.get("problems", []), "usage": result["usage"]}
        except (json.JSONDecodeError, KeyError) as e:
            last_parse_error = e
    raise RuntimeError(f"Model didn't return valid JSON after 3 attempts ({last_parse_error}).")


# ---------------------------------------------------------------------------
# Business Case track -- open-ended analytical-reasoning questions with no
# single verifiable right answer (metric design, root-cause investigation,
# pipeline trade-offs), graded by a rubric-based AI judge instead of
# execution. Two lenses (DA/DE) riding the same track='case' storage and
# grading, exactly like stats/pandas/numpy are topic lenses within
# track='python' rather than separate tracks -- see case_topics.py.
# ---------------------------------------------------------------------------

_CASE_BATCH_SHARED_RULES = (
    "There is no single verifiable right answer here -- these are graded "
    "by a rubric-based AI judge scoring a candidate's written response, "
    "not by execution. Each problem needs:\n"
    "- title\n- difficulty (easy/medium/hard)\n"
    "- topic (exactly one from the allowed list, exact spelling)\n"
    "- tags (list of strings)\n"
    "- case_prompt: the full scenario, written the way a real interviewer "
    "would actually present it in the moment -- concrete numbers and "
    "context, never abstract or generic. Answerable in a focused written "
    "response (a few paragraphs), not a full slide deck or a multi-week "
    "project plan.\n"
    "- case_context: any supporting data the candidate needs (a small "
    "table of numbers, a metric definition, a snippet of dashboard "
    "context) as a plain string, or null if case_prompt is fully "
    "self-contained on its own.\n"
    "- rubric_points: a list of 4-7 CONCRETE things a strong answer "
    "should cover -- specific enough to actually check an answer against "
    "(e.g. \"considers whether the metric drop could be a measurement/"
    "instrumentation artifact before assuming a real behavior change\"), "
    "never vague platitudes like \"shows good communication\" or "
    "\"demonstrates structured thinking.\"\n"
    "- sample_strong_answer: an answer that would score well against your "
    "own rubric_points above -- internal-only, never shown to students, "
    "used solely to self-validate this draft before it goes live.\n\n"
    "Vary the business domain across the batch (e-commerce, fintech, "
    "marketplace/gig platforms, social/media, an AI or GenAI product, "
    "B2B SaaS, etc.) rather than reusing the same company framing "
    "repeatedly, and don't draft the same underlying scenario twice under "
    "a different label.\n\n"
    "Respond with ONLY a JSON object, no other text: "
    '{"problems": [{"title": "...", "difficulty": "...", "topic": "...", '
    '"tags": [...], "case_prompt": "...", "case_context": "..."|null, '
    '"rubric_points": ["...", ...], "sample_strong_answer": "..."}, ...]}'
)

CASE_DA_BATCH_SYSTEM_PROMPT = (
    "You write realistic business-case interview questions for a "
    "candidate preparing for Data Analyst interviews at companies like "
    "Uber, Meta, Google, and peers -- the round that decides most DA "
    "offers: metric design, diagnosing a metric that moved, designing an "
    "A/B test, reasoning about growth/retention, or a product/prioritization "
    "trade-off. This is the kind of open-ended reasoning a SQL query or a "
    "coding exercise can't test at all.\n\n" + _CASE_BATCH_SHARED_RULES
)

CASE_DE_BATCH_SYSTEM_PROMPT = (
    "You write realistic business-case interview questions for a "
    "candidate preparing for Data Engineer interviews -- system-design-"
    "style trade-off discussions: how to architect a pipeline, batch vs. "
    "streaming decisions, schema/modeling trade-offs, data quality/"
    "validation strategy, or scaling a system under real constraints "
    "(cost, latency, team size). Test genuine engineering judgment and "
    "trade-off reasoning, not a trivia question with one memorized "
    "correct term.\n\n" + _CASE_BATCH_SHARED_RULES
)


def generate_case_batch(*, user_id: str, topics: list[str], count: int, existing_titles: list[str] | None = None, system_prompt: str | None = None) -> dict:
    """
    Business Case equivalent of generate_python_problem_batch() -- same
    shape, same JSON-parse-with-retry machinery. `system_prompt` defaults
    to the DA-flavored prompt; callers pass CASE_DE_BATCH_SYSTEM_PROMPT
    for a DE-flavored batch, the same swap-in pattern main.py already
    uses for stats/pandas/numpy Python batches.
    """
    system_prompt = system_prompt or CASE_DA_BATCH_SYSTEM_PROMPT
    user_prompt = (
        f"Draft {count} new business-case practice problems spread across "
        f"these topics (cover each at least once if count allows): {', '.join(topics)}.\n"
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
    last_parse_error = None
    for attempt in range(3):
        result = _call_chat_with_retry(
            user_id=user_id, problem_id="admin-case-problem-batch", messages=messages,
            max_tokens=min(6000, max(1500, count * 550)), json_mode=False,
            timeout=120, temperature=0.8,
        )
        try:
            parsed = _parse_json_reply(result["reply"])
            return {"problems": parsed.get("problems", []), "usage": result["usage"]}
        except (json.JSONDecodeError, KeyError) as e:
            last_parse_error = e
    raise RuntimeError(f"Model didn't return valid JSON after 3 attempts ({last_parse_error}).")


def validate_case_draft_quality(*, case_prompt: str, case_context: str | None, rubric_points: list[str], sample_strong_answer: str) -> tuple[bool, str]:
    """
    Self-consistency gate for a newly-drafted case problem, run once at
    insert time (see problems.insert_pending_draft): does
    sample_strong_answer actually hit its own rubric_points well? Uses a
    plain hit-count rather than the full case_feedback machinery below --
    no follow-up-question logic is needed for an internal quality check.
    Returns (passed, reason). Unlike other validators' "benefit of the
    doubt on infra hiccups" stance, lets a parse/call failure propagate to
    the caller as a rejection -- a rubric that can't even be confirmed
    against its own sample answer shouldn't ship regardless of cause.
    """
    rubric_block = "\n".join(f"- {p}" for p in rubric_points)
    context_block = f"\nSupporting context: {case_context}" if case_context else ""
    user_prompt = (
        f"Case prompt: {case_prompt}{context_block}\n\n"
        f"Rubric points:\n{rubric_block}\n\n"
        f"Sample answer to check:\n{sample_strong_answer}"
    )
    system_prompt = (
        "You are checking whether a SAMPLE answer clearly hits the rubric "
        "points listed, as a quality gate before this question goes live "
        "-- you are NOT grading a real candidate. Respond with ONLY a "
        'JSON object: {"points_hit": <int, how many rubric points this '
        'answer clearly addresses>, "total_points": <int, total rubric '
        'points given>, "reason": "<one sentence>"}'
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    result = _call_chat_with_retry(
        user_id="admin", problem_id="case-draft-validation", messages=messages,
        max_tokens=300, json_mode=True,
    )
    parsed = _parse_json_reply(result["reply"])
    total = parsed.get("total_points") or len(rubric_points)
    hit = parsed.get("points_hit", 0)
    passed = total > 0 and (hit / total) >= 0.7
    return passed, parsed.get("reason", f"{hit}/{total} rubric points hit")


CASE_FEEDBACK_SYSTEM_PROMPT = (
    "You are an experienced interviewer scoring a candidate's written "
    "answer to a business-case question, against a specific rubric. "
    "Score substance and analytical rigor, never length or polish -- a "
    "short, sharp answer that hits the real analytical points beats a "
    "long one that circles around them without landing anywhere.\n\n"
    "rubric_points_hit and rubric_points_missed together MUST exactly "
    "partition the full rubric you're given -- every point appears "
    "verbatim in exactly one of the two lists, never both, never "
    "neither.\n\n"
    "If -- and only if -- the answer is genuinely borderline (covers some "
    "but not all of the rubric, in a way where ONE targeted follow-up "
    "question could reasonably let the candidate close the gap, the way "
    "a real interviewer probes rather than immediately ending the round) "
    "set needs_follow_up to true and write that ONE question in "
    "follow_up_question. Do NOT ask a follow-up for an answer that's "
    "already clearly strong (nothing meaningful left to probe) or "
    "clearly weak (a follow-up wouldn't salvage it) -- reserve it "
    "strictly for the genuinely in-between case. When you are told this "
    "is final scoring (a follow-up was already asked and answered), you "
    "MUST set needs_follow_up to false regardless of how the answer "
    "reads -- there is no second follow-up round.\n\n"
    "Respond with ONLY a JSON object, no other text: "
    '{"needs_follow_up": bool, "follow_up_question": "..."|null, '
    '"score": <integer 0-100>, "overall_summary": "<2-3 sentences>", '
    '"rubric_points_hit": ["...", ...], "rubric_points_missed": ["...", ...], '
    '"strengths": ["...", ...], "weaknesses": ["...", ...]}'
)


def case_feedback(
    *,
    user_id: str,
    problem: dict,
    answer: str,
    follow_up_question: str | None = None,
    follow_up_answer: str | None = None,
) -> dict:
    """
    Two-pass rubric grading for one Business Case answer.

    First call (follow_up_question/follow_up_answer both None): scores
    `answer` against problem['rubric_points']. If the judge decides the
    answer is genuinely borderline, returns
    {"status": "follow_up_needed", "follow_up_question": str, "usage": {...}}
    with no score yet -- the caller shows this question to the student,
    collects one more free-text response, and calls again.

    Second call (both follow_up_question and follow_up_answer provided --
    the caller echoes back the exact question it received from the first
    call, stateless rather than needing server-side session storage):
    produces a FINAL score informed by both the original answer and the
    follow-up response. The prompt explicitly tells the model this is
    final scoring so it can never loop into asking a second follow-up.

    Returns either the follow-up shape above, or:
    {"status": "final", "score": int, "overall_summary": str,
     "rubric_points_hit": [...], "rubric_points_missed": [...],
     "strengths": [...], "weaknesses": [...], "usage": {...}}
    """
    is_final_pass = follow_up_question is not None and follow_up_answer is not None

    parts = [f"Case prompt: {problem['case_prompt']}"]
    if problem.get("case_context"):
        parts.append(f"Supporting context: {problem['case_context']}")
    rubric_block = "\n".join(f"- {p}" for p in problem["rubric_points"])
    parts.append(f"Rubric points to check for:\n{rubric_block}")
    parts.append(f"Candidate's answer:\n{answer}")
    if is_final_pass:
        parts.append(f"You previously asked this follow-up question: {follow_up_question}")
        parts.append(f"Candidate's follow-up response: {follow_up_answer}")
        parts.append(
            "This is now FINAL scoring -- a follow-up has already been asked and "
            "answered. Do not ask another follow-up under any circumstances; "
            "incorporate the follow-up response into one final judgment."
        )

    messages = [
        {"role": "system", "content": CASE_FEEDBACK_SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
    result = _call_chat_with_retry(
        user_id=user_id, problem_id=f"case-{problem['id']}", messages=messages,
        max_tokens=1200, json_mode=True,
    )
    try:
        parsed = _parse_json_reply(result["reply"])
    except (json.JSONDecodeError, KeyError):
        parsed = {
            "needs_follow_up": False, "score": None,
            "overall_summary": result["reply"],
            "rubric_points_hit": [], "rubric_points_missed": list(problem["rubric_points"]),
            "strengths": [], "weaknesses": [],
        }

    if not is_final_pass and parsed.get("needs_follow_up") and parsed.get("follow_up_question"):
        return {
            "status": "follow_up_needed",
            "follow_up_question": parsed["follow_up_question"],
            "usage": result["usage"],
        }

    return {
        "status": "final",
        "score": parsed.get("score"),
        "overall_summary": parsed.get("overall_summary", ""),
        "rubric_points_hit": parsed.get("rubric_points_hit", []),
        "rubric_points_missed": parsed.get("rubric_points_missed", []),
        "strengths": parsed.get("strengths", []),
        "weaknesses": parsed.get("weaknesses", []),
        "usage": result["usage"],
    }
