initAdminSidebar("dashboard");

async function loadDashboard() {
  const grid = document.getElementById("statGrid");
  const chart = document.getElementById("categoryChart");
  const errorArea = document.getElementById("errorArea");
  grid.innerHTML = `<div class="loading-dots">Loading…</div>`;
  try {
    const [userSummary, liveProblems, pending, cadence, solvedBreakdown] = await Promise.all([
      adminApi("/api/admin/users/summary"),
      adminApi("/api/admin/problems/live"),
      adminApi("/api/admin/problems/pending"),
      adminApi("/api/admin/cadence"),
      adminApi("/api/admin/stats/solved-by-category"),
    ]);

    grid.innerHTML = `
      <div class="stat-card"><div class="num">${userSummary.total_users}</div><div class="label">Total Users</div></div>
      <div class="stat-card"><div class="num">${userSummary.paid_users}</div><div class="label">Pro Users</div></div>
      <div class="stat-card"><div class="num">${userSummary.free_users}</div><div class="label">Free Users</div></div>
      <div class="stat-card"><div class="num">${liveProblems.problems.length}</div><div class="label">Live Problems</div></div>
      <div class="stat-card"><div class="num">${pending.problems.length}</div><div class="label">Pending Drafts</div></div>
    `;

    chart.innerHTML = renderCategoryPie(solvedBreakdown, 140);

    if (cadence.last_batch_generated_at) {
      const when = new Date(cadence.last_batch_generated_at).toLocaleString();
      chart.innerHTML += `<p style="color:var(--muted); font-size:12px; margin-top:14px;">Last batch generated: ${when}</p>`;
    }
  } catch (err) {
    errorArea.innerHTML = `<div class="error-banner">${escapeHtml(err.message)}</div>`;
    grid.innerHTML = "";
  }
}

loadDashboard();
