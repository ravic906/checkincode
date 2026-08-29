import type { ProgressTopic } from "../api/types";
import styles from "./TopicReadinessBar.module.css";

export function TopicReadinessBar({ topic }: { topic: ProgressTopic }) {
  const pct = topic.total ? Math.round((topic.solved / topic.total) * 100) : 0;
  return (
    <div className={styles.row}>
      <div className={styles.head}>
        <span className={styles.name}>{topic.name}</span>
        <span className={styles.detail}>{topic.solved} solved</span>
      </div>
      <div className={styles.track}>
        <div
          className={`${styles.fill} ${pct < 50 ? styles.fillWeak : styles.fillStrong}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
