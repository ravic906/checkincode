import type { ProgressActivityDay } from "../api/types";
import { computeStreak } from "../lib/streak";
import styles from "./ActivityHeatmap.module.css";

const LEVELS = ["#232532", "var(--color-accent-800)", "var(--color-accent-600)", "var(--color-accent)"];

function intensity(count: number): number {
  if (count <= 0) return 0;
  if (count === 1) return 1;
  if (count <= 3) return 2;
  return 3;
}

export function ActivityHeatmap({ activity }: { activity: ProgressActivityDay[] }) {
  const streak = computeStreak(activity);
  let longest = 0;
  let run = 0;
  for (const day of activity) {
    if (day.count > 0) {
      run += 1;
      longest = Math.max(longest, run);
    } else {
      run = 0;
    }
  }

  return (
    <>
      <div className={styles.grid}>
        {activity.map((day) => (
          <span
            key={day.date}
            className={styles.cell}
            style={{ background: LEVELS[intensity(day.count)] }}
            title={`${day.date}: ${day.count} submission${day.count === 1 ? "" : "s"}`}
          />
        ))}
      </div>
      <div className={styles.caption}>
        {streak > 0 ? `${streak}-day streak` : "No active streak"} · longest {longest}
      </div>
    </>
  );
}
