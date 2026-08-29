import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchProgress } from "../api/client";
import type { ProgressResponse } from "../api/types";
import { ActivityHeatmap } from "../components/ActivityHeatmap";
import { RecentRow } from "../components/RecentRow";
import { StatCard } from "../components/StatCard";
import { TopicReadinessBar } from "../components/TopicReadinessBar";
import { generateHeadline } from "../lib/progressHeadline";
import styles from "./ProgressBoard.module.css";

export function ProgressBoard() {
  const navigate = useNavigate();
  const [progress, setProgress] = useState<ProgressResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchProgress()
      .then((p) => {
        if (!cancelled) setProgress(p);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message || "Couldn't load your progress.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className={styles.page}>
      <button className={styles.back} onClick={() => navigate("/")}>
        ← Back to practice
      </button>

      <div className={styles.kicker}>Last 30 days</div>
      <h1 className={styles.h1}>{progress ? generateHeadline(progress.topics) : "Loading your progress…"}</h1>
      <p className={styles.sub}>
        Readiness is measured on verified solutions only — problems you ran and passed, weighted by difficulty and
        how recently you solved them.
      </p>

      {error && <p style={{ color: "var(--color-fail-text)" }}>{error}</p>}

      {progress && (
        <>
          <div className={styles.statGrid}>
            <StatCard value={progress.problems_verified} label="Problems verified" />
            <StatCard value={`${progress.first_run_pass_rate}%`} label="First-run pass rate" />
            <StatCard value={progress.mock_interviews} label="Mock interviews this month" />
          </div>

          <div className={styles.columns}>
            <section className={styles.left}>
              <h2 className={styles.h2}>Readiness by topic</h2>
              {progress.topics.length === 0 ? (
                <p style={{ color: "var(--color-neutral-600)", fontSize: 13 }}>
                  Solve a few problems to see per-topic readiness here.
                </p>
              ) : (
                progress.topics.map((t) => <TopicReadinessBar key={t.name} topic={t} />)
              )}
            </section>

            <section className={styles.right}>
              <h2 className={styles.h2}>Activity</h2>
              <ActivityHeatmap activity={progress.activity} />

              <h2 className={styles.h2}>Recent</h2>
              {progress.recent.length === 0 ? (
                <p style={{ color: "var(--color-neutral-600)", fontSize: 13 }}>Nothing submitted yet.</p>
              ) : (
                progress.recent.map((r, i) => <RecentRow key={i} item={r} />)
              )}
            </section>
          </div>
        </>
      )}
    </div>
  );
}
