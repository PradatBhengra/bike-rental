import secrets
from datetime import datetime, timedelta
from math import ceil

from psycopg import errors as pgerr

from app import events
from app.bookings import states
from app.bookings.states import get_booking, transition
from app.catalog import pricing
from app.catalog import service as catalog
from app.errors import Problem
from app.payments import service as payments
from app.timeutil import fmt, rupees

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I: the code is read out at the station desk


def _new_code() -> str:
    return "BK-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def create_booking(conn, user_id: int, station_id: int, bike_type_id: int, start: datetime, end: datetime,
                   now: datetime) -> dict:
    """Holds one free bike of the type for the customer, as a pending_payment booking."""
    if payments.has_dues(conn, user_id):
        raise Problem(409, "You have unpaid dues", "Pay them before booking again.", code="dues")
    holds = conn.execute(
        "SELECT count(*) AS n FROM booking WHERE user_id = %s AND status = 'pending_payment'", (user_id,)
    ).fetchone()["n"]
    if holds >= catalog.get_setting(conn, "max_unpaid_holds"):
        raise Problem(409, "Too many unpaid bookings", "Pay for or cancel a booking you already hold.", code="holds")
    station = catalog.get_station(conn, station_id)
    if not station["active"]:
        raise Problem(404, "Station not found")
    catalog.check_times(conn, station, start, end, now)
    snapshot = catalog.price(conn, bike_type_id, start, end)
    hold_expires_at = now + timedelta(minutes=catalog.get_setting(conn, "hold_minutes"))

    for bike_id in catalog.free_bikes(conn, station_id, bike_type_id, start, end, now):
        try:
            with conn.transaction():  # a savepoint: losing the race for this bike undoes only this insert
                row = conn.execute(
                    """INSERT INTO booking (code, user_id, bike_id, station_id, during, status, hold_expires_at,
                                            price_snapshot, quoted_paise, created_at)
                       VALUES (%s, %s, %s, %s, tstzrange(%s, %s), 'pending_payment', %s, %s, %s, %s) RETURNING id""",
                    (_new_code(), user_id, bike_id, station_id, start, end, hold_expires_at, snapshot,
                     snapshot["total_paise"], now),
                ).fetchone()
        except pgerr.ExclusionViolation:
            continue  # another customer took this bike a moment ago; the constraint said no, so try the next bike
        conn.execute(
            "INSERT INTO booking_status_history (booking_id, to_status, actor_id, at) VALUES (%s, 'pending_payment', %s, %s)",
            (row["id"], user_id, now),
        )
        return get_booking(conn, row["id"])
    raise Problem(409, "No bike free", "No bike of this type is free at this station for those times.", code="none_free")


def list_bookings(conn, user_id: int, status: str | None = None, cursor: int | None = None, limit: int = 20,
                  start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    """Newest first. `cursor` is the id of the last row of the previous page."""
    return conn.execute(
        states.BOOKING + """
        WHERE b.user_id = %(user)s
          AND (%(status)s::text IS NULL OR b.status::text = %(status)s)
          AND (%(cursor)s::bigint IS NULL OR b.id < %(cursor)s)
          AND (%(start)s::timestamptz IS NULL OR upper(b.during) >= %(start)s)
          AND (%(end)s::timestamptz IS NULL OR lower(b.during) < %(end)s)
        ORDER BY b.id DESC LIMIT %(limit)s""",
        {"user": user_id, "status": status, "cursor": cursor, "limit": limit, "start": start, "end": end},
    ).fetchall()


def cancel_quote(conn, booking: dict, now: datetime) -> dict:
    """What the customer gets back if they cancel now, and the rule that decides it."""
    if booking["status"] not in ("pending_payment", "confirmed"):
        raise Problem(409, "This booking can no longer be cancelled",
                      f"It is {booking['status'].replace('_', ' ')}.")
    tiers = catalog.get_setting(conn, "refund_tiers")
    pct, tier = pricing.refund_pct(tiers, booking["start_at"], now)
    paid = payments.paid_paise(conn, booking["id"])
    if tier["min_hours_before"] > 0:
        rule = f"Cancelled {tier['min_hours_before']} hours or more before the start: {pct}% refund."
    else:
        shortest = min(t["min_hours_before"] for t in tiers if t["min_hours_before"] > 0)
        rule = f"Cancelled less than {shortest} hours before the start: {'no' if pct == 0 else f'{pct}%'} refund."
    return {
        "booking_id": booking["id"], "code": booking["code"], "paid_paise": paid, "refund_pct": pct,
        "refund_paise": paid * pct // 100, "rule": rule,
        "hours_before_start": round((booking["start_at"] - now) / timedelta(hours=1), 1),
    }


def cancel_booking(conn, booking: dict, actor_id: int | None, now: datetime, reason: str | None = None,
                   refund_pct: int | None = None) -> dict:
    """Cancels and refunds by the cancellation policy. Staff overrides pass their own refund_pct."""
    quote = cancel_quote(conn, booking, now)
    pct = quote["refund_pct"] if refund_pct is None else refund_pct
    transition(conn, booking["id"], "cancelled", actor_id=actor_id, reason=reason, now=now)
    refunded = payments.refund_booking(conn, booking["id"], pct, reason or "cancelled by customer", now)
    return {**quote, "refund_pct": pct, "refund_paise": refunded}


def extend_quote(conn, booking: dict, new_end: datetime, now: datetime) -> dict:
    if booking["status"] not in ("confirmed", "picked_up"):
        raise Problem(409, "Only a confirmed or active booking can be extended")
    if now > booking["end_at"]:
        raise Problem(409, "The booked time has already ended", "A late return is charged as a late fee.")
    if new_end <= booking["end_at"]:
        raise Problem(422, "Invalid booking time", "The new end must be later than the current end.")
    catalog.check_times(conn, catalog.get_station(conn, booking["station_id"]), booking["start_at"], new_end,
                        booking["start_at"])
    old = booking["price_snapshot"]
    plan = {"id": old["plan_id"], **{k: old[k] for k in
                                     ("bike_type_id", "hourly_paise", "daily_cap_paise", "late_fee_per_hour_paise")}}
    modifiers = conn.execute(
        "SELECT * FROM price_modifier WHERE active AND (bike_type_id IS NULL OR bike_type_id = %s)",
        (old["bike_type_id"],),
    ).fetchall()
    snapshot = pricing.quote(booking["start_at"], new_end, plan, modifiers)  # the plan the customer was quoted
    return {"booking_id": booking["id"], "code": booking["code"], "new_end": new_end, "snapshot": snapshot,
            "extra_paise": max(snapshot["total_paise"] - booking["quoted_paise"], 0)}


def extend_booking(conn, booking: dict, new_end: datetime, now: datetime) -> dict:
    """Moves the end of the booked time. The exclusion constraint refuses if the bike's next booking starts sooner."""
    quote = extend_quote(conn, booking, new_end, now)
    try:
        with conn.transaction():
            conn.execute(
                """UPDATE booking SET during = tstzrange(lower(during), %s), price_snapshot = %s, quoted_paise = %s
                   WHERE id = %s""",
                (new_end, quote["snapshot"], max(quote["snapshot"]["total_paise"], booking["quoted_paise"]), booking["id"]),
            )
    except pgerr.ExclusionViolation:
        nxt = conn.execute(
            f"""SELECT min(lower(during)) AS at FROM booking
                WHERE bike_id = %s AND id <> %s AND status IN {catalog.ACTIVE} AND lower(during) >= %s""",
            (booking["bike_id"], booking["id"], booking["end_at"]),
        ).fetchone()["at"]
        raise Problem(409, "The bike is booked after you",
                      f"Its next booking starts {fmt(nxt)}, so the ride cannot run past that.", next_start=nxt) from None
    conn.execute(
        "INSERT INTO booking_status_history (booking_id, from_status, to_status, actor_id, reason, at) VALUES (%s, %s, %s, %s, %s, %s)",
        (booking["id"], booking["status"], booking["status"], booking["user_id"], f"extended to {fmt(new_end)}", now),
    )
    if quote["extra_paise"] > 0:
        payments.create_due(conn, booking["user_id"], booking["id"], "extension", quote["extra_paise"],
                            f"Extension of {booking['code']} to {fmt(new_end)}", now)
    events.emit(conn, "booking.extended", booking["id"], {"booking_id": booking["id"], "user_id": booking["user_id"],
                                                          "station_id": booking["station_id"], "at": now})
    return {"booking": get_booking(conn, booking["id"]), "extra_paise": quote["extra_paise"]}


def reassign_bike(conn, booking: dict, now: datetime) -> bool:
    """Moves a booking to another free bike of the same type. Used when its own bike is not back yet."""
    for bike_id in catalog.free_bikes(conn, booking["station_id"], booking["bike_type_id"], booking["start_at"],
                                      booking["end_at"], now):
        if bike_id == booking["bike_id"]:
            continue
        try:
            with conn.transaction():
                conn.execute("UPDATE booking SET bike_id = %s WHERE id = %s", (bike_id, booking["id"]))
            return True
        except pgerr.ExclusionViolation:
            continue
    return False


def pickup(conn, booking: dict, staff_id: int, now: datetime) -> dict:
    early = timedelta(minutes=catalog.get_setting(conn, "early_pickup_minutes"))
    if booking["status"] == "confirmed" and now < booking["start_at"] - early:
        raise Problem(409, "Too early", f"This booking starts {fmt(booking['start_at'])}.")
    still_out = conn.execute(
        "SELECT 1 FROM booking WHERE bike_id = %s AND status = 'picked_up' AND id <> %s", (booking["bike_id"], booking["id"])
    ).fetchone()
    if still_out and booking["status"] == "confirmed" and not reassign_bike(conn, booking, now):
        raise Problem(409, "The bike is not back yet", "No other bike of this type is free. Offer a refund.")
    transition(conn, booking["id"], "picked_up", actor_id=staff_id, now=now, picked_up_at=now)
    return get_booking(conn, booking["id"])


def return_bike(conn, booking: dict, staff_id: int, now: datetime, note: str | None = None,
                odometer_km: float | None = None) -> dict:
    """Checks a bike back in. A return past the grace period creates a late fee the customer owes."""
    minutes_late = max(ceil((now - booking["end_at"]) / timedelta(minutes=1)), 0)
    late_minutes = minutes_late if minutes_late > catalog.get_setting(conn, "late_grace_minutes") else 0
    transition(conn, booking["id"], "returned", actor_id=staff_id, now=now, returned_at=now,
               late_minutes=late_minutes, return_note=note)
    fee = pricing.late_fee(late_minutes, booking["price_snapshot"]["late_fee_per_hour_paise"])
    if fee > 0:
        payments.create_due(conn, booking["user_id"], booking["id"], "late_fee", fee,
                            f"Late return of {booking['code']}: {late_minutes} minutes, {rupees(fee)}", now)
    if odometer_km is not None:
        conn.execute("UPDATE bike SET odometer_km = %s WHERE id = %s AND odometer_km <= %s",
                     (odometer_km, booking["bike_id"], odometer_km))
    return {"booking": get_booking(conn, booking["id"]), "late_minutes": late_minutes, "late_fee_paise": fee}
