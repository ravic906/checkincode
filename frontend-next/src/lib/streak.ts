import type { ProgressActivityDay } from "../api/types";

/**
 * Counts consecutive active days ending today (or yesterday, so the streak
 * doesn't reset to 0 the moment midnight passes before today's first
 * solve). `activity` is oldest-first, one entry per day.
 */
export function computeStreak(activity: ProgressActivityDay[]): number {
  const byDate = new Map(activity.map((d) => [d.date, d.count]));
  const today = new Date();
  let streak = 0;
  let cursor = new Date(today);

  const todayKey = cursor.toISOString().slice(0, 10);
  if ((byDate.get(todayKey) ?? 0) === 0) {
    cursor.setDate(cursor.getDate() - 1); // allow "yesterday" as the streak's most recent day
  }

  // eslint-disable-next-line no-constant-condition
  while (true) {
    const key = cursor.toISOString().slice(0, 10);
    const count = byDate.get(key);
    if (count === undefined || count === 0) break;
    streak += 1;
    cursor.setDate(cursor.getDate() - 1);
  }
  return streak;
}
