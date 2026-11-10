"""The booking row and its state machine. transition() is the only code that changes a booking's status."""

from datetime import datetime

from app import events
from app.errors import Problem
from app.timeutil import utcnow

ALLOWED = {
    "pending_payment": {"confirmed", "expired", "cancelled"},
    "confirmed": {"picked_up", "cancelled", "no_show"},
    "picked_up": {"returned"},
}
EXTRA_COLUMNS = {"picked_up_at", "returned_at", "late_minutes", "return_note"}

BOOKING = """
SELECT b.id, b.code, b.user_id, b.status, lower(b.during) AS start_at, upper(b.during) AS end_at,
       b.hold_expires_at, b.quoted_paise, b.price_snapshot, b.picked_up_at, b.returned_at, b.late_minutes,
       b.overdue_notified_at, b.return_note, b.created_at, b.receipt_file_id,
       b.station_id, s.name AS station_name, b.bike_id, k.code AS bike_code,
       t.id AS bike_type_id, t.slug AS bike_type_slug, t.name AS bike_type_name,
       u.full_name AS customer_name, u.email AS customer_email, u.phone AS customer_phone
FROM booking b
JOIN app_user u ON u.id = b.user_id
JOIN station s ON s.id = b.station_id
JOIN bike k ON k.id = b.bike_id
JOIN bike_type t ON t.id = k.bike_type_id
"""


def get_booking(conn, booking_id: int, user_id: int | None = None, for_update: bool = False) -> dict:
    """With user_id, someone else's booking is 'not found', exactly like an id that does not exist."""
    sql = BOOKING + " WHERE b.id = %(id)s"
    if user_id is not None:
        sql += " AND b.user_id = %(user_id)s"
    if for_update:
        sql += " FOR UPDATE OF b"
    row = conn.execute(sql, {"id": booking_id, "user_id": user_id}).fetchone()
    if row is None:
        raise Problem(404, "Booking not found")
    return row


def get_by_code(conn, code: str, station_id: int) -> dict:
    """For the station desk: a booking at another station is 'not found'."""
    row = conn.execute(
        BOOKING + " WHERE b.code = %s AND b.station_id = %s FOR UPDATE OF b", (code.strip().upper(), station_id)
    ).fetchone()
    if row is None:
        raise Problem(404, "Booking not found", "Check the code. It must be a booking at this station.")
    return row


def transition(conn, booking_id: int, to: str, *, actor_id: int | None = None, reason: str | None = None,
               now: datetime | None = None, **columns) -> dict:
    """Moves a booking to a new status if the move is allowed, and records it.

    The UPDATE only matches while the booking is still in a status that may become `to` (compare-and-set), so two
    requests racing to move the same booking cannot both win. The history row and the outbox event are written in
    the caller's transaction: they exist exactly when the status change does.
    """
    now = now or utcnow()
    assert columns.keys() <= EXTRA_COLUMNS, columns
    sets = "".join(f", {col} = %({col})s" for col in columns)
    row = conn.execute(
        f"""WITH old AS (SELECT id, status FROM booking WHERE id = %(id)s FOR UPDATE)
            UPDATE booking b SET status = %(to)s{sets}
            FROM old
            WHERE b.id = old.id AND old.status = ANY(%(froms)s::booking_status[])
            RETURNING b.id, b.code, b.user_id, b.station_id, b.bike_id, old.status AS from_status""",
        {"id": booking_id, "to": to, "froms": [s for s, tos in ALLOWED.items() if to in tos], **columns},
    ).fetchone()
    if row is None:
        current = conn.execute("SELECT status FROM booking WHERE id = %s", (booking_id,)).fetchone()
        if current is None:
            raise Problem(404, "Booking not found")
        raise Problem(409, "Not allowed for this booking",
                      f"A booking that is {current['status'].replace('_', ' ')} cannot become {to.replace('_', ' ')}.")
    conn.execute(
        """INSERT INTO booking_status_history (booking_id, from_status, to_status, actor_id, reason, at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (booking_id, row["from_status"], to, actor_id, reason, now),
    )
    events.emit(conn, f"booking.{to}", booking_id, {
        "booking_id": booking_id, "user_id": row["user_id"], "station_id": row["station_id"],
        "bike_id": row["bike_id"], "from": row["from_status"], "at": now,
    })
    return row
