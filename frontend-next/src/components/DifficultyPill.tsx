import type { Difficulty } from "../api/types";

export function DifficultyPill({ difficulty }: { difficulty: Difficulty }) {
  const variant = difficulty.toLowerCase();
  return <span className={`diff-pill diff-pill--${variant}`}>{difficulty}</span>;
}
