import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { fetchProblems } from "../api/client";
import type { Difficulty, ProblemSummary } from "../api/types";
import { ProblemRow } from "../components/ProblemRow";
import { TRACKS } from "../lib/tracks";
import styles from "./ProblemList.module.css";

const DIFFS: (Difficulty | "All")[] = ["All", "Easy", "Medium", "Hard"];

export function ProblemList() {
  const { track = "sql" } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [problems, setProblems] = useState<ProblemSummary[] | null>(null);

  const difficulty = (searchParams.get("difficulty") as Difficulty | "All") || "All";
  const unsolvedOnly = searchParams.get("unsolved") === "1";

  const trackInfo = TRACKS.find((t) => t.id === track);

  useEffect(() => {
    let cancelled = false;
    setProblems(null);
    fetchProblems({ track, difficulty: difficulty === "All" ? undefined : difficulty })
      .then((res) => {
        if (!cancelled) setProblems(res.problems);
      })
      .catch(() => {
        if (!cancelled) setProblems([]);
      });
    return () => {
      cancelled = true;
    };
  }, [track, difficulty]);

  const visible = (problems ?? []).filter((p) => !unsolvedOnly || !p.solved);

  function setDifficulty(d: Difficulty | "All") {
    const next = new URLSearchParams(searchParams);
    if (d === "All") next.delete("difficulty");
    else next.set("difficulty", d);
    setSearchParams(next, { replace: true });
  }

  function toggleUnsolved() {
    const next = new URLSearchParams(searchParams);
    if (unsolvedOnly) next.delete("unsolved");
    else next.set("unsolved", "1");
    setSearchParams(next, { replace: true });
  }

  return (
    <div className={styles.page}>
      <button className={styles.back} onClick={() => navigate("/")}>
        ← All tracks
      </button>

      <div className={styles.headRow}>
        <h1 className={styles.h1}>{trackInfo ? `${trackInfo.name} practice` : "Practice"}</h1>
        {trackInfo && <p className={styles.blurb}>{trackInfo.blurb}</p>}
      </div>

      <div className={styles.filters} role="tablist" aria-label="Filter by difficulty">
        {DIFFS.map((d) => (
          <button
            key={d}
            role="tab"
            aria-selected={difficulty === d}
            className="pill"
            aria-pressed={difficulty === d}
            onClick={() => setDifficulty(d)}
          >
            {d === "All" ? "All difficulties" : d}
          </button>
        ))}
      </div>

      <div className={styles.metaRow}>
        <span className={styles.resultCount}>
          {visible.length} {visible.length === 1 ? "problem" : "problems"}
        </span>
        <button
          className={`${styles.unsolvedToggle} ${unsolvedOnly ? styles.unsolvedToggleActive : ""}`}
          onClick={toggleUnsolved}
        >
          {unsolvedOnly ? "Unsolved only" : "Solved + unsolved"}
        </button>
      </div>

      <div className={styles.list}>
        {problems === null ? (
          <div className={styles.empty}>Loading…</div>
        ) : visible.length === 0 ? (
          <div className={styles.empty}>No problems match these filters.</div>
        ) : (
          visible.map((p) => (
            <ProblemRow
              key={p.id}
              problem={p}
              selected={false}
              onSelect={() => navigate(`/practice/${track}/${p.id}`)}
            />
          ))
        )}
      </div>
    </div>
  );
}
