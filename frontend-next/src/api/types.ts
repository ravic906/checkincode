export type Difficulty = "Easy" | "Medium" | "Hard";
export type Track = "sql" | "python" | "case" | "mock";

export interface TrackInfo {
  id: Track;
  name: string;
  blurb: string;
  meta: string;
  pro: boolean;
}

export interface ProblemSummary {
  id: string;
  title: string;
  difficulty: Difficulty;
  topic: string;
  tags: string[];
  is_free: boolean;
  track: Exclude<Track, "mock">;
  solved: boolean;
  locked: boolean;
}

export interface SchemaTable {
  name: string;
  cols: string; // dot-separated column list, matches the mock's display format
}

interface ProblemBase {
  id: string;
  title: string;
  difficulty: Difficulty;
  topic: string;
  tags: string[];
}

export interface SqlProblemDetail extends ProblemBase {
  track: "sql";
  description: string;
  schema_sql: string;
  sample_tables: Record<string, { columns: string[]; rows: unknown[][] }>;
  examples: unknown;
}

export interface PythonProblemDetail extends ProblemBase {
  track: "python";
  description: string;
  starter_code: string;
  examples: unknown[];
}

export interface CaseProblemDetail extends ProblemBase {
  track: "case";
  case_prompt: string;
  case_context: string | null;
}

export type ProblemDetail = SqlProblemDetail | PythonProblemDetail | CaseProblemDetail;

export interface ResultPreview {
  columns: string[];
  rows: unknown[][];
}

export type CaseSubmitResult =
  | { status: "follow_up_needed"; follow_up_question: string }
  | {
      status: "final";
      score: number;
      overall_summary: string;
      rubric_points_hit: string[];
      rubric_points_missed: string[];
      strengths: string[];
      weaknesses: string[];
    };

export interface SubmitResult {
  correct: boolean;
  error: string | null;
  // SQL track
  actual_preview?: ResultPreview | null;
  expected_preview?: ResultPreview | null;
  // Python track
  output?: string | null;
}

export interface AskPhoenixMessage {
  role: "user" | "assistant";
  content: string;
}

export interface AskPhoenixResponse {
  answer: string;
}

export interface UsageInfo {
  user_id: string;
  tier: "free" | "paid";
  submissions_today: number;
  free_daily_submissions: number;
  interview_trial_used: boolean;
  interviews_this_month: number;
  max_interviews_per_month: number;
  is_admin: boolean;
}

export interface InterviewStartResponse {
  session_id: string;
  question: string;
  topic: string;
  action: string;
  remaining_seconds: number;
  duration_seconds: number;
}

export interface InterviewAnswerResponse {
  time_up: boolean;
  session_id: string;
  question?: string;
  topic?: string;
  action?: string;
  remaining_seconds: number;
}

export interface InterviewFeedbackReport {
  overall_summary: string;
  score: number | null;
  strengths: string[];
  weaknesses: string[];
  topics_to_study: string[];
  rough_level: string;
}

export interface InterviewEndResponse {
  feedback: InterviewFeedbackReport;
  conversation: AskPhoenixMessage[];
}

export interface ProgressTopic {
  name: string;
  solved: number;
  total: number;
}

export interface ProgressActivityDay {
  date: string;
  count: number;
}

export interface ProgressRecent {
  title: string;
  correct: boolean;
  submitted_at: string | null;
}

export interface ProgressResponse {
  problems_verified: number;
  first_run_pass_rate: number;
  mock_interviews: number;
  topics: ProgressTopic[];
  activity: ProgressActivityDay[];
  recent: ProgressRecent[];
}
