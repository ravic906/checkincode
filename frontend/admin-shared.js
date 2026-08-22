/*
 * Shared across all four admin pages (Dashboard, Problems, Users, Admins).
 * Load order: config.js, auth.js, admin-shared.js, then the page's own JS.
 *
 * Auth: every call attaches BOTH the static X-Admin-Token (if present, in
 * the sidebar's collapsed fallback field or localStorage) AND a signed-in
 * Clerk session token (if any) -- the backend's _require_admin() accepts
 * either. Normal day-to-day use should just be "sign in as an admin
 * account"; the token is a fallback/bootstrap path, not the primary one.
 */

const ADMIN_API_BASE = window.API_BASE || "http://127.0.0.1:8000";

function getToken() {
  const el = document.getElementById("adminTokenFallback");
  return (el && el.value.trim()) || localStorage.getItem("phoenix_admin_token") || "";
}

async function adminApi(path, options = {}) {
  const token = getToken();
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };
  if (token) headers["X-Admin-Token"] = token;
  if (typeof getAuthToken === "function") {
    const clerkToken = await getAuthToken();
    if (clerkToken) headers["Authorization"] = `Bearer ${clerkToken}`;
  }
  const res = await fetch(`${ADMIN_API_BASE}${path}`, { ...options, headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --- Category bucketing (pandas/numpy/stats are topics within
// track='python', not their own track -- mirrors problems.py's
// _category_bucket()). ---

const PANDAS_TOPICS = new Set([
  "Pandas Data Cleaning & Missing Data",
  "Pandas Merging, Joining & Reshaping",
  "Pandas GroupBy & Aggregation",
  "Pandas Time Series Operations",
]);
const NUMPY_TOPICS = new Set([
  "NumPy Array Creation & Indexing",
  "NumPy Broadcasting & Vectorization",
  "NumPy Aggregations & Boolean Masking",
]);
const STATS_TOPICS = new Set([
  "Descriptive Statistics",
  "Probability Fundamentals",
  "Distributions",
  "Hypothesis Testing",
  "A/B Testing & Experimental Design",
  "Confidence Intervals & Estimation",
  "Regression Fundamentals",
  "Bayesian Reasoning",
  "Sampling & Bias",
]);
function categoryBucket(track, topic) {
  if (track === "case") return "case";
  if (track !== "python") return "sql";
  if (PANDAS_TOPICS.has(topic)) return "pandas";
  if (NUMPY_TOPICS.has(topic)) return "numpy";
  if (STATS_TOPICS.has(topic)) return "stats";
  return "python";
}

const CATEGORY_META = {
  sql: { label: "SQL", color: "var(--accent)" },
  python: { label: "Python", color: "var(--green)" },
  stats: { label: "Statistics", color: "#b083f0" },
  pandas: { label: "Pandas", color: "var(--amber)" },
  numpy: { label: "NumPy", color: "var(--red)" },
  case: { label: "Business Case", color: "#e07b39" },
};

// Small SVG pie chart from {sql, python, stats, pandas, numpy} counts,
// plus a text legend -- no charting library, this is a no-build-step
// frontend and a handful of arcs is simple enough to compute directly.
function renderCategoryPie(counts, size = 120) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const r = size / 2;
  const cx = r, cy = r;
  let angle = -Math.PI / 2;
  let slices = "";
  if (total === 0) {
    slices = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="var(--panel-2)" />`;
  } else {
    for (const [key, meta] of Object.entries(CATEGORY_META)) {
      const value = counts[key] || 0;
      if (value === 0) continue;
      const slice = (value / total) * Math.PI * 2;
      const x1 = cx + r * Math.cos(angle);
      const y1 = cy + r * Math.sin(angle);
      angle += slice;
      const x2 = cx + r * Math.cos(angle);
      const y2 = cy + r * Math.sin(angle);
      const largeArc = slice > Math.PI ? 1 : 0;
      slices += `<path d="M${cx},${cy} L${x1},${y1} A${r},${r} 0 ${largeArc} 1 ${x2},${y2} Z" fill="${meta.color}" />`;
    }
  }
  const legend = Object.entries(CATEGORY_META).map(([key, meta]) => `
    <div class="legend-row">
      <span class="legend-swatch" style="background:${meta.color};"></span>
      <span>${meta.label}</span>
      <span class="legend-count">${counts[key] || 0}</span>
    </div>
  `).join("");
  return `
    <div class="pie-chart-wrap">
      <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-label="Solved problems by category">${slices}</svg>
      <div class="legend">${legend}</div>
    </div>
  `;
}

// --- Sidebar: active-link highlight + token fallback toggle. Each page
// calls initAdminSidebar("dashboard"|"problems"|"users"|"admins"). ---

function initAdminSidebar(activePage) {
  document.querySelectorAll(".admin-sidebar-nav a").forEach(a => {
    a.classList.toggle("active", a.dataset.page === activePage);
  });
  const toggle = document.getElementById("tokenFallbackToggle");
  const panel = document.getElementById("adminTokenFallbackPanel");
  if (toggle && panel) {
    toggle.onclick = () => panel.classList.toggle("open");
  }
  const input = document.getElementById("adminTokenFallback");
  const saved = localStorage.getItem("phoenix_admin_token");
  if (input && saved) input.value = saved;
  if (input) {
    input.addEventListener("change", () => localStorage.setItem("phoenix_admin_token", input.value.trim()));
  }
}
