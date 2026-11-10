"""Replays fake rides for demos: starts a few rides, then sends GPS points for every active ride every few seconds.

Run with `python -m app.simulator --rides 8`. It talks to the tracking service over HTTP like a phone would.
"""

import argparse
import math
import random
import secrets
import time
from datetime import timedelta

import httpx

from app import db
from app.auth.security import access_token
from app.bookings.states import transition
from app.catalog import service as catalog
from app.timeutil import utcnow


def start_rides(n: int, rng: random.Random) -> None:
    """Puts n consenting customers on a bike right now, skipping opening hours: this is a demo tool."""
    now = utcnow()
    with db.tx() as conn:
        users = conn.execute(
            "SELECT id FROM app_user WHERE role = 'customer' AND tracking_consent AND blocked_at IS NULL"
        ).fetchall()
        stations = conn.execute("SELECT id FROM station WHERE active").fetchall()
        for user in rng.sample(users, min(n, len(users))):
            station = rng.choice(stations)["id"]
            for type_row in rng.sample(catalog.list_bike_types(conn, now), 4):
                end = now + timedelta(minutes=rng.choice([30, 45, 60, 90]))
                bikes = catalog.free_bikes(conn, station, type_row["id"], now, end, now)
                if bikes:
                    booking = conn.execute(
                        """INSERT INTO booking (code, user_id, bike_id, station_id, during, status, price_snapshot, quoted_paise)
                           VALUES (%s, %s, %s, %s, tstzrange(%s, %s), 'confirmed', %s, 0) RETURNING id""",
                        (f"SIM-{secrets.token_hex(3).upper()}", user["id"], bikes[0], station, now, end,
                         {"late_fee_per_hour_paise": type_row["late_fee_per_hour_paise"]}),
                    ).fetchone()
                    transition(conn, booking["id"], "picked_up", reason="simulator", now=now, picked_up_at=now)
                    break


def run(url: str, interval: float, rng: random.Random) -> None:
    angle: dict[int, float] = {}
    with httpx.Client(base_url=url, timeout=5) as client:
        while True:
            with db.tx() as conn:
                active = conn.execute(
                    """SELECT b.id, b.user_id, ST_Y(s.location::geometry) AS lat, ST_X(s.location::geometry) AS lng
                       FROM booking b JOIN station s ON s.id = b.station_id JOIN app_user u ON u.id = b.user_id
                       WHERE b.status = 'picked_up' AND u.tracking_consent"""
                ).fetchall()
            for ride in active:
                a = angle[ride["id"]] = angle.get(ride["id"], rng.uniform(0, 2 * math.pi)) + 0.03
                radius = 0.008 + 0.004 * math.sin(ride["id"])  # roughly 0.5 to 1.3 km: a loop around the station
                point = {"lat": ride["lat"] + radius * math.sin(a), "lng": ride["lng"] + radius * math.cos(a),
                         "recorded_at": utcnow().isoformat(), "accuracy_m": rng.uniform(4, 15)}
                token = access_token({"id": ride["user_id"], "role": "customer"})
                r = client.post(f"/track/{ride['id']}/points", json={"points": [point]},
                                headers={"Authorization": f"Bearer {token}"})
                if r.status_code >= 400:
                    print(f"ride {ride['id']}: {r.status_code} {r.text[:120]}")
            time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rides", type=int, default=0, help="start this many new rides first")
    parser.add_argument("--url", default="http://localhost:8001")
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args()
    rng = random.Random()
    db.open_pool()
    if args.rides:
        start_rides(args.rides, rng)
    run(args.url, args.interval, rng)


if __name__ == "__main__":
    main()
