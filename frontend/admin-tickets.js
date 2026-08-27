/*
 * Not linked from the main nav for non-admins -- reached at
 * /admin-tickets.html. Accepts either the shared X-Admin-Token secret or a
 * signed-in Clerk account with is_admin=True (see main.py's _require_admin()
 * and admin-shared.js).
 */

initAdminSidebar("tickets");

let allTickets = [];
let currentStatus = "open";

// Shared by both a ticket's own attachment and a reply's -- fetches the
// blob (with proper auth, see adminApiBlob) and opens it in a new tab
// rather than trying to point a plain <a>/<img> at the endpoint directly.
async function openAttachment(path) {
  try {
    const blob = await adminApiBlob(path);
    const url = URL.createObjectURL(blob);
    window.open(url, "_blank");
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch (err) {
    alert(`Couldn't load attachment: ${err.message}`);
  }
}

function attachmentChip(filename, path) {
  return `<button type="button" class="attachment-chip" data-attachment-path="${escapeHtml(path)}">📎 ${escapeHtml(filename)}</button>`;
}

function renderReply(r, ticketId) {
  const when = r.created_at ? new Date(r.created_at).toLocaleString() : "—";
  return `
    <div class="ticket-reply">
      <div class="user-sublabel">You replied · ${when}</div>
      <p class="ticket-reply-text">${escapeHtml(r.message)}</p>
      ${r.has_attachment ? attachmentChip(r.attachment_filename, `/api/admin/tickets/${ticketId}/replies/${r.id}/attachment`) : ""}
    </div>
  `;
}

function renderTicketCard(t) {
  const when = t.created_at ? new Date(t.created_at).toLocaleString() : "—";
  const resolvedWhen = t.resolved_at ? new Date(t.resolved_at).toLocaleString() : null;
  const replies = t.replies || [];
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
      ${t.has_attachment ? attachmentChip(t.attachment_filename, `/api/admin/tickets/${t.id}/attachment`) : ""}
      ${replies.length ? `<div class="ticket-replies">${replies.map((r) => renderReply(r, t.id)).join("")}</div>` : ""}
      <div class="ticket-card-actions">
        ${t.status === "open"
          ? `<button class="history-load-btn" data-action="resolve">Mark Resolved</button>`
          : `<button class="history-load-btn" data-action="reopen">Reopen</button>`}
        ${resolvedWhen ? `<span class="user-sublabel">Resolved ${resolvedWhen}</span>` : ""}
      </div>
      ${t.email ? `
        <form class="ticket-reply-form" data-reply-form>
          <div class="ticket-reply-form-fields">
            <textarea placeholder="Write a reply…" required></textarea>
            <input type="file" class="ticket-reply-file" title="Attach a file (optional)" />
          </div>
          <button type="submit" class="submit-btn">Send Reply</button>
        </form>
      ` : `<p class="empty-note">No email on file for this ticket -- can't reply.</p>`}
    </div>
  `;
}

function renderTickets(tickets) {
  const list = document.getElementById("ticketsList");
  list.innerHTML = tickets.length
    ? tickets.map(renderTicketCard).join("")
    : `<p class="empty-note">No tickets match.</p>`;

  list.querySelectorAll("[data-attachment-path]").forEach((btn) => {
    btn.onclick = () => openAttachment(btn.dataset.attachmentPath);
  });

  list.querySelectorAll(".ticket-card").forEach((card) => {
    const id = Number(card.dataset.ticketId);
    const btn = card.querySelector("[data-action]");
    if (btn) {
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
    }

    const replyForm = card.querySelector("[data-reply-form]");
    if (replyForm) {
      replyForm.onsubmit = async (e) => {
        e.preventDefault();
        const textarea = replyForm.querySelector("textarea");
        const fileInput = replyForm.querySelector(".ticket-reply-file");
        const message = textarea.value.trim();
        if (!message) return;
        const sendBtn = replyForm.querySelector("button");
        sendBtn.disabled = true;
        sendBtn.textContent = "Sending…";
        try {
          const formData = new FormData();
          formData.append("message", message);
          if (fileInput.files[0]) formData.append("attachment", fileInput.files[0]);
          await adminApi(`/api/admin/tickets/${id}/reply`, { method: "POST", body: formData });
          await loadTickets();
        } catch (err) {
          alert(`Couldn't send reply: ${err.message}`);
          sendBtn.disabled = false;
          sendBtn.textContent = "Send Reply";
        }
      };
    }
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
