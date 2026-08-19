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
    "the error."
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
    Calls the configured OpenAI-compatible chat completions endpoint to
    explain a wrong (or erroring) submission. Returns:
        {"explanation": str, "usage": {"prompt_tokens", "completion_tokens", "total_tokens", "estimated_cost_usd"}}

    Raises RuntimeError if LLM_API_KEY isn't configured or the call fails --
    caller should catch this and degrade gracefully (e.g. show a generic
    "couldn't get an AI explanation right now" message) rather than 500ing
    the whole submission.
    """
    if not LLM_API_KEY:
        raise RuntimeError(
            "LLM_API_KEY is not set. Configure LLM_API_BASE / LLM_API_KEY / "
            "LLM_MODEL env vars to enable AI explanations."
        )

    user_prompt = _build_user_prompt(problem, student_query, expected_preview, actual_preview, error)

    resp = requests.post(
        f"{LLM_API_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 300,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    explanation = data["choices"][0]["message"]["content"].strip()
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
        "problem_id": problem["id"],
        "model": LLM_MODEL,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": round(estimated_cost, 6),
    })

    return {
        "explanation": explanation,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
        },
    }
