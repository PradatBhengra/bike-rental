"""Transactional outbox: an event is a row written in the same transaction as the change it describes."""

import logging

from app import cache, db
from app.jobs import enqueue
from app.jsonutil import dumps

log = logging.getLogger("app.events")

# Event type -> the job kinds that consume it. Each consumer gets its own job, so one failing does not block the rest.
BOOKING_CHANGE = ["booking_email", "rollup", "invalidate_stations"]
SUBSCRIBERS: dict[str, list[str]] = {
    "booking.confirmed": BOOKING_CHANGE,
    "booking.expired": BOOKING_CHANGE,
    "booking.cancelled": BOOKING_CHANGE,
    "booking.no_show": BOOKING_CHANGE,
    "booking.picked_up": ["rollup", "invalidate_stations"],
    "booking.returned": ["make_receipt", "rollup", "invalidate_stations", "build_path"],
    "booking.late": ["booking_email"],
    "booking.extended": ["invalidate_stations"],
    "payment.succeeded": ["rollup"],
    "payment.refunded": ["rollup", "refund_email"],
}


def emit(conn, type: str, aggregate_id: int, payload: dict) -> None:
    conn.execute(
        "INSERT INTO outbox_event (type, aggregate_id, payload) VALUES (%s, %s, %s)", (type, aggregate_id, payload)
    )


def dispatch_once(limit: int = 100) -> int:
    """Turns undispatched events into one job per consumer, then tells live screens through Redis."""
    with db.tx() as conn:
        events = conn.execute(
            "SELECT * FROM outbox_event WHERE dispatched_at IS NULL ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s",
            (limit,),
        ).fetchall()
        for event in events:
            payload = {**event["payload"], "event_id": event["id"], "type": event["type"]}
            for consumer in SUBSCRIBERS.get(event["type"], []):
                # the key makes a second dispatch of the same event a no-op for this consumer
                enqueue(conn, consumer, payload, dedupe_key=f"{consumer}:{event['id']}")
        conn.execute(
            "UPDATE outbox_event SET dispatched_at = now() WHERE id = ANY(%s)", ([e["id"] for e in events],)
        )
    for event in events:  # after commit, and best effort: a screen that misses this refetches
        try:
            cache.r.publish("fleet", dumps({"type": event["type"], "id": event["aggregate_id"], **event["payload"]}))
        except Exception:
            log.warning("could not publish event %s", event["id"])
    return len(events)
