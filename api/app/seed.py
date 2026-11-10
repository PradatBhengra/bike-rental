"""Demo data: stations, fleet, prices, staff and months of fake ride history, made through the real service functions.

Run with `python -m app.seed` on an empty database. Every user is fake; passwords are printed at the end.
"""

import random
from datetime import datetime, time, timedelta

from app import db
from app.analytics import rollups
from app.auth.security import hash_password
from app.bookings import service as bookings
from app.bookings.states import get_booking, transition
from app.errors import Problem
from app.payments import service as payments
from app.timeutil import IST, utcnow

STATIONS = [  # name, address, lat, lng, opens, closes
    ("Morabadi Ground", "Morabadi Ground, east gate", 23.3975, 85.3290, "06:00", "21:00"),
    ("Ranchi Lake", "Bada Talab west bank", 23.3717, 85.3209, "06:00", "21:00"),
    ("Kanke Dam", "Kanke Dam park gate, Kanke Road", 23.4172, 85.3105, "06:00", "19:00"),
    ("Firayalal Chowk", "Main Road, near Albert Ekka Chowk", 23.3631, 85.3236, "07:00", "21:00"),
    ("Ranchi Junction", "Ranchi railway station, Station Road", 23.3512, 85.3334, "05:00", "23:00"),
    ("Lalpur", "Lalpur Chowk, Circular Road", 23.3780, 85.3330, "06:00", "21:00"),
    ("Harmu", "Harmu Housing Colony market", 23.3640, 85.3020, "06:00", "21:00"),
    ("Doranda", "Doranda, near the Hinoo turning", 23.3420, 85.3220, "06:00", "21:00"),
]
BIKE_TYPES = [
    {"slug": "city-cruiser", "name": "City Cruiser", "category": "city", "per_station": 4,
     "hourly_paise": 4000, "daily_cap_paise": 25000, "late_fee_per_hour_paise": 6000,
     "description": "Step-through town bike with a basket and one gear. Easy short trips.",
     "specs": {"gears": "single speed", "brakes": "rim", "weight_kg": 17, "rider_height_cm": "150-185"}},
    {"slug": "trail-hybrid", "name": "Trail Hybrid", "category": "hybrid", "per_station": 3,
     "hourly_paise": 6000, "daily_cap_paise": 35000, "late_fee_per_hour_paise": 8000,
     "description": "Light, fast 21-speed bike for city roads and paved lanes.",
     "specs": {"gears": "21 speed", "brakes": "mechanical disc", "weight_kg": 13, "rider_height_cm": "160-190"}},
    {"slug": "ridge-mtb", "name": "Ridge MTB", "category": "mountain", "per_station": 2,
     "hourly_paise": 8000, "daily_cap_paise": 45000, "late_fee_per_hour_paise": 10000,
     "description": "Front-suspension mountain bike for rough tracks around Kanke and Tagore Hill.",
     "specs": {"gears": "24 speed", "brakes": "hydraulic disc", "suspension": "100 mm fork", "weight_kg": 15}},
    {"slug": "volt-e", "name": "Volt E-Bike", "category": "ebike", "per_station": 2,
     "hourly_paise": 12000, "daily_cap_paise": 70000, "late_fee_per_hour_paise": 15000,
     "description": "Pedal-assist e-bike, up to 25 km/h, 30 to 60 km per charge.",
     "specs": {"motor": "250 W", "range_km": "30-60", "assist_levels": 5, "weight_kg": 24}},
]
NAMES = ("Aarav Oraon,Ananya Munda,Rohan Tirkey,Priya Kumari,Vikram Singh,Sneha Lakra,Arjun Mahto,Kavya Sinha,"
         "Rahul Ekka,Neha Kujur,Aditya Sahu,Pooja Toppo,Karan Gupta,Ritika Minz,Sahil Kerketta,Isha Verma,"
         "Manish Bara,Divya Horo,Nikhil Pandey,Shreya Xalxo,Amit Barla,Tanya Soren,Deepak Hembrom,Riya Mishra").split(",")
PASSWORDS = {"admin": "admin12345", "staff": "staff12345", "customer": "rider12345"}


def _user(conn, email: str, name: str, role: str, station_id: int | None = None, consent: bool = True) -> int:
    return conn.execute(
        """INSERT INTO app_user (email, password_hash, full_name, role, staff_station_id, tracking_consent, email_verified_at)
           VALUES (%s, %s, %s, %s, %s, %s, now()) RETURNING id""",
        (email, hash_password(PASSWORDS[role]), name, role, station_id, consent),
    ).fetchone()["id"]


def _pay(conn, payment: dict, at: datetime) -> None:
    """Pays the way Razorpay would: an order, then a captured webhook event."""
    order = payments.attach_order(conn, payment["id"], f"order_seed_{payment['id']}")
    body = {"event": "payment.captured", "payload": {"payment": {"entity": {"id": f"pay_seed_{payment['id']}", "order_id": order}}}}
    payments.handle_event(conn, f"evt_seed_{payment['id']}", "payment.captured", body, at)


def _ride(conn, rng, user_id: int, station: dict, type_id: int, day, demo: bool) -> None:
    opens, closes = station["opens_at"].hour, station["closes_at"].hour
    hours = rng.choice([1, 1, 2, 2, 2, 3, 4])
    hour = rng.randint(max(opens, 7), closes - hours)
    start = datetime.combine(day, time(hour, rng.choice([0, 0, 30])), IST)
    end = start + timedelta(hours=hours)
    booked_at = start - timedelta(hours=rng.randint(1, 60))
    try:
        b = bookings.create_booking(conn, user_id, station["id"], type_id, start, end, booked_at)
    except Problem:
        return  # no bike free, or unpaid dues: skip, as a real customer would
    roll = rng.random()
    if roll < 0.06 and not demo:
        transition(conn, b["id"], "expired", reason="hold ran out", now=booked_at + timedelta(minutes=16))
        payments.void_unpaid(conn, b["id"])
        return
    _pay(conn, payments.rental_payment(conn, b, booked_at), booked_at + timedelta(minutes=2))
    b = get_booking(conn, b["id"])
    if roll < 0.13:
        bookings.cancel_booking(conn, b, user_id, start - timedelta(hours=rng.choice([30, 8, 1])), "changed plans")
    elif roll < 0.17 and not demo:
        transition(conn, b["id"], "no_show", reason="not picked up", now=start + timedelta(minutes=61))
    else:
        out = bookings.pickup(conn, b, None, start + timedelta(minutes=rng.randint(-10, 10)))
        late = rng.choice([-25, -15, -5, 0, 5, 10]) if rng.random() < 0.9 else rng.randint(30, 100)
        result = bookings.return_bike(conn, out, None, end + timedelta(minutes=late))
        if result["late_fee_paise"] and (demo or rng.random() < 0.9):
            due = conn.execute("SELECT * FROM payment WHERE booking_id = %s AND status = 'due'", (b["id"],)).fetchone()
            _pay(conn, due, result["booking"]["returned_at"] + timedelta(hours=rng.randint(2, 30)))


def run(conn, days: int = 120, customers: int = 24, seed: int = 7) -> dict:
    rng = random.Random(seed)
    if conn.execute("SELECT 1 FROM station LIMIT 1").fetchone():
        raise SystemExit("The database already has stations; seed only an empty database.")
    stations = []
    for name, address, lat, lng, opens, closes in STATIONS:
        stations.append(conn.execute(
            """INSERT INTO station (name, address, location, opens_at, closes_at, capacity)
               VALUES (%s, %s, ST_MakePoint(%s, %s)::geography, %s, %s, 20)
               RETURNING id, name, opens_at, closes_at""",
            (name, address, lng, lat, opens, closes),
        ).fetchone())
    type_ids = []
    for t in BIKE_TYPES:
        type_id = conn.execute(
            "INSERT INTO bike_type (slug, name, category, description, specs) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (t["slug"], t["name"], t["category"], t["description"], t["specs"]),
        ).fetchone()["id"]
        type_ids.append(type_id)
        conn.execute(
            """INSERT INTO pricing_plan (bike_type_id, hourly_paise, daily_cap_paise, late_fee_per_hour_paise, valid_during)
               VALUES (%s, %s, %s, %s, tstzrange('2025-01-01', NULL))""",
            (type_id, t["hourly_paise"], t["daily_cap_paise"], t["late_fee_per_hour_paise"]),
        )
        for s in stations:
            for n in range(t["per_station"]):
                code = f"{s['name'][:3].upper()}{s['id']}-{t['slug'][0].upper()}{n + 1:02d}"  # the id keeps Ranchi Lake and Ranchi Junction apart
                conn.execute("INSERT INTO bike (code, bike_type_id, home_station_id) VALUES (%s, %s, %s)",
                             (code, type_id, s["id"]))
    conn.execute(
        """INSERT INTO price_modifier (name, pct, days_of_week, starts_at, ends_at) VALUES
           ('Weekend', 20, '{6,7}', '00:00', '23:59:59'), ('Weekday evening', 10, '{1,2,3,4,5}', '17:00', '20:00')"""
    )

    _user(conn, "admin@saalbikes.test", "Meera Admin", "admin")
    for s in stations:
        _user(conn, f"staff.{s['name'].lower().replace(' ', '-')}@saalbikes.test", f"{s['name']} Desk", "staff", s["id"])
    demo = _user(conn, "asha@example.com", "Asha Kumari", "customer")
    people = [demo] + [_user(conn, f"{n.split()[0].lower()}@example.com", n, "customer", consent=rng.random() < 0.7)
                       for n in NAMES[:customers]]

    today = utcnow().astimezone(IST).date()
    for back in range(days, 0, -1):
        day = today - timedelta(days=back)
        for _ in range(rng.randint(8, 16)):
            user = demo if rng.random() < 0.08 else rng.choice(people[1:])
            _ride(conn, rng, user, rng.choice(stations), rng.choice(type_ids), day, user == demo)

    # Asha's upcoming confirmed booking, for "If I cancel Friday's booking, how much do I get back?"
    friday = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
    start = datetime.combine(friday, time(9, 0), IST)
    b = bookings.create_booking(conn, demo, stations[0]["id"], type_ids[3], start, start + timedelta(hours=3), utcnow())
    _pay(conn, payments.rental_payment(conn, b, utcnow()), utcnow())

    # History is final: no emails for it, refunds already processed, no jobs waiting.
    conn.execute("UPDATE outbox_event SET dispatched_at = now()")
    conn.execute("UPDATE refund SET status = 'succeeded', processed_at = created_at, razorpay_refund_id = 'rfnd_seed_' || id")
    conn.execute("DELETE FROM job")
    _fake_paths(conn, rng)
    rollups.rebuild(conn)
    conn.execute("REFRESH MATERIALIZED VIEW station_hourly_demand")
    return {"stations": len(stations), "customers": len(people),
            "bookings": conn.execute("SELECT count(*) AS n FROM booking").fetchone()["n"]}


def _fake_paths(conn, rng) -> None:
    """A route summary for past rides of customers who allow tracking: out from the station and back."""
    rides = conn.execute(
        """SELECT b.id FROM booking b JOIN app_user u ON u.id = b.user_id
           WHERE b.status = 'returned' AND u.tracking_consent"""
    ).fetchall()
    for r in rides:
        km, bearing = rng.uniform(0.8, 6.0), rng.uniform(0, 360)
        conn.execute(
            """WITH s AS (SELECT st.location AS home, b.picked_up_at, b.returned_at FROM booking b
                          JOIN station st ON st.id = b.station_id WHERE b.id = %(id)s),
                    line AS (SELECT ST_MakeLine(ARRAY[home::geometry, ST_Project(home, %(m)s, radians(%(deg)s))::geometry,
                                                      ST_Project(home, %(m)s, radians(%(deg)s + 50))::geometry, home::geometry]) AS g,
                                    picked_up_at, returned_at FROM s)
               INSERT INTO ride_path (booking_id, path, replay_path, distance_m, point_count, gap_count, started_at, ended_at)
               SELECT %(id)s, g::geography, g, ST_Length(g::geography)::int, 4, 0, picked_up_at, returned_at FROM line""",
            {"id": r["id"], "m": km * 500, "deg": bearing},
        )


def main() -> None:
    db.open_pool()
    with db.tx() as conn:
        counts = run(conn)
    db.close_pool()
    print(f"Seeded {counts}. Logins: admin@saalbikes.test / {PASSWORDS['admin']}, "
          f"staff.morabadi-ground@saalbikes.test / {PASSWORDS['staff']}, asha@example.com / {PASSWORDS['customer']}")


if __name__ == "__main__":
    main()
