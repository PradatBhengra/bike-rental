"""Rollups: per-day tables kept up to date by outbox events, and always rebuildable from the source tables.

A rollup job recomputes whole days from booking, payment and refund instead of adding increments, so running it
twice gives the same rows, and an event-fed table always equals a full rebuild (a test checks this).
Daily figures use the Asia/Kolkata calendar day.
"""

from datetime import date

KOLKATA = "AT TIME ZONE 'Asia/Kolkata')::date"

USER_DAILY = f"""
INSERT INTO user_daily_stats (user_id, day, rides, ride_minutes, distance_m, spend_paise, late_fee_paise, refund_paise)
SELECT user_id, day, sum(rides), sum(ride_minutes), sum(distance), sum(spend), sum(late), sum(refund) FROM (
    SELECT b.user_id, (b.picked_up_at {KOLKATA} AS day, 1 AS rides,
           ceil(extract(epoch FROM b.returned_at - b.picked_up_at) / 60)::int AS ride_minutes,
           coalesce(rp.distance_m, 0) AS distance, 0 AS spend, 0 AS late, 0 AS refund
    FROM booking b LEFT JOIN ride_path rp ON rp.booking_id = b.id WHERE b.status = 'returned'
    UNION ALL
    SELECT user_id, (paid_at {KOLKATA}, 0, 0, 0, amount_paise, CASE WHEN kind = 'late_fee' THEN amount_paise ELSE 0 END, 0
    FROM payment WHERE status = 'succeeded'
    UNION ALL
    SELECT p.user_id, (r.created_at {KOLKATA}, 0, 0, 0, 0, 0, r.amount_paise
    FROM refund r JOIN payment p ON p.id = r.payment_id WHERE r.status = 'succeeded'
) s WHERE %(all)s OR day = ANY(%(days)s)
GROUP BY user_id, day
"""

STATION_DAILY = f"""
INSERT INTO station_daily_stats (station_id, day, pickups, revenue_paise, cancellations, no_shows, late_returns)
SELECT station_id, day, sum(pickups), sum(revenue), sum(cancellations), sum(no_shows), sum(late_returns) FROM (
    SELECT station_id, (picked_up_at {KOLKATA} AS day, 1 AS pickups, 0 AS revenue, 0 AS cancellations, 0 AS no_shows,
           0 AS late_returns
    FROM booking WHERE picked_up_at IS NOT NULL
    UNION ALL
    SELECT b.station_id, (p.paid_at {KOLKATA}, 0, p.amount_paise, 0, 0, 0
    FROM payment p JOIN booking b ON b.id = p.booking_id WHERE p.status = 'succeeded'
    UNION ALL
    SELECT b.station_id, (r.created_at {KOLKATA}, 0, -r.amount_paise, 0, 0, 0
    FROM refund r JOIN payment p ON p.id = r.payment_id JOIN booking b ON b.id = p.booking_id WHERE r.status = 'succeeded'
    UNION ALL
    SELECT station_id, (lower(during) {KOLKATA}, 0, 0, (status = 'cancelled')::int, (status = 'no_show')::int, 0
    FROM booking WHERE status IN ('cancelled', 'no_show')
    UNION ALL
    SELECT station_id, (returned_at {KOLKATA}, 0, 0, 0, 0, 1
    FROM booking WHERE status = 'returned' AND late_minutes > 0
) s WHERE %(all)s OR day = ANY(%(days)s)
GROUP BY station_id, day
"""

BIKE_DAILY = f"""
INSERT INTO bike_daily_usage (bike_id, day, booked_minutes, ridden_minutes)
SELECT bike_id, day, sum(booked), sum(ridden) FROM (
    SELECT bike_id, (lower(during) {KOLKATA} AS day,
           ceil(extract(epoch FROM upper(during) - lower(during)) / 60)::int AS booked, 0 AS ridden
    FROM booking WHERE status IN ('confirmed', 'picked_up', 'returned')
    UNION ALL
    SELECT bike_id, (picked_up_at {KOLKATA}, 0, ceil(extract(epoch FROM returned_at - picked_up_at) / 60)::int
    FROM booking WHERE status = 'returned'
) s WHERE %(all)s OR day = ANY(%(days)s)
GROUP BY bike_id, day
"""

TABLES = {"user_daily_stats": USER_DAILY, "station_daily_stats": STATION_DAILY, "bike_daily_usage": BIKE_DAILY}


def rebuild(conn, days: list[date] | None = None) -> None:
    """Recomputes the given days, or everything when days is None."""
    # one rollup at a time: two jobs deleting and reinserting the same day would collide
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('rollups'))")
    params = {"all": days is None, "days": days or []}
    for table, insert in TABLES.items():
        if days is None:
            conn.execute(f"DELETE FROM {table}")
        else:
            conn.execute(f"DELETE FROM {table} WHERE day = ANY(%(days)s)", params)
        conn.execute(insert, params)


def affected_days(conn, booking_id: int | None, payment_id: int | None) -> list[date]:
    """Every day a booking or payment counts towards: its slot, pickup, return, payments and refunds."""
    rows = conn.execute(
        f"""SELECT DISTINCT day FROM (
                SELECT (v.at {KOLKATA} AS day FROM booking b,
                       LATERAL (VALUES (lower(b.during)), (b.picked_up_at), (b.returned_at)) v(at)
                WHERE b.id = %(booking)s
                UNION SELECT (paid_at {KOLKATA} FROM payment WHERE booking_id = %(booking)s OR id = %(payment)s
                UNION SELECT (r.created_at {KOLKATA} FROM refund r JOIN payment p ON p.id = r.payment_id
                      WHERE p.booking_id = %(booking)s OR p.id = %(payment)s
            ) s WHERE day IS NOT NULL""",
        {"booking": booking_id, "payment": payment_id},
    ).fetchall()
    return [r["day"] for r in rows]


def rollup_job(conn, payload: dict) -> None:
    """Outbox consumer for booking and payment events."""
    days = affected_days(conn, payload.get("booking_id"), payload.get("payment_id"))
    if days:
        rebuild(conn, days)


def refresh_demand_job(conn, payload: dict) -> None:
    """The materialized view is refreshed on a schedule. CONCURRENTLY keeps it readable during the refresh."""
    conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY station_hourly_demand")
