"""
Inbuilt support ticketing -- the site previously had no way at all for a
user to reach the team. An admin can now reply from the admin console
itself (see email_sender.py) rather than only via a mailto: link that
opens their own mail client. Good enough at zero-to-early-user scale; a
real helpdesk tool can replace this later if volume ever justifies it.

Attachments (one optional file per ticket submission, one optional file
per reply) are stored directly as BYTEA rather than a separate object-
storage service -- not worth a new external dependency at this volume.
List queries never SELECT the blob itself (only a has_attachment flag) so
listing a page of tickets doesn't pull megabytes of binary data along
with it; the actual bytes are fetched on demand via the get_*_attachment
functions.
"""

import psycopg2

import db


def create_ticket(
    user_id: str, email: str | None, subject: str, message: str,
    attachment_filename: str | None = None, attachment_content_type: str | None = None,
    attachment_data: bytes | None = None,
) -> dict:
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO support_tickets (user_id, email, subject, message, attachment_filename, attachment_content_type, attachment_data)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, user_id, email, subject, message, status, created_at, resolved_at,
                          (attachment_data IS NOT NULL) AS has_attachment, attachment_filename
                """,
                (user_id, email, subject, message, attachment_filename, attachment_content_type,
                 psycopg2.Binary(attachment_data) if attachment_data is not None else None),
            )
            return dict(cur.fetchone())


def get_ticket(ticket_id: int) -> dict | None:
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                """
                SELECT id, user_id, email, subject, message, status, created_at, resolved_at,
                       (attachment_data IS NOT NULL) AS has_attachment, attachment_filename
                FROM support_tickets WHERE id = %s
                """,
                (ticket_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_ticket_attachment(ticket_id: int) -> dict | None:
    """Returns {"filename", "content_type", "data": bytes} or None if this
    ticket has no attachment -- fetched separately from get_ticket/
    list_tickets so a normal page load never pulls the blob itself."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                "SELECT attachment_filename, attachment_content_type, attachment_data "
                "FROM support_tickets WHERE id = %s AND attachment_data IS NOT NULL",
                (ticket_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"filename": row["attachment_filename"], "content_type": row["attachment_content_type"], "data": bytes(row["attachment_data"])}


def get_reply_attachment(reply_id: int) -> dict | None:
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                "SELECT attachment_filename, attachment_content_type, attachment_data "
                "FROM ticket_replies WHERE id = %s AND attachment_data IS NOT NULL",
                (reply_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"filename": row["attachment_filename"], "content_type": row["attachment_content_type"], "data": bytes(row["attachment_data"])}


def list_tickets(status: str | None = None) -> list[dict]:
    """All tickets, newest first, each with its reply thread attached as
    `replies` (empty list if none). `status` ("open"/"resolved") filters
    to just that state -- omitted, returns everything. Neither tickets nor
    replies carry their attachment bytes here -- just has_attachment."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            base_select = (
                "SELECT id, user_id, email, subject, message, status, created_at, resolved_at, "
                "(attachment_data IS NOT NULL) AS has_attachment, attachment_filename "
                "FROM support_tickets"
            )
            if status:
                cur.execute(f"{base_select} WHERE status = %s ORDER BY created_at DESC", (status,))
            else:
                cur.execute(f"{base_select} ORDER BY created_at DESC")
            tickets = [dict(r) for r in cur.fetchall()]
            if not tickets:
                return tickets

            ticket_ids = [t["id"] for t in tickets]
            cur.execute(
                "SELECT id, ticket_id, message, created_at, "
                "(attachment_data IS NOT NULL) AS has_attachment, attachment_filename "
                "FROM ticket_replies WHERE ticket_id = ANY(%s) ORDER BY created_at",
                (ticket_ids,),
            )
            replies_by_ticket: dict[int, list[dict]] = {}
            for r in cur.fetchall():
                replies_by_ticket.setdefault(r["ticket_id"], []).append(dict(r))
            for t in tickets:
                t["replies"] = replies_by_ticket.get(t["id"], [])
            return tickets


def set_ticket_status(ticket_id: int, status: str) -> dict | None:
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE support_tickets SET
                    status = %s,
                    resolved_at = CASE WHEN %s = 'resolved' THEN now() ELSE NULL END
                WHERE id = %s
                RETURNING id, user_id, email, subject, message, status, created_at, resolved_at,
                          (attachment_data IS NOT NULL) AS has_attachment, attachment_filename
                """,
                (status, status, ticket_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def add_reply(
    ticket_id: int, message: str,
    attachment_filename: str | None = None, attachment_content_type: str | None = None,
    attachment_data: bytes | None = None,
) -> dict:
    """Records a reply that was (or is about to be) emailed out for this
    ticket. Called from main.py right after email_sender.send_email
    succeeds -- a reply that failed to send is never recorded, so the
    thread only ever shows replies that actually went out."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO ticket_replies (ticket_id, message, attachment_filename, attachment_content_type, attachment_data)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, ticket_id, message, created_at,
                          (attachment_data IS NOT NULL) AS has_attachment, attachment_filename
                """,
                (ticket_id, message, attachment_filename, attachment_content_type,
                 psycopg2.Binary(attachment_data) if attachment_data is not None else None),
            )
            return dict(cur.fetchone())
