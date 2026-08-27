/*
 * Not linked from the main nav for non-admins -- reached at
 * /admin-tickets.html. Accepts either the shared X-Admin-Token secret or a
 * signed-in Clerk account with is_admin=True (see main.py's _require_admin()
 * and admin-shared.js).
 */

initAdminSidebar("tickets");

let allTickets = [];
let currentStatus = "open";

function renderTicketCard(t) {
  const when = t.created_at ? new Date(t.created_at).toLocaleString() : "—";
  const resolvedWhen = t.resolved_at ? new Date(t.resolved_at).toLocaleString() : null;
  return `
    <div class="ticket-card" data-ticket-id="${t.id}">
      <div class="ticket-card-header">
        <div>
          <div class="ticket-subject">${escapeHtml(t.subject)}</div>
          <div class="user-sublabel">${escapeHtml(t.email || t.user_id)} · ${when}</div>
        </div>
        <span class="pill ticket-status-${t.status}">${t.status === "resolved" ? "Resolved" : "Open"}</span>
      </div>
      <p class="ticket-message">${escapeHtml(t.message)}</p>
      <div class="ticket-card-actions">
        ${t.status === "open"
          ? `<button class="history-load-btn" data-action="resolve">Mark Resolved</button>`
          : `<button class="history-load-btn" data-action="reopen">Reopen</button>`}
        <a class="ticket-reply-link" href="mailto:${encodeURIComponent(t.email || "")}?subject=${encodeURIComponent("Re: " + t.subject)}">Reply by email</a>
        ${resolvedWhen ? `<span class="user-sublabel">Resolved ${resolvedWhen}</span>` : ""}
      </div>
    </div>
  `;
}

function renderTickets(tickets) {
  const list = document.getElementById("ticketsList");
  list.innerHTML = tickets.length
    ? tickets.map(renderTicketCard).join("")
    : `<p class="empty-note">No tickets match.</p>`;
  list.querySelectorAll(".ticket-card").forEach((card) => {
    const id = Number(card.dataset.ticketId);
    const btn = card.querySelector("[data-action]");
    if (!btn) return;
    btn.onclick = async () => {
      const newStatus = btn.dataset.action === "resolve" ? "resolved" : "open";
      btn.disabled = true;
      try {
        await adminApi(`/api/admin/tickets/${id}/status`, {
          method: "POST",
          body: JSON.stringify({ status: newStatus }),
        });
        await loadTickets();
      } catch (err) {
        alert(`Couldn't update ticket: ${err.message}`);
        btn.disabled = false;
      }
    };
  });
}

function applyTicketSearch() {
  const q = document.getElementById("ticketSearchInput").value.trim().toLowerCase();
  if (!q) { renderTickets(allTickets); return; }
  const filtered = allTickets.filter((t) =>
    [t.email, t.subject, t.message, t.user_id].some((field) => field && field.toLowerCase().includes(q))
  );
  renderTickets(filtered);
}

async function loadTickets() {
  const list = document.getElementById("ticketsList");
  list.innerHTML = `<div class="loading-dots">Loading…</div>`;
  try {
    const qs = currentStatus ? `?status=${encodeURIComponent(currentStatus)}` : "";
    const data = await adminApi(`/api/admin/tickets${qs}`);
    allTickets = data.tickets;
    applyTicketSearch();
  } catch (err) {
    list.innerHTML = `<div class="error-banner">${escapeHtml(err.message)}</div>`;
  }
}

document.getElementById("ticketTabs").querySelectorAll("button").forEach((btn) => {
  btn.onclick = () => {
    document.getElementById("ticketTabs").querySelectorAll("button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentStatus = btn.dataset.status;
    loadTickets();
  };
});
document.getElementById("ticketSearchInput").addEventListener("input", applyTicketSearch);

loadTickets();
