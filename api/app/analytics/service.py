"""Read-side queries for the analytics pages and the chat tools. Customer queries always filter by user_id."""

from datetime import date, datetime, timedelta

from app.timeutil import IST

LOCAL = "AT TIME ZONE 'Asia/Kolkata'"


def _period(start: date | None, end: date | None, today: date) -> tuple[date, date]:
    """An inclusive day range; defaults to the last 12 months."""
    end = end or today
    return start or (end.replace(day=1) - timedelta(days=335)).replace(day=1), end


def summary(conn, user_id: int, start: date | None, end: date | None, now: datetime) -> dict:
    today = now.astimezone(IST).date()
    start, end = _period(start, end, today)
    totals = conn.execute(
        """SELECT coalesce(sum(rides), 0)::int AS rides, coalesce(sum(ride_minutes), 0)::int AS ride_minutes,
                  coalesce(sum(distance_m), 0)::int AS distance_m,
                  coalesce(sum(spend_paise), 0)::int AS spend_paise, coalesce(sum(late_fee_paise), 0)::int AS late_fee_paise,
                  coalesce(sum(refund_paise), 0)::int AS refund_paise
           FROM user_daily_stats WHERE user_id = %s AND day BETWEEN %s AND %s""",
        (user_id, start, end),
    ).fetchone()
    month_start = today.replace(day=1)
    last_month_start = (month_start - timedelta(days=1)).replace(day=1)
    months = conn.execute(
        """SELECT coalesce(sum(spend_paise - refund_paise) FILTER (WHERE day >= %(this)s), 0)::int AS this_month,
                  coalesce(sum(spend_paise - refund_paise) FILTER (WHERE day >= %(last)s AND day < %(this)s), 0)::int AS last_month
           FROM user_daily_stats WHERE user_id = %(user)s AND day >= %(last)s""",
        {"user": user_id, "this": month_start, "last": last_month_start},
    ).fetchone()
    rides = conn.execute(
        f"""SELECT max(ceil(extract(epoch FROM b.returned_at - b.picked_up_at) / 60))::int AS longest_ride_minutes,
                   mode() WITHIN GROUP (ORDER BY s.name) AS favourite_station,
                   mode() WITHIN GROUP (ORDER BY t.name) AS favourite_bike_type
            FROM booking b JOIN station s ON s.id = b.station_id JOIN bike k ON k.id = b.bike_id
            JOIN bike_type t ON t.id = k.bike_type_id
            WHERE b.user_id = %s AND b.status = 'returned' AND (b.picked_up_at {LOCAL})::date BETWEEN %s AND %s""",
        (user_id, start, end),
    ).fetchone()
    return {
        **totals, **rides, "from": start, "to": end,
        "net_spend_paise": totals["spend_paise"] - totals["refund_paise"],
        "avg_ride_minutes": round(totals["ride_minutes"] / totals["rides"]) if totals["rides"] else 0,
        "this_month_paise": months["this_month"], "last_month_paise": months["last_month"],
    }


def spend(conn, user_id: int, group_by: str, start: date | None, end: date | None, now: datetime) -> list[dict]:
    """Net spend by month (from the rollup), or by bike type or station (from the payments themselves)."""
    start, end = _period(start, end, now.astimezone(IST).date())
    if group_by == "month":
        return conn.execute(
            """SELECT to_char(day, 'YYYY-MM') AS label, sum(spend_paise - refund_paise)::int AS amount_paise,
                      sum(rides)::int AS rides
               FROM user_daily_stats WHERE user_id = %s AND day BETWEEN %s AND %s GROUP BY 1 ORDER BY 1""",
            (user_id, start, end),
        ).fetchall()
    label = {"bike_type": "t.name", "station": "s.name"}[group_by]
    return conn.execute(
        f"""SELECT {label} AS label,
                   sum(p.amount_paise - coalesce((SELECT sum(r.amount_paise) FROM refund r
                                                  WHERE r.payment_id = p.id AND r.status = 'succeeded'), 0))::int AS amount_paise,
                   count(DISTINCT b.id) FILTER (WHERE b.status = 'returned')::int AS rides
            FROM payment p JOIN booking b ON b.id = p.booking_id JOIN station s ON s.id = b.station_id
            JOIN bike k ON k.id = b.bike_id JOIN bike_type t ON t.id = k.bike_type_id
            WHERE p.user_id = %s AND p.status = 'succeeded' AND (p.paid_at {LOCAL})::date BETWEEN %s AND %s
            GROUP BY 1 ORDER BY 2 DESC""",
        (user_id, start, end),
    ).fetchall()


def habits(conn, user_id: int, start: date | None, end: date | None, now: datetime) -> dict:
    start, end = _period(start, end, now.astimezone(IST).date())
    where = f"b.user_id = %s AND b.status = 'returned' AND (b.picked_up_at {LOCAL})::date BETWEEN %s AND %s"
    params = (user_id, start, end)
    by_weekday = conn.execute(
        f"""SELECT extract(isodow FROM b.picked_up_at {LOCAL})::int AS weekday, count(*)::int AS rides
            FROM booking b WHERE {where} GROUP BY 1 ORDER BY 1""", params).fetchall()
    by_hour = conn.execute(
        f"""SELECT extract(hour FROM b.picked_up_at {LOCAL})::int AS hour, count(*)::int AS rides
            FROM booking b WHERE {where} GROUP BY 1 ORDER BY 1""", params).fetchall()
    stations = conn.execute(
        f"""SELECT s.name, count(*)::int AS rides FROM booking b JOIN station s ON s.id = b.station_id
            WHERE {where} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 5""", params).fetchall()
    lead = conn.execute(
        f"""SELECT round(avg(extract(epoch FROM lower(b.during) - b.created_at) / 3600)::numeric, 1)::float AS hours
            FROM booking b WHERE {where}""", params).fetchone()["hours"]
    return {"from": start, "to": end, "by_weekday": by_weekday, "by_hour": by_hour, "stations": stations,
            "avg_booking_lead_hours": lead}


def fees_and_refunds(conn, user_id: int, start: date | None, end: date | None, now: datetime) -> dict:
    start, end = _period(start, end, now.astimezone(IST).date())
    fees = conn.execute(
        f"""SELECT p.id, p.kind, p.amount_paise, p.status, p.description, b.code AS booking_code, p.created_at
            FROM payment p LEFT JOIN booking b ON b.id = p.booking_id
            WHERE p.user_id = %s AND p.kind IN ('late_fee', 'extension', 'damage') AND p.status IN ('due', 'succeeded')
              AND (p.created_at {LOCAL})::date BETWEEN %s AND %s ORDER BY p.id DESC""",
        (user_id, start, end)).fetchall()
    refunds = conn.execute(
        f"""SELECT r.amount_paise, r.status, r.reason, b.code AS booking_code, r.created_at
            FROM refund r JOIN payment p ON p.id = r.payment_id LEFT JOIN booking b ON b.id = p.booking_id
            WHERE p.user_id = %s AND (r.created_at {LOCAL})::date BETWEEN %s AND %s ORDER BY r.id DESC""",
        (user_id, start, end)).fetchall()
    return {"from": start, "to": end, "fees": fees, "refunds": refunds,
            "unpaid_dues_paise": sum(f["amount_paise"] for f in fees if f["status"] == "due")}


def ride_history(conn, user_id: int, start: date | None, end: date | None, limit: int, now: datetime) -> list[dict]:
    start, end = _period(start, end, now.astimezone(IST).date())
    return conn.execute(
        f"""SELECT b.id, b.code, s.name AS station, t.name AS bike_type, b.picked_up_at, b.returned_at,
                   ceil(extract(epoch FROM b.returned_at - b.picked_up_at) / 60)::int AS minutes, b.late_minutes,
                   (SELECT coalesce(sum(p.amount_paise), 0) FROM payment p
                    WHERE p.booking_id = b.id AND p.status = 'succeeded')::int AS paid_paise
            FROM booking b JOIN station s ON s.id = b.station_id JOIN bike k ON k.id = b.bike_id
            JOIN bike_type t ON t.id = k.bike_type_id
            WHERE b.user_id = %s AND b.status = 'returned' AND (b.picked_up_at {LOCAL})::date BETWEEN %s AND %s
            ORDER BY b.picked_up_at DESC LIMIT %s""",
        (user_id, start, end, limit),
    ).fetchall()


# ---- admin reports

def admin_overview(conn, now: datetime) -> dict:
    today = now.astimezone(IST).date()
    return conn.execute(
        """SELECT (SELECT count(*) FROM booking WHERE status = 'picked_up')::int AS rides_now,
                  (SELECT count(*) FROM booking WHERE status = 'picked_up' AND overdue_notified_at IS NOT NULL)::int AS overdue_now,
                  (SELECT coalesce(sum(pickups), 0) FROM station_daily_stats WHERE day = %(today)s)::int AS pickups_today,
                  (SELECT coalesce(sum(revenue_paise), 0) FROM station_daily_stats WHERE day > %(today)s - 30)::int AS revenue_30d_paise,
                  (SELECT count(*) FROM bike WHERE status = 'maintenance')::int AS bikes_in_maintenance,
                  (SELECT count(*) FROM bike WHERE status <> 'retired')::int AS bikes,
                  (SELECT count(*) FROM job WHERE status = 'dead')::int AS dead_jobs""",
        {"today": today},
    ).fetchone()


def revenue_by_day(conn, days: int, now: datetime) -> list[dict]:
    today = now.astimezone(IST).date()
    return conn.execute(
        """SELECT d::date AS day, coalesce(sum(s.revenue_paise), 0)::int AS revenue_paise, coalesce(sum(s.pickups), 0)::int AS pickups
           FROM generate_series(%(from)s::date, %(today)s::date, interval '1 day') d
           LEFT JOIN station_daily_stats s ON s.day = d::date
           GROUP BY 1 ORDER BY 1""",
        {"from": today - timedelta(days=days - 1), "today": today},
    ).fetchall()


def station_report(conn, days: int, now: datetime) -> list[dict]:
    """Per station: revenue, utilisation (ridden minutes over bike-minutes open) and the problem rates."""
    today = now.astimezone(IST).date()
    return conn.execute(
        """WITH stats AS (
               SELECT station_id, sum(pickups) AS pickups, sum(revenue_paise) AS revenue, sum(cancellations) AS cancellations,
                      sum(no_shows) AS no_shows, sum(late_returns) AS late_returns
               FROM station_daily_stats WHERE day BETWEEN %(from)s AND %(today)s GROUP BY 1),
           usage AS (
               SELECT k.home_station_id AS station_id, sum(u.ridden_minutes) AS ridden
               FROM bike_daily_usage u JOIN bike k ON k.id = u.bike_id
               WHERE u.day BETWEEN %(from)s AND %(today)s GROUP BY 1),
           fleet AS (SELECT home_station_id AS station_id, count(*) AS bikes FROM bike WHERE status <> 'retired' GROUP BY 1)
           SELECT s.id AS station_id, s.name, coalesce(f.bikes, 0)::int AS bikes,
                  coalesce(st.pickups, 0)::int AS pickups, coalesce(st.revenue, 0)::int AS revenue_paise,
                  round(100.0 * coalesce(u.ridden, 0) / nullif(coalesce(f.bikes, 0)
                        * extract(epoch FROM s.closes_at - s.opens_at) / 60 * %(days)s, 0), 1)::float AS utilisation_pct,
                  round(100.0 * coalesce(st.no_shows, 0) / nullif(coalesce(st.pickups, 0) + coalesce(st.no_shows, 0), 0), 1)::float AS no_show_pct,
                  round(100.0 * coalesce(st.late_returns, 0) / nullif(st.pickups, 0), 1)::float AS late_return_pct,
                  coalesce(st.cancellations, 0)::int AS cancellations
           FROM station s LEFT JOIN stats st ON st.station_id = s.id LEFT JOIN usage u ON u.station_id = s.id
           LEFT JOIN fleet f ON f.station_id = s.id
           WHERE s.active ORDER BY revenue_paise DESC""",
        {"from": today - timedelta(days=days - 1), "today": today, "days": days},
    ).fetchall()


def peak_hours(conn, station_id: int | None) -> list[dict]:
    return conn.execute(
        """SELECT day_of_week, hour, sum(pickups)::int AS pickups FROM station_hourly_demand
           WHERE %(station)s::bigint IS NULL OR station_id = %(station)s GROUP BY 1, 2 ORDER BY 1, 2""",
        {"station": station_id},
    ).fetchall()
