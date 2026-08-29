import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchProblems } from "../api/client";
import type { ProblemSummary } from "../api/types";
import { DifficultyPill } from "../components/DifficultyPill";
import { TrackCard } from "../components/TrackCard";
import { TRACKS, trackName } from "../lib/tracks";
import styles from "./Home.module.css";

export function Home() {
  const navigate = useNavigate();
  const [unsolved, setUnsolved] = useState<ProblemSummary[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchProblems()
      .then((res) => {
        if (!cancelled) setUnsolved(res.problems.filter((p) => !p.solved).slice(0, 3));
      })
      .catch(() => {
        if (!cancelled) setUnsolved([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className={styles.page}>
      <div className={styles.kicker}>Interview Prep, Targeted for Success</div>
      <h1 className={styles.h1}>What do you want to practice?</h1>
      <p className={styles.sub}>
        Pick a track. Every answer is verified by actually running it — no guessing whether you're right.
      </p>

      <div className={styles.trackGrid} role="list">
        {TRACKS.map((t) => (
          <div role="listitem" key={t.id}>
            <TrackCard
              track={t}
              onSelect={() => navigate(t.id === "mock" ? "/mock" : `/practice/${t.id}`)}
            />
          </div>
        ))}
      </div>

      <div className={styles.continueHead}>
        <h2>Continue where you left off</h2>
        {unsolved && <span className={styles.continueCount}>{unsolved.length} problems</span>}
      </div>
      {unsolved === null ? (
        <p className={styles.emptyState}>Loading…</p>
      ) : unsolved.length === 0 ? (
        <p className={styles.emptyState}>You've solved everything in the free bank — nice work.</p>
      ) : (
        <div className={styles.tileGrid}>
          {unsolved.map((p) => (
            <button
              key={p.id}
              className={styles.tile}
              onClick={() => navigate(`/practice/${p.track}/${p.id}`)}
            >
              <div className={styles.tileHead}>
                <DifficultyPill difficulty={p.difficulty} />
                <span className={styles.tileTrackName}>{trackName(p.track)}</span>
              </div>
              <div className={styles.tileTitle}>{p.title}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
