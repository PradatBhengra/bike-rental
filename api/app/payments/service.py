from datetime import datetime

from app import events
from app.bookings import states
from app.errors import Problem
from app.jobs import enqueue
from app.payments import razorpay
from app.timeutil import utcnow


def has_dues(conn, user_id: int) -> bool:
    return conn.execute("SELECT 1 FROM payment WHERE user_id = %s AND status = 'due' LIMIT 1", (user_id,)).fetchone() is not None


def list_payments(conn, user_id: int, status: str | None = None, booking_id: int | None = None) -> list[dict]:
    return conn.execute(
        """SELECT p.id, p.booking_id, b.code AS booking_code, p.kind, p.amount_paise, p.status, p.description,
                  p.paid_at, p.created_at,
                  coalesce((SELECT sum(r.amount_paise) FROM refund r
                            WHERE r.payment_id = p.id AND r.status <> 'failed'), 0)::int AS refunded_paise
           FROM payment p LEFT JOIN booking b ON b.id = p.booking_id
           WHERE p.user_id = %(user)s AND (%(status)s::text IS NULL OR p.status::text = %(status)s)
             AND (%(booking)s::bigint IS NULL OR p.booking_id = %(booking)s)
           ORDER BY p.id DESC""",
        {"user": user_id, "status": status, "booking": booking_id},
    ).fetchall()


def create_due(conn, user_id: int, booking_id: int, kind: str, amount_paise: int, description: str, now: datetime) -> dict:
    """An unpaid fee. The customer pays it themselves; the app never charges a card in the background."""
    return conn.execute(
        """INSERT INTO payment (user_id, booking_id, kind, amount_paise, status, description, created_at)
           VALUES (%s, %s, %s, %s, 'due', %s, %s) RETURNING *""",
        (user_id, booking_id, kind, amount_paise, description, now),
    ).fetchone()


def paid_paise(conn, booking_id: int) -> int:
    """What the customer has paid for a booking and not yet got back."""
    return conn.execute(
        """SELECT coalesce(sum(p.amount_paise - coalesce(
                     (SELECT sum(r.amount_paise) FROM refund r WHERE r.payment_id = p.id AND r.status <> 'failed'), 0)), 0)::int AS paid
           FROM payment p WHERE p.booking_id = %s AND p.status = 'succeeded'""",
        (booking_id,),
    ).fetchone()["paid"]


def rental_payment(conn, booking: dict, now: datetime) -> dict:
    """The one rental payment of a booking, created on the first checkout and reused after that."""
    if booking["status"] != "pending_payment":
        raise Problem(409, "This booking is not waiting for payment")
    if booking["hold_expires_at"] <= now:
        raise Problem(409, "The hold has run out", "Book again to get a new hold.")
    conn.execute(
        """INSERT INTO payment (user_id, booking_id, kind, amount_paise, status, description, created_at)
           VALUES (%s, %s, 'rental', %s, 'pending', %s, %s)
           ON CONFLICT (booking_id) WHERE kind = 'rental' DO NOTHING""",
        (booking["user_id"], booking["id"], booking["quoted_paise"], f"Rental {booking['code']}", now),
    )
    return conn.execute("SELECT * FROM payment WHERE booking_id = %s AND kind = 'rental'", (booking["id"],)).fetchone()


def get_payment(conn, payment_id: int, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM payment WHERE id = %s AND user_id = %s", (payment_id, user_id)).fetchone()
    if row is None:
        raise Problem(404, "Payment not found")
    return row


def attach_order(conn, payment_id: int, order_id: str) -> str:
    """Stores the order id unless a concurrent checkout already stored one; returns the one that won."""
    row = conn.execute(
        "UPDATE payment SET razorpay_order_id = %s WHERE id = %s AND razorpay_order_id IS NULL RETURNING razorpay_order_id",
        (order_id, payment_id),
    ).fetchone()
    if row is None:
        row = conn.execute("SELECT razorpay_order_id FROM payment WHERE id = %s", (payment_id,)).fetchone()
    return row["razorpay_order_id"]


def void_unpaid(conn, booking_id: int) -> None:
    """A booking that ended before the ride: nothing more is owed on it."""
    conn.execute(
        "UPDATE payment SET status = 'cancelled' WHERE booking_id = %s AND status IN ('pending', 'due', 'failed')",
        (booking_id,),
    )


def handle_event(conn, event_id: str, type: str, body: dict, now: datetime) -> bool:
    """Applies one Razorpay webhook event. Returns False for a repeat delivery, which changes nothing.

    Razorpay retries deliveries and does not promise an order, so the event id is stored first (a repeat hits the
    primary key) and each handler checks the current status instead of trusting the order of arrival.
    """
    first = conn.execute(
        "INSERT INTO payment_event (event_id, type, payload) VALUES (%s, %s, %s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
        (event_id, type, body),
    ).fetchone()
    if first is None:
        return False
    entities = body.get("payload", {})
    if type in ("payment.captured", "order.paid"):
        payment = entities["payment"]["entity"]
        _captured(conn, payment["order_id"], payment["id"], now)
    elif type == "payment.failed":
        # only a payment still waiting can fail; one that already succeeded stays succeeded
        conn.execute("UPDATE payment SET status = 'failed' WHERE razorpay_order_id = %s AND status = 'pending'",
                     (entities["payment"]["entity"]["order_id"],))
    elif type in ("refund.processed", "refund.failed"):
        refund = entities["refund"]["entity"]
        _refund_result(conn, refund["id"], (refund.get("notes") or {}).get("key"), type == "refund.processed", now)
    conn.execute("UPDATE payment_event SET processed_at = %s WHERE event_id = %s", (now, event_id))
    return True


def _captured(conn, order_id: str, razorpay_payment_id: str, now: datetime) -> None:
    payment = conn.execute(
        """UPDATE payment SET status = 'succeeded', paid_at = %s, razorpay_payment_id = %s
           WHERE razorpay_order_id = %s AND status <> 'succeeded' RETURNING *""",
        (now, razorpay_payment_id, order_id),
    ).fetchone()
    if payment is None:
        return  # not our order, or already recorded: payment.captured and order.paid both arrive for one payment
    events.emit(conn, "payment.succeeded", payment["id"], {
        "payment_id": payment["id"], "user_id": payment["user_id"], "booking_id": payment["booking_id"],
        "kind": payment["kind"], "amount_paise": payment["amount_paise"], "at": now,
    })
    if payment["kind"] != "rental":
        return
    booking = states.get_booking(conn, payment["booking_id"], for_update=True)
    if booking["status"] == "pending_payment":
        states.transition(conn, booking["id"], "confirmed", reason="payment received", now=now)
    else:
        # the money arrived after the hold ran out or the booking was cancelled: the bike may be gone, so it goes back
        refund(conn, payment, payment["amount_paise"], f"payment arrived after the booking was {booking['status']}", now)


def refund(conn, payment: dict, amount_paise: int, reason: str, now: datetime) -> int:
    """Records a refund and queues the request to Razorpay. The key makes a retry a no-op, so nobody is refunded twice."""
    if amount_paise <= 0:
        return 0
    key = f"refund-{payment['booking_id']}-{payment['id']}"
    row = conn.execute(
        """INSERT INTO refund (payment_id, amount_paise, reason, idempotency_key, created_at) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (idempotency_key) DO NOTHING RETURNING id""",
        (payment["id"], amount_paise, reason, key, now),
    ).fetchone()
    if row is None:
        return 0
    enqueue(conn, "request_refund", {"refund_id": row["id"]}, dedupe_key=f"request_refund:{row['id']}")
    return amount_paise


def refund_booking(conn, booking_id: int, pct: int, reason: str, now: datetime) -> int:
    """Refunds pct% of every paid payment on a booking and voids what is unpaid. Returns the paise refunded."""
    total = 0
    paid = conn.execute("SELECT * FROM payment WHERE booking_id = %s AND status = 'succeeded' ORDER BY id", (booking_id,))
    for payment in paid.fetchall():
        total += refund(conn, payment, payment["amount_paise"] * pct // 100, reason, now)
    void_unpaid(conn, booking_id)
    return total


def request_refund_job(conn, payload: dict) -> None:
    row = conn.execute(
        """SELECT r.*, p.razorpay_payment_id AS pay_id FROM refund r JOIN payment p ON p.id = r.payment_id
           WHERE r.id = %s FOR UPDATE OF r""",
        (payload["refund_id"],),
    ).fetchone()
    if row is None or row["status"] != "pending" or row["razorpay_refund_id"]:
        return
    # A retry after a crash must not refund twice: look for a refund carrying our key before asking for one.
    result = razorpay.find_refund(row["pay_id"], row["idempotency_key"]) or razorpay.create_refund(
        row["pay_id"], row["amount_paise"], row["idempotency_key"]
    )
    conn.execute("UPDATE refund SET razorpay_refund_id = %s WHERE id = %s", (result["id"], row["id"]))
    if result["status"] == "processed":  # otherwise the refund.processed webhook finishes it
        _refund_result(conn, result["id"], row["idempotency_key"], True, utcnow())


def _refund_result(conn, razorpay_refund_id: str, key: str | None, ok: bool, now: datetime) -> None:
    row = conn.execute(
        """UPDATE refund r SET status = %s, processed_at = %s, razorpay_refund_id = coalesce(r.razorpay_refund_id, %s)
           FROM payment p
           WHERE p.id = r.payment_id AND (r.razorpay_refund_id = %s OR r.idempotency_key = %s) AND r.status = 'pending'
           RETURNING r.id, r.payment_id, r.amount_paise, p.user_id, p.booking_id""",
        ("succeeded" if ok else "failed", now, razorpay_refund_id, razorpay_refund_id, key),
    ).fetchone()
    if row and ok:
        events.emit(conn, "payment.refunded", row["payment_id"], {
            "refund_id": row["id"], "payment_id": row["payment_id"], "user_id": row["user_id"],
            "booking_id": row["booking_id"], "amount_paise": row["amount_paise"], "at": now,
        })
