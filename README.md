# Phoenix Prep

"LeetCode for interviews" for Indian IT professionals — SQL today, with
Python, DSA, and other tracks planned. Every SQL submission is graded by
actually running the query in DuckDB — never by an LLM guessing whether
it's right. The LLM only gets called on demand, when a (Pro) student opens
"Ask Phoenix" for contextual help on a problem, so grading itself costs ₹0
in inference.

## Stack (and why)

Node.js isn't installed on this machine, so the MVP is built as:

- **Backend**: Python + FastAPI + DuckDB (`backend/`)
- **Frontend**: plain HTML/CSS/JS, no build step, Monaco editor loaded from
  a CDN (`frontend/`)

This is a deliberate MVP tradeoff to get something running today with zero
npm/build friction. If/when you want React + a component library + a real
build pipeline (recommended before hiring frontend help or scaling the UI),
`brew install node` and swap `frontend/` for a Vite+React app — the backend
API is framework-agnostic and doesn't need to change.

**DuckDB vs SQLite**: DuckDB was used as specified — it was already
installed (`pip show duckdb` found 0.8.1) and its in-memory connections are
cheap enough to spin up fresh per submission, which is what gives you real
sandbox isolation for free. If you ever hit friction with DuckDB in a
deploy environment, SQLite is a near-drop-in swap in `sandbox.py` (same
"fresh in-memory connection per request" pattern), but you'd lose DuckDB's
better window-function/analytics ergonomics, which matter a lot for a SQL
*interview prep* product specifically.

## Deploying to Render

`render.yaml` at the repo root is a Render **Blueprint** defining two free-
tier services:

- `sql-practice-backend` — the FastAPI app (`backend/`)
- `sql-practice-frontend` — the static frontend (`frontend/`)

Steps:
1. Push this repo to GitHub (see below).
2. In the Render dashboard: **New +** → **Blueprint** → connect this repo.
   Render reads `render.yaml` and proposes both services — click **Apply**.
3. On the backend service, set the `LLM_API_KEY` env var (left blank in
   `render.yaml` on purpose — never commit real keys). `LLM_API_BASE` /
   `LLM_MODEL` default to Groq's Llama 3.1 8B; change them in the Render
   dashboard if you want a different provider.
4. Once the backend deploys, copy its URL (e.g.
   `https://sql-practice-backend.onrender.com`) into
   `frontend/config.js` (`window.API_BASE = "..."`), commit, and push —
   Render auto-redeploys the static site on every push to the connected
   branch.

**Free-tier caveat**: Render's free web services spin down after 15 min of
inactivity and take ~30-50s to cold-start on the next request — the first
submission after idle time will be slow. Fine for demoing to friends/early
users; upgrade the backend to a paid instance before real traffic.

**CORS**: the backend currently allows all origins (`allow_origins=["*"]`
in `main.py`) so the frontend works from any Render URL without extra
config. Once you have a fixed frontend domain, tighten this to that exact
origin.

## Running it locally

Terminal 1 — backend:
```bash
cd backend
pip3 install --prefer-binary -r requirements.txt
uvicorn main:app --reload --port 8000
```

Terminal 2 — frontend (any static file server works):
```bash
cd frontend
python3 -m http.server 8080
```

Then open http://127.0.0.1:8080 in a browser.

## Configuring Ask Phoenix (LLM)

All LLM logic is isolated in `backend/llm.py` — `ask_phoenix()` is the only
function that talks to a model (plus `interview_turn()`/`interview_feedback()`
for mock interviews, and `generate_problem_batch()` for admin content
generation). Swap providers by changing env vars only:

```bash
# Example: Groq (OpenAI-compatible endpoint, fast + cheap Llama models)
export LLM_API_BASE="https://api.groq.com/openai/v1"
export LLM_API_KEY="gsk_..."
export LLM_MODEL="llama-3.1-8b-instant"

# Example: Gemini Flash via its OpenAI-compatible layer
export LLM_API_BASE="https://generativelanguage.googleapis.com/v1beta/openai"
export LLM_API_KEY="AIza..."
export LLM_MODEL="gemini-1.5-flash"
```

Grading itself never depends on the LLM — Ask Phoenix is a separate,
on-demand, Pro-only feature. Without these env vars set, `/api/ask-phoenix`
just returns a 502 with a clear error instead of an answer.

Every LLM call appends one JSON line to `backend/usage_log.jsonl`:
`user_id, problem_id, model, prompt_tokens, completion_tokens,
total_tokens, estimated_cost_usd, logged_at`. Query it directly with
DuckDB once you have real volume:
```sql
SELECT user_id, SUM(estimated_cost_usd) AS cost
FROM read_json_auto('backend/usage_log.jsonl')
GROUP BY user_id ORDER BY cost DESC;
```
Update `COST_PER_1M_TOKENS` in `llm.py` to match whatever provider/model
you actually configure — the numbers in there right now are placeholders.

## How grading works (backend/sandbox.py)

1. Student query is validated: must be a single `SELECT`/`WITH` statement,
   no semicolons except a trailing one, and a keyword blocklist rejects
   `INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/ATTACH/COPY/PRAGMA/...`.
2. A **fresh in-memory DuckDB connection** is created, the problem's schema
   + seed data loaded into it, and the query run in a worker thread with a
   5-second wall-clock timeout. Results are capped at 1000 rows.
3. Output is compared against a pre-computed expected result (the
   canonical query is run once at server startup, cached). Problems that
   don't require a specific row order are compared as sorted multisets, so
   students aren't failed for missing an `ORDER BY` they weren't asked for.
4. Either way (pass or fail) → return the result immediately. **No LLM
   call** -- grading is pure code. A Pro student can separately open "Ask
   Phoenix" for contextual help on the problem (`llm.ask_phoenix()`), but
   that's a distinct, on-demand action, not part of grading.

This is enforced only at the process level (no OS sandbox/container), which
is fine for a local MVP but **not** sufficient hardening for a public
multi-tenant deploy — before opening this to the internet, run the DuckDB
execution inside a locked-down subprocess/container with no filesystem or
network access, since the keyword blocklist alone won't stop every
DuckDB extension/function surface (e.g. `read_csv`, `httpfs`).

## Tiering and auth

- Anonymous `X-User-Id` (a random UUID the frontend generates and stores in
  `localStorage`) is the fallback identity for practice mode -- signing in
  isn't required just to solve problems.
- Signing in (Clerk, `frontend/auth.js` + `backend/auth.py`) verifies a
  session JWT server-side and swaps in the real Clerk user id wherever a
  request carries a valid `Authorization: Bearer` token. Tier + daily
  counters persist in Postgres (`backend/users.py`), not in memory, so they
  survive a Render restart.
- Free tier: `FREE_DAILY_SUBMISSIONS` submissions/day (`main.py`), plus only
  the curated free-tier subset of the problem bank
  (`problems.FREE_PROBLEM_IDS`). No AI help.
- Paid tier (₹199/mo, real Razorpay checkout -- `backend/payments.py`,
  `POST /api/payments/create-order` + `POST /api/payments/verify`):
  unlimited submissions, the full problem bank, mock interviews, and
  unlimited **Ask Phoenix** (`POST /api/ask-phoenix`) -- open-ended
  contextual help on any problem, any time, not gated by a daily count.

## Mock voice interview (backend/interview.py, frontend/interview.js)

A 45-minute spoken SQL interview, Pro-tier only. Generic mode covers a fixed
topic list (`GENERIC_TOPICS` in `interview.py`); personalized mode takes an
uploaded PDF/DOCX resume (`backend/resume_parser.py`) and grounds questions
in it. After each answer the LLM decides one of `follow_up` (gap in the
answer), `probe` (go deeper, same topic), or `switch_topic` — per
`_interview_system_prompt()` in `llm.py`. At the end (time up or manually
ended) `interview_feedback()` generates a structured report: overall
summary, strengths, weaknesses, topics to study, rough level.

**Voice is entirely client-side**: speech-to-text and text-to-speech both
run via the browser's Web Speech API (`SpeechRecognition` /
`speechSynthesis`) — no audio ever reaches the backend, which only ever
sees transcribed text in and spoken question text out. That's free but
robotic-sounding and Chrome-only for STT (Firefox has no
`SpeechRecognition`; the UI falls back to a typed-answer textbox when
unsupported). To upgrade later to Groq Whisper for STT, only the
`startListening()`/`stopListening()` functions in `interview.js` need to
change — the orchestration logic in `interview.py`/`llm.py` is unaffected
since it already just deals in text.

**Session state is persisted to Postgres** (`backend/db.py`,
`interview_sessions` table) on every turn, so an in-progress interview
survives a browser crash/reload, a transient LLM failure, or the backend
itself restarting. `GET /api/interview/session/{id}` lets the frontend reconnect
to an existing session and rehydrate the full transcript, current question,
table context, and remaining time; the frontend tracks the active
`session_id` in `localStorage` and offers a "Resume interview?" prompt
before falling through to the normal setup screen. Once an interview ends
(`interview.mark_ended`), only the feedback report matters — there's no
further persistence need after that point.

Requires the `sql-practice-db` Postgres instance declared in `render.yaml`
(`databases:` section, wired to the backend via `DATABASE_URL`
`fromDatabase`). Render's free Postgres tier auto-expires 30 days after
creation unless upgraded — a real constraint to revisit, not a bug.

**Cost note**: unlike the practice platform (LLM only called on wrong
answers), a full interview makes many LLM calls over 45 minutes — hence
paid-tier-only gating (`_require_paid()` in `main.py`, 402 for free users).

## Problem bank

10 problems in `backend/problems.py`, each fully self-contained (own
schema + seed data + canonical query), spanning:
`select/where` → `distinct` → `joins` (inner + left) → `aggregation
+ group by + having` → `correlated subqueries` → `relational division
(NOT EXISTS-style)` → `window functions (RANK, running total)`.
Seed data intentionally includes NULLs, a rehire duplicate, and a
duplicate department row, so problems feel like real messy analyst data
rather than toy examples.

Problems live in Postgres, not in code — `PROBLEMS` in `problems.py` only
seeds an empty table on first deploy; editing it after that has no effect.
Add new problems either by hand (a direct SQL insert into the `problems`
table, `status='live'`) or via the admin batch-generation flow
(`POST /api/admin/problems/generate-batch`, human-reviewed via `/approve`
before going live — see `insert_pending_draft()` in `problems.py`).

## What's not in this MVP (known gaps)

- No production sandboxing for DuckDB grading (see grading section above)
  — process-level isolation only, fine for the current scale, not
  sufficient hardening for a large-scale public deploy.
- Anonymous progress (made before signing in) only merges into a real
  account automatically the first time that browser signs in
  (`POST /api/merge-progress`) -- there's no manual merge path if that
  auto-merge is ever missed.
- Interview STT/TTS quality is whatever the browser's Web Speech API gives
  you — no fallback to a paid provider yet (see mock-interview section
  above).
- Problem bank is at 65 problems, short of the ~150 target discussed for
  full topic/difficulty coverage.
