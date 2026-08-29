import type {
  AskPhoenixMessage,
  AskPhoenixResponse,
  CaseSubmitResult,
  InterviewAnswerResponse,
  InterviewEndResponse,
  InterviewStartResponse,
  ProblemDetail,
  ProblemSummary,
  ProgressResponse,
  SubmitResult,
  UsageInfo,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000";

// Same localStorage key the previous vanilla-JS frontend used, so an
// anonymous visitor's existing progress (solved problems, submission
// history) carries over to this rebuild rather than starting fresh.
const USER_ID_KEY = "sqlpractice_user_id";

function getAnonUserId(): string {
  let id = localStorage.getItem(USER_ID_KEY);
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem(USER_ID_KEY, id);
  }
  return id;
}

export const ANON_USER_ID = getAnonUserId();

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

/** Supplied by the app root once Clerk has loaded (see ClerkTokenBridge). */
let getAuthToken: () => Promise<string | null> = async () => null;
export function setAuthTokenGetter(fn: () => Promise<string | null>) {
  getAuthToken = fn;
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-User-Id": ANON_USER_ID,
    ...((options.headers as Record<string, string>) || {}),
  };
  const token = await getAuthToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}) as { detail?: string });
    throw new ApiError(body.detail || `Request failed (${res.status})`, res.status);
  }
  return res.json();
}

export interface FetchProblemsParams {
  track?: string;
  difficulty?: string;
  topic?: string;
  tag?: string;
}

export function fetchProblems(params: FetchProblemsParams = {}): Promise<{ problems: ProblemSummary[] }> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v) qs.set(k, v);
  }
  const suffix = qs.toString() ? `?${qs}` : "";
  return request(`/api/problems${suffix}`);
}

export function fetchProblem(id: string): Promise<ProblemDetail> {
  return request(`/api/problems/${encodeURIComponent(id)}`);
}

export function runSubmission(args: { problemId: string; code: string }): Promise<SubmitResult> {
  return request("/api/submit", {
    method: "POST",
    body: JSON.stringify({ problem_id: args.problemId, query: args.code }),
  });
}

export function askPhoenix(args: {
  problemId: string;
  code: string;
  question: string;
  conversation: AskPhoenixMessage[];
}): Promise<AskPhoenixResponse> {
  return request("/api/ask-phoenix", {
    method: "POST",
    body: JSON.stringify({
      problem_id: args.problemId,
      current_query: args.code,
      question: args.question,
      conversation: args.conversation,
    }),
  });
}

export function submitCase(args: {
  problemId: string;
  answer: string;
  followUpQuestion?: string;
  followUpAnswer?: string;
}): Promise<CaseSubmitResult> {
  return request("/api/case/submit", {
    method: "POST",
    body: JSON.stringify({
      problem_id: args.problemId,
      answer: args.answer,
      follow_up_question: args.followUpQuestion,
      follow_up_answer: args.followUpAnswer,
    }),
  });
}

export function fetchUsage(): Promise<UsageInfo> {
  return request("/api/usage");
}

export function fetchProgress(): Promise<ProgressResponse> {
  return request("/api/progress");
}

export function startMockSession(args: {
  mode: "personalized" | "generic";
  resumeText?: string;
  persona?: "friendly" | "neutral" | "strict";
}): Promise<InterviewStartResponse> {
  return request("/api/interview/start", {
    method: "POST",
    body: JSON.stringify({
      mode: args.mode,
      resume_text: args.resumeText,
      persona: args.persona || "neutral",
    }),
  });
}

export function submitMockTurn(args: { sessionId: string; answerText: string }): Promise<InterviewAnswerResponse> {
  return request("/api/interview/answer", {
    method: "POST",
    body: JSON.stringify({ session_id: args.sessionId, answer_text: args.answerText }),
  });
}

export function endMockSession(sessionId: string): Promise<InterviewEndResponse> {
  return request("/api/interview/end", {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId }),
  });
}

async function requestMultipart<T>(path: string, form: FormData): Promise<T> {
  const headers: Record<string, string> = { "X-User-Id": ANON_USER_ID };
  const token = await getAuthToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  // Deliberately no Content-Type header -- the browser sets
  // multipart/form-data with the correct boundary itself; setting it
  // manually here would omit the boundary and break parsing server-side.
  const res = await fetch(`${API_BASE}${path}`, { method: "POST", headers, body: form });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}) as { detail?: string });
    throw new ApiError(body.detail || `Request failed (${res.status})`, res.status);
  }
  return res.json();
}

export function parseResume(file: File): Promise<{ resume_text: string }> {
  const form = new FormData();
  form.append("file", file);
  return requestMultipart("/api/interview/parse-resume", form);
}

export function transcribeAudio(blob: Blob): Promise<{ text: string }> {
  const form = new FormData();
  form.append("file", blob, "answer.webm");
  return requestMultipart("/api/interview/stt", form);
}

export function mergeProgress(anonymousUserId: string): Promise<{ user_id: string; tier: string }> {
  return request("/api/merge-progress", {
    method: "POST",
    body: JSON.stringify({ anonymous_user_id: anonymousUserId }),
  });
}
