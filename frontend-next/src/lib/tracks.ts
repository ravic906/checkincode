import type { TrackInfo } from "../api/types";

// Card copy is final per the design handoff README -- do not reword.
export const TRACKS: TrackInfo[] = [
  {
    id: "sql",
    name: "SQL",
    blurb: "Joins, aggregation, subqueries, window functions — graded by DuckDB.",
    meta: "Real interview-style questions, easy to hard",
    pro: false,
  },
  {
    id: "python",
    name: "Python",
    blurb:
      "Data structures, strings, iterators, OOP, plus pandas, NumPy & statistics — graded by a real sandbox.",
    meta: "Real interview-style questions, easy to hard",
    pro: false,
  },
  {
    id: "case",
    name: "Business Case",
    blurb:
      "Open-ended metric, root-cause & pipeline-design questions — no single right answer, scored by an AI interviewer.",
    meta: "Data Analyst & Data Engineer case rounds",
    pro: false,
  },
  {
    id: "mock",
    name: "Mock Interview",
    blurb: "45-min spoken SQL interview with an adaptive virtual interviewer.",
    meta: "Personalized from your resume, or a generic round",
    pro: true,
  },
];

export function trackName(id: string): string {
  return TRACKS.find((t) => t.id === id)?.name ?? id;
}
