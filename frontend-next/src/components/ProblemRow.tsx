import type { ProblemSummary } from "../api/types";
import { DifficultyPill } from "./DifficultyPill";
import styles from "./ProblemRow.module.css";

export function ProblemRow({
  problem,
  selected,
  onSelect,
}: {
  problem: ProblemSummary;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      className={`${styles.row} ${selected ? styles.rowSelected : ""}`}
      onClick={onSelect}
      aria-current={selected ? "true" : undefined}
    >
      <div className={styles.top}>
        <span
          className={`${styles.stateDot} ${problem.solved ? styles.stateDotSolved : ""}`}
          aria-hidden="true"
        />
        <span className={styles.title}>{problem.title}</span>
        {problem.locked && <span className="tag-pro">PRO</span>}
        <DifficultyPill difficulty={problem.difficulty} />
      </div>
      {problem.tags.length > 0 && (
        <div className={styles.tags}>
          {problem.tags.map((tag) => (
            <span key={tag} className={styles.tag}>
              {tag}
            </span>
          ))}
        </div>
      )}
    </button>
  );
}
