import type { ProgressTopic } from "../api/types";

/** Mirrors the mock's "generated from the data" headline, driven by real
 * per-topic readiness instead of a hardcoded example. */
export function generateHeadline(topics: ProgressTopic[]): string {
  if (topics.length === 0) {
    return "Solve a few problems and this page will tell you what's ready and what isn't.";
  }
  const withPct = topics.map((t) => ({ ...t, pct: t.total ? t.solved / t.total : 0 }));
  const strongest = withPct.reduce((a, b) => (b.pct > a.pct ? b : a));
  const weakest = withPct.reduce((a, b) => (b.pct < a.pct ? b : a));

  if (strongest.name === weakest.name || strongest.pct === weakest.pct) {
    return strongest.pct >= 0.7
      ? `You're interview-ready across the board.`
      : `Keep going — no topic is interview-ready yet.`;
  }
  const readyPart = strongest.pct >= 0.7 ? `You're interview-ready on ${strongest.name}.` : "";
  const weakPart = weakest.pct < 0.5 ? `${weakest.name} needs another week.` : "";
  return [readyPart, weakPart].filter(Boolean).join(" ") || "Keep practicing — readiness is still building up.";
}
