import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { fetchProblem, runSubmission, submitCase } from "../api/client";
import type { ProblemDetail } from "../api/types";
import { AskPhoenixDrawer } from "../components/AskPhoenixDrawer";
import { CodeEditor, type EditorLanguage } from "../components/CodeEditor";
import { DifficultyPill } from "../components/DifficultyPill";
import { ResultPanel, type ResultStatus } from "../components/ResultPanel";
import { SchemaCard } from "../components/SchemaCard";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { trackName } from "../lib/tracks";
import styles from "./ProblemWorkspace.module.css";

function starterFor(problem: ProblemDetail): string {
  // The backend's SQL validator requires the query to literally start with
  // SELECT/WITH (see sandbox.validate_student_sql) -- a leading comment
  // here would make every fresh SQL problem fail with "Only SELECT ...
  // statements are allowed" before the student even changes anything.
  if (problem.track === "sql") return "";
  if (problem.track === "python") return problem.starter_code;
  return "Framing\n1. \n\nHypotheses\n- \n\nWhat I would check first\n- \n";
}

function filenameFor(track: ProblemDetail["track"]): string {
  return track === "sql" ? "solution.sql" : track === "python" ? "solution.py" : "response.md";
}

function languageFor(track: ProblemDetail["track"]): EditorLanguage {
  return track === "sql" ? "sql" : track === "python" ? "python" : "markdown";
}

export function ProblemWorkspace() {
  const { track = "sql", problemId = "" } = useParams();
  const navigate = useNavigate();

  const [problem, setProblem] = useState<ProblemDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [resultStatus, setResultStatus] = useState<ResultStatus>("idle");
  const [resultProps, setResultProps] = useState<{
    detail?: string;
    timing?: string;
    columns?: string[];
    rows?: unknown[][];
    rawOutput?: string;
  }>({});
  const [phoenixOpen, setPhoenixOpen] = useState(false);
  const [followUpQuestion, setFollowUpQuestion] = useState<string | null>(null);
  const [followUpAnswer, setFollowUpAnswer] = useState("");

  const [code, setCode] = useLocalStorage(`phoenixprep:code:${problemId}`, "");
  const codeRef = useRef(code);
  codeRef.current = code;

  useEffect(() => {
    let cancelled = false;
    setProblem(null);
    setLoadError(null);
    setResultStatus("idle");
    setFollowUpQuestion(null);
    fetchProblem(problemId)
      .then((p) => {
        if (cancelled) return;
        setProblem(p);
        // Only seed starter code the first time this problem is opened on
        // this browser -- useLocalStorage already restores a previously
        // saved buffer for this key, so don't clobber it.
        setCode((prev) => (prev ? prev : starterFor(p)));
      })
      .catch((e) => {
        if (!cancelled) setLoadError(e.message || "Couldn't load this problem.");
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [problemId]);

  const run = useCallback(async () => {
    if (!problem || running) return;
    setRunning(true);
    setResultStatus("running");
    setFollowUpQuestion(null);
    const started = performance.now();
    try {
      if (problem.track === "case") {
        const res = await submitCase({ problemId: problem.id, answer: codeRef.current });
        if (res.status === "follow_up_needed") {
          setFollowUpQuestion(res.follow_up_question);
          setResultStatus("idle");
        } else {
          const pass = res.score >= 70;
          setResultStatus(pass ? "pass" : "fail");
          setResultProps({
            detail: res.overall_summary,
            timing: `${res.score} / 100`,
            columns: ["Rubric point", "Result"],
            rows: [
              ...res.rubric_points_hit.map((p) => [p, "Met"]),
              ...res.rubric_points_missed.map((p) => [p, "Missing"]),
            ],
          });
        }
      } else {
        const res = await runSubmission({ problemId: problem.id, code: codeRef.current });
        const ms = Math.round(performance.now() - started);
        if (problem.track === "python") {
          setResultStatus(res.correct ? "pass" : res.error ? "error" : "fail");
          setResultProps({
            detail: res.correct ? "All tests passed." : res.error || "Output didn't match.",
            timing: `${ms} ms`,
            rawOutput: res.output || undefined,
          });
        } else {
          setResultStatus(res.correct ? "pass" : res.error ? "error" : "fail");
          const preview = res.actual_preview;
          setResultProps({
            detail: res.correct
              ? "Output matches the expected result set, row for row."
              : res.error || "Query ran, but the result set differs from the expected output.",
            timing: preview ? `${ms} ms · ${preview.rows.length} rows` : `${ms} ms`,
            columns: preview?.columns,
            rows: preview?.rows,
          });
        }
      }
    } catch (e) {
      setResultStatus("error");
      setResultProps({ detail: e instanceof Error ? e.message : "Something went wrong." });
    } finally {
      setRunning(false);
    }
  }, [problem, running]);

  async function submitFollowUp() {
    if (!problem || !followUpQuestion) return;
    setRunning(true);
    setResultStatus("running");
    try {
      const res = await submitCase({
        problemId: problem.id,
        answer: codeRef.current,
        followUpQuestion,
        followUpAnswer,
      });
      if (res.status === "final") {
        const pass = res.score >= 70;
        setResultStatus(pass ? "pass" : "fail");
        setResultProps({
          detail: res.overall_summary,
          timing: `${res.score} / 100`,
          columns: ["Rubric point", "Result"],
          rows: [
            ...res.rubric_points_hit.map((p) => [p, "Met"]),
            ...res.rubric_points_missed.map((p) => [p, "Missing"]),
          ],
        });
        setFollowUpQuestion(null);
        setFollowUpAnswer("");
      }
    } catch (e) {
      setResultStatus("error");
      setResultProps({ detail: e instanceof Error ? e.message : "Something went wrong." });
    } finally {
      setRunning(false);
    }
  }

  if (loadError) {
    return (
      <div className={styles.page}>
        <button className={styles.back} onClick={() => navigate(`/practice/${track}`)}>
          ← All problems
        </button>
        <p>{loadError}</p>
      </div>
    );
  }

  if (!problem) {
    return (
      <div className={styles.page}>
        <button className={styles.back} onClick={() => navigate(`/practice/${track}`)}>
          ← All problems
        </button>
        <p style={{ color: "var(--color-neutral-500)" }}>Loading…</p>
      </div>
    );
  }

  const promptText = problem.track === "case" ? problem.case_prompt : problem.description;

  return (
    <div className={styles.page}>
      <button className={styles.back} onClick={() => navigate(`/practice/${track}`)}>
        ← All problems
      </button>

      <div className={styles.titleRow}>
        <div className={styles.titleLeft}>
          <div className={styles.metaLine}>
            <DifficultyPill difficulty={problem.difficulty} />
            <span className={styles.trackLabel}>{trackName(problem.track)}</span>
          </div>
          <h1 className={styles.h1}>{problem.title}</h1>
          <p className={styles.prompt}>{promptText}</p>
        </div>
        <div className={styles.actions}>
          <button className="btn btn-secondary" onClick={() => setPhoenixOpen((v) => !v)}>
            Ask Phoenix
          </button>
          <button className="btn btn-primary" onClick={run} disabled={running}>
            {running ? "Running…" : "Run & verify"}
          </button>
        </div>
      </div>

      {problem.track === "sql" && (
        <div className={styles.schemaStrip}>
          {Object.entries(problem.sample_tables).map(([name, tbl]) => (
            <SchemaCard key={name} name={name} cols={tbl.columns.join(" · ")} />
          ))}
        </div>
      )}

      {problem.track === "case" && problem.case_context && (
        <div className={styles.schemaStrip}>
          <SchemaCard name="context" cols={problem.case_context} />
        </div>
      )}

      <div className={styles.editorCard}>
        <div className={styles.toolbar}>
          <span className={styles.filename}>{filenameFor(problem.track)}</span>
          <span className={styles.shortcut}>⌘↵ to run</span>
        </div>
        <CodeEditor
          value={code}
          onChange={setCode}
          language={languageFor(problem.track)}
          onRun={run}
        />
        {followUpQuestion && (
          <div className={styles.followUp}>
            <p className={styles.followUpQuestion}>{followUpQuestion}</p>
            <textarea
              className={styles.followUpTextarea}
              value={followUpAnswer}
              onChange={(e) => setFollowUpAnswer(e.target.value)}
              placeholder="Your follow-up response…"
            />
            <button className="btn btn-primary" onClick={submitFollowUp} disabled={running || !followUpAnswer.trim()}>
              {running ? "Scoring…" : "Submit follow-up"}
            </button>
          </div>
        )}
        {!followUpQuestion && <ResultPanel status={resultStatus} {...resultProps} />}
      </div>

      <AskPhoenixDrawer
        open={phoenixOpen}
        onClose={() => setPhoenixOpen(false)}
        problemId={problem.id}
        code={code}
      />
    </div>
  );
}
