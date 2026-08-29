import type { ProgressRecent } from "../api/types";
import { relativeTime } from "../lib/relativeTime";
import styles from "./RecentRow.module.css";

export function RecentRow({ item }: { item: ProgressRecent }) {
  return (
    <div className={styles.row}>
      <span className={`${styles.dot} ${item.correct ? styles.dotPass : styles.dotFail}`} aria-hidden="true" />
      <span className={styles.title}>{item.title}</span>
      <span className={styles.when}>{relativeTime(item.submitted_at)}</span>
    </div>
  );
}
