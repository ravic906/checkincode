"""
Inbuilt support ticketing -- the site previously had no way at all for a
user to reach the team. Deliberately minimal: no email notifications, no
canned responses, just a queue an admin reads and marks resolved. Good
enough at zero-to-early-user scale; a real helpdesk tool can replace this
later if volume ever justifies it.
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


def list_tickets(status: str | None = None) -> list[dict]:
    """All tickets, newest first. `status` ("open"/"resolved") filters to
    just that state -- omitted, returns everything."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            if status:
                cur.execute(
                    "SELECT * FROM support_tickets WHERE status = %s ORDER BY created_at DESC",
                    (status,),
                )
            else:
                cur.execute("SELECT * FROM support_tickets ORDER BY created_at DESC")
            return [dict(r) for r in cur.fetchall()]


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
