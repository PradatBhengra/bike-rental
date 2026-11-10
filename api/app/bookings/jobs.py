"""Background work for bookings: the every-minute checks and the emails sent when a booking changes."""

from datetime import datetime, timedelta

from app import events, mail
from app.bookings import service, states
from app.catalog import service as catalog
from app.config import settings
from app.payments import service as payments
from app.timeutil import fmt, rupees


def expire_holds(conn, now: datetime) -> int:
    """A pending_payment booking past its hold becomes expired, which frees the bike."""
    rows = conn.execute(
        "SELECT id FROM booking WHERE status = 'pending_payment' AND hold_expires_at < %s FOR UPDATE SKIP LOCKED", (now,)
    ).fetchall()
    for row in rows:
        states.transition(conn, row["id"], "expired", reason="hold ran out", now=now)
        payments.void_unpaid(conn, row["id"])
    return len(rows)


def mark_no_shows(conn, now: datetime) -> int:
    cutoff = now - timedelta(minutes=catalog.get_setting(conn, "no_show_after_minutes"))
    rows = conn.execute(
        "SELECT id FROM booking WHERE status = 'confirmed' AND lower(during) < %s FOR UPDATE SKIP LOCKED", (cutoff,)
    ).fetchall()
    pct = catalog.get_setting(conn, "no_show_refund_pct")
    for row in rows:
        states.transition(conn, row["id"], "no_show", reason="not picked up", now=now)
        payments.refund_booking(conn, row["id"], pct, "no-show", now)
    return len(rows)


def flag_overdue(conn, now: datetime) -> int:
    """Overdue is a flag, not a status: the ride is still picked_up. Each ride is flagged once."""
    cutoff = now - timedelta(minutes=catalog.get_setting(conn, "late_grace_minutes"))
    rows = conn.execute(
        """UPDATE booking SET overdue_notified_at = %s
           WHERE status = 'picked_up' AND upper(during) < %s AND overdue_notified_at IS NULL
           RETURNING id, user_id, station_id, bike_id""",
        (now, cutoff),
    ).fetchall()
    for row in rows:
        events.emit(conn, "booking.late", row["id"], {"booking_id": row["id"], "user_id": row["user_id"],
                                                      "station_id": row["station_id"], "at": now})
        _knock_on(conn, row["bike_id"], now)
    return len(rows)


def _knock_on(conn, bike_id: int, now: datetime) -> None:
    """The overdue bike's next customer, if they start within the hour, is moved to another bike or warned."""
    upcoming = conn.execute(
        states.BOOKING + " WHERE b.bike_id = %s AND b.status = 'confirmed' AND lower(b.during) < %s",
        (bike_id, now + timedelta(hours=1)),
    ).fetchall()
    for booking in upcoming:
        if not service.reassign_bike(conn, booking, now):
            user = conn.execute("SELECT * FROM app_user WHERE id = %s", (booking["user_id"],)).fetchone()
            mail.queue_email(
                conn, user["id"], user["email"], f"Your bike may be late: {booking['code']}",
                f"Hi {user['full_name']},\n\nThe bike for your booking {booking['code']} at {booking['station_name']} "
                f"({fmt(booking['start_at'])}) has not been returned yet, and no other {booking['bike_type_name']} is free. "
                "Staff at the station will help when you arrive; you can also cancel for a refund.",
                kind="bike_delayed", dedupe_key=f"bike_delayed:{booking['id']}",
            )


def booking_email_job(conn, payload: dict) -> None:
    """Outbox consumer: one email per booking event."""
    booking = states.get_booking(conn, payload["booking_id"])
    user = conn.execute("SELECT * FROM app_user WHERE id = %s", (booking["user_id"],)).fetchone()
    when = f"{fmt(booking['start_at'])} to {fmt(booking['end_at'])}"
    link = f"{settings.app_url}/bookings/{booking['id']}"
    what = f"{booking['bike_type_name']} at {booking['station_name']}, {when}"
    subject, text = {
        "booking.confirmed": (
            f"Booking confirmed: {booking['code']}",
            f"Your booking is confirmed.\n\n{what}\nPaid: {rupees(booking['quoted_paise'])}\n\n"
            f"Show the code {booking['code']} at the station desk to collect the bike.\n{link}"),
        "booking.expired": (
            f"Booking expired: {booking['code']}",
            f"We did not receive a payment in time, so the bike was released.\n\n{what}\n\nYou can book again any time."),
        "booking.cancelled": (
            f"Booking cancelled: {booking['code']}",
            f"Your booking was cancelled.\n\n{what}\n\nAny refund due is on its way; we email you when it is done.\n{link}"),
        "booking.no_show": (
            f"Missed booking: {booking['code']}",
            f"The bike was not collected, so the booking was closed as a no-show.\n\n{what}\n{link}"),
        "booking.late": (
            f"Your ride is overdue: {booking['code']}",
            f"Your ride was due back at {fmt(booking['end_at'])}. Please return the bike to {booking['station_name']}.\n"
            "A late fee is charged for each started hour after the grace period."),
    }[payload["type"]]
    mail.queue_email(conn, user["id"], user["email"], subject, f"Hi {user['full_name']},\n\n{text}", kind=payload["type"],
                     dedupe_key=f"email:{payload['event_id']}")


def refund_email_job(conn, payload: dict) -> None:
    user = conn.execute("SELECT * FROM app_user WHERE id = %s", (payload["user_id"],)).fetchone()
    mail.queue_email(
        conn, user["id"], user["email"], f"Refund of {rupees(payload['amount_paise'])}",
        f"Hi {user['full_name']},\n\nWe refunded {rupees(payload['amount_paise'])} to your original payment method. "
        "Banks usually show it within 5 to 7 working days.",
        kind="payment.refunded", dedupe_key=f"email:{payload['event_id']}",
    )
