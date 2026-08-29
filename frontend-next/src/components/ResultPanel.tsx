import styles from "./ResultPanel.module.css";

export type ResultStatus = "idle" | "running" | "pass" | "fail" | "error";

export interface ResultPanelProps {
  status: ResultStatus;
  detail?: string;
  timing?: string;
  columns?: string[];
  rows?: unknown[][];
  /** Python track has no row/column diff to show -- just raw stdout/error text. */
  rawOutput?: string;
}

/**
 * Verdict language: "Verified" / "Not there yet" for pass/fail come
 * straight from the design handoff and are final copy. "Running…" and
 * "Error" states are not in the mock (the README flags them as an open
 * design gap) -- extended here using the same system rules already
 * established for pass/fail: no red anywhere, amber signals "something's
 * off" without implying a threat, and the running state stays neutral
 * until a verdict is known.
 */
export function ResultPanel({ status, detail, timing, columns = [], rows = [], rawOutput }: ResultPanelProps) {
  if (status === "idle") {
    return (
      <div className={styles.hint}>
        Run to check your answer. Output is compared against the verified result set, not a keyword match.
      </div>
    );
  }

  const barClass =
    status === "pass"
      ? styles.barPass
      : status === "running"
        ? styles.barRunning
        : styles.barFail; // fail and error share the amber treatment

  const dotClass =
    status === "pass" ? styles.dotPass : status === "running" ? styles.dotRunning : styles.dotFail;

  const verdictWord =
    status === "running"
      ? "Running…"
      : status === "pass"
        ? "Verified"
        : status === "error"
          ? "Couldn't run"
          : "Not there yet";

  return (
    <div className={`${styles.wrap} fade-up`}>
      <div className={`${styles.bar} ${barClass}`} role="status" aria-live="polite">
        <span className={`${styles.dot} ${dotClass}`} aria-hidden="true" />
        <span className={styles.verdict}>{verdictWord}</span>
        {detail && <span className={styles.detail}>{detail}</span>}
        {timing && <span className={styles.timing}>{timing}</span>}
      </div>
      {(status === "pass" || status === "fail") && columns.length > 0 && (
        <div className={styles.tableWrap}>
          <table className="table">
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i}>
                  {row.map((cell, j) => (
                    <td key={j}>{String(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rawOutput && (
        <div className={styles.tableWrap}>
          <pre className={styles.rawOutput}>{rawOutput}</pre>
        </div>
      )}
    </div>
  );
}
