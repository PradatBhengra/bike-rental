import json
from datetime import datetime, timedelta

from app import cache
from app.catalog import pricing
from app.errors import Problem
from app.jsonutil import dumps
from app.timeutil import IST, fmt

STATION_COLS = """s.id, s.name, s.address, ST_Y(s.location::geometry) AS lat, ST_X(s.location::geometry) AS lng,
                  s.opens_at, s.closes_at, s.capacity, s.active"""
ACTIVE = "('pending_payment', 'confirmed', 'picked_up')"
STATIONS_KEY = "cache:stations"


def get_setting(conn, key: str):
    return conn.execute("SELECT value FROM setting WHERE key = %s", (key,)).fetchone()["value"]


def list_stations(conn, now: datetime, near: tuple[float, float] | None = None) -> list[dict]:
    """Active stations with the number of bikes free for the next hour, nearest first when a point is given."""
    if near is None:  # cache-aside: the plain list is the same for everyone for a minute
        cached = cache.r.get(STATIONS_KEY)
        if cached:
            return json.loads(cached)
    lat, lng = near or (None, None)
    rows = conn.execute(
        f"""SELECT {STATION_COLS},
                   (SELECT count(*) FROM bike k
                    WHERE k.home_station_id = s.id AND k.status = 'available'
                      AND NOT EXISTS (SELECT 1 FROM booking b WHERE b.bike_id = k.id AND b.status IN {ACTIVE}
                                      AND (b.during && tstzrange(%(now)s, %(soon)s) OR b.status = 'picked_up'))
                   )::int AS free_bikes,
                   CASE WHEN %(lat)s::float8 IS NULL THEN NULL
                        ELSE ST_Distance(s.location, ST_MakePoint(%(lng)s, %(lat)s)::geography)::int END AS distance_m
            FROM station s WHERE s.active
            ORDER BY CASE WHEN %(lat)s::float8 IS NULL THEN 0
                          ELSE s.location <-> ST_MakePoint(%(lng)s, %(lat)s)::geography END, s.name""",
        {"now": now, "soon": now + timedelta(hours=1), "lat": lat, "lng": lng},
    ).fetchall()
    text = dumps(rows)
    if near is None:
        cache.r.set(STATIONS_KEY, text, ex=60)
    return json.loads(text)  # the same plain types whether or not the cache answered


def get_station(conn, station_id: int) -> dict:
    row = conn.execute(f"SELECT {STATION_COLS} FROM station s WHERE s.id = %s", (station_id,)).fetchone()
    if row is None:
        raise Problem(404, "Station not found")
    return row


def list_bike_types(conn, now: datetime) -> list[dict]:
    return conn.execute(
        """SELECT t.id, t.slug, t.name, t.category, t.description, t.specs, p.hourly_paise, p.daily_cap_paise,
                  p.late_fee_per_hour_paise, d.slug AS manual_slug
           FROM bike_type t
           LEFT JOIN pricing_plan p ON p.bike_type_id = t.id AND p.valid_during @> %s::timestamptz
           LEFT JOIN document d ON d.id = t.manual_document_id
           WHERE t.active ORDER BY p.hourly_paise NULLS LAST, t.name""",
        (now,),
    ).fetchall()


def get_bike_type(conn, slug_or_id: str | int, now: datetime) -> dict:
    for row in list_bike_types(conn, now):
        if row["slug"] == slug_or_id or row["id"] == slug_or_id:
            return row
    raise Problem(404, "Bike type not found")


def plan_for(conn, bike_type_id: int, at: datetime) -> dict:
    plan = conn.execute(
        "SELECT * FROM pricing_plan WHERE bike_type_id = %s AND valid_during @> %s::timestamptz", (bike_type_id, at)
    ).fetchone()
    if plan is None:
        raise Problem(409, "No price is set for this bike type at that time")
    return plan


def price(conn, bike_type_id: int, start: datetime, end: datetime) -> dict:
    """The quote for a ride, priced on the plan in force when the ride starts."""
    modifiers = conn.execute(
        "SELECT * FROM price_modifier WHERE active AND (bike_type_id IS NULL OR bike_type_id = %s)", (bike_type_id,)
    ).fetchall()
    return pricing.quote(start, end, plan_for(conn, bike_type_id, start), modifiers)


def free_bikes(conn, station_id: int, bike_type_id: int, start: datetime, end: datetime, now: datetime) -> list[int]:
    """Bikes of this type at this station with no active booking in the window, and not out past their return time."""
    rows = conn.execute(
        f"""SELECT k.id FROM bike k
            WHERE k.home_station_id = %(station)s AND k.bike_type_id = %(type)s AND k.status = 'available'
              AND NOT EXISTS (SELECT 1 FROM booking b WHERE b.bike_id = k.id AND b.status IN {ACTIVE}
                              AND (b.during && tstzrange(%(start)s, %(end)s)
                                   OR (b.status = 'picked_up' AND upper(b.during) < %(now)s)))
            ORDER BY k.id""",
        {"station": station_id, "type": bike_type_id, "start": start, "end": end, "now": now},
    ).fetchall()
    return [r["id"] for r in rows]


def check_times(conn, station: dict, start: datetime, end: datetime, now: datetime) -> None:
    """Raises a 422 that says which rule the times break."""
    def bad(detail: str):
        raise Problem(422, "Invalid booking time", detail)

    if start >= end:
        bad("The end must be after the start.")
    if start < now - timedelta(minutes=5):
        bad("The start is in the past.")
    minutes = (end - start) / timedelta(minutes=1)
    if minutes < get_setting(conn, "min_booking_minutes"):
        bad(f"The shortest booking is {get_setting(conn, 'min_booking_minutes')} minutes.")
    if minutes > get_setting(conn, "max_booking_hours") * 60:
        bad(f"The longest booking is {get_setting(conn, 'max_booking_hours')} hours.")
    if start > now + timedelta(days=get_setting(conn, "max_advance_days")):
        bad(f"Bookings open {get_setting(conn, 'max_advance_days')} days ahead.")
    for label, moment in (("start", start), ("end", end)):
        local = moment.astimezone(IST).time()
        if not station["opens_at"] <= local <= station["closes_at"]:
            bad(f"The {label} ({fmt(moment)}) is outside {station['name']}'s opening hours, "
                f"{station['opens_at']:%H:%M} to {station['closes_at']:%H:%M}.")


def availability(conn, station_id: int, bike_type_id: int, start: datetime, end: datetime, now: datetime) -> dict:
    station = get_station(conn, station_id)
    check_times(conn, station, start, end, now)
    return {
        "free": len(free_bikes(conn, station_id, bike_type_id, start, end, now)),
        "quote": price(conn, bike_type_id, start, end),
    }


def invalidate_stations_job(conn, payload: dict) -> None:
    """Outbox consumer: a booking changed, so the cached free-bike counts are stale."""
    cache.r.delete(STATIONS_KEY)
