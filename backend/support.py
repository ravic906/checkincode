"""
Inbuilt support ticketing -- the site previously had no way at all for a
user to reach the team. An admin can now reply from the admin console
itself (see email_sender.py) rather than only via a mailto: link that
opens their own mail client. Good enough at zero-to-early-user scale; a
real helpdesk tool can replace this later if volume ever justifies it.
"""

import db


def create_ticket(user_id: str, email: str | None, subject: str, message: str) -> dict:
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO support_tickets (user_id, email, subject, message)
                VALUES (%s, %s, %s, %s)
                RETURNING id, user_id, email, subject, message, status, created_at, resolved_at
                """,
                (user_id, email, subject, message),
            )
            return dict(cur.fetchone())


def get_ticket(ticket_id: int) -> dict | None:
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM support_tickets WHERE id = %s", (ticket_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def list_tickets(status: str | None = None) -> list[dict]:
    """All tickets, newest first, each with its reply thread attached as
    `replies` (empty list if none). `status` ("open"/"resolved") filters
    to just that state -- omitted, returns everything."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            if status:
                cur.execute(
                    "SELECT * FROM support_tickets WHERE status = %s ORDER BY created_at DESC",
                    (status,),
                )
            else:
                cur.execute("SELECT * FROM support_tickets ORDER BY created_at DESC")
            tickets = [dict(r) for r in cur.fetchall()]
            if not tickets:
                return tickets

            ticket_ids = [t["id"] for t in tickets]
            cur.execute(
                "SELECT * FROM ticket_replies WHERE ticket_id = ANY(%s) ORDER BY created_at",
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
                RETURNING id, user_id, email, subject, message, status, created_at, resolved_at
                """,
                (status, status, ticket_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def add_reply(ticket_id: int, message: str) -> dict:
    """Records a reply that was (or is about to be) emailed out for this
    ticket. Called from main.py right after email_sender.send_email
    succeeds -- a reply that failed to send is never recorded, so the
    thread only ever shows replies that actually went out."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO ticket_replies (ticket_id, message)
                VALUES (%s, %s)
                RETURNING id, ticket_id, message, created_at
                """,
                (ticket_id, message),
            )
            return dict(cur.fetchone())
