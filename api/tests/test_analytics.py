from datetime import timedelta

from app import db
from app.analytics import rollups
from app.bookings import service
from app.bookings.states import get_booking
from tests.conftest import at, auth, book, pay, rows

TABLES = ["user_daily_stats", "station_daily_stats", "bike_daily_usage"]


def snapshot() -> dict:
    return {t: rows(f"SELECT * FROM {t} ORDER BY 1, 2") for t in TABLES}


def ride(client, user, world, days_ahead: int, late_minutes: int = 0) -> int:
    """Books, pays, picks up and returns: the whole life of one ride."""
    booking_id = book(client, user, world, at(days_ahead, 10), at(days_ahead, 12)).json()["id"]
    pay(client, user, booking_id)
    with db.tx() as conn:
        out = service.pickup(conn, get_booking(conn, booking_id), None, at(days_ahead, 10))
        service.return_bike(conn, out, None, at(days_ahead, 12) + timedelta(minutes=late_minutes))
    return booking_id


def test_event_fed_rollups_equal_a_full_rebuild(client, world, make_user, drain):
    world.add_bike("CC-1"), world.add_bike("CC-2")
    asha, ravi = make_user(), make_user()
    ride(client, asha, world, 1)
    ride(client, asha, world, 2, late_minutes=50)
    ride(client, ravi, world, 3)
    cancelled = book(client, ravi, world, at(4, 10), at(4, 12)).json()["id"]
    pay(client, ravi, cancelled)
    client.post(f"/api/v1/bookings/{cancelled}/cancel", headers=auth(ravi))
    drain()

    from_events = snapshot()
    assert from_events["user_daily_stats"]  # the events did fill the tables
    with db.tx() as conn:
        rollups.rebuild(conn)
    assert snapshot() == from_events
    drain()
    assert snapshot() == from_events  # running the jobs again changes nothing


def test_customer_analytics(client, world, make_user, drain):
    world.add_bike("CC-1")
    asha = make_user()
    ride(client, asha, world, 1)
    ride(client, asha, world, 2, late_minutes=50)
    drain()
    with db.tx() as conn:  # pay the late fee so it counts as spend
        due = conn.execute("SELECT id FROM payment WHERE status = 'due'").fetchone()["id"]
        conn.execute("UPDATE payment SET status = 'succeeded', paid_at = %s WHERE id = %s", (at(2, 13), due))
        rollups.rebuild(conn)

    s = client.get("/api/v1/me/analytics/summary", headers=auth(asha), params={"end": at(30, 0).date().isoformat()}).json()
    assert (s["rides"], s["ride_minutes"], s["avg_ride_minutes"]) == (2, 120 + 170, 145)
    assert (s["spend_paise"], s["late_fee_paise"], s["refund_paise"]) == (8000 + 8000 + 6000, 6000, 0)
    assert s["longest_ride_minutes"] == 170 and s["favourite_station"] == "Morabadi"

    by_type = client.get("/api/v1/me/analytics/spend", headers=auth(asha),
                         params={"group_by": "bike_type", "end": at(30, 0).date().isoformat()}).json()
    assert by_type == [{"label": "City Cruiser", "amount_paise": 22000, "rides": 2}]
    habits = client.get("/api/v1/me/analytics/habits", headers=auth(asha), params={"end": at(30, 0).date().isoformat()}).json()
    assert habits["by_hour"] == [{"hour": 10, "rides": 2}]
    other = make_user()
    assert client.get("/api/v1/me/analytics/summary", headers=auth(other)).json()["rides"] == 0


def test_admin_reports(client, world, make_user):
    from app.bookings.states import transition

    world.add_bike("CC-1")
    user = make_user()
    with db.tx() as conn:  # a ride two days ago, booked the day before: the services take the clock as an argument
        b = service.create_booking(conn, user["id"], world.station_id, world.type_id, at(-2, 10), at(-2, 12), at(-3, 9))
        transition(conn, b["id"], "confirmed", now=at(-3, 9))
        out = service.pickup(conn, get_booking(conn, b["id"]), None, at(-2, 10))
        service.return_bike(conn, out, None, at(-2, 12, 40))
        rollups.rebuild(conn)
        rollups.refresh_demand_job(conn, {})
    admin = make_user("admin")
    assert client.get("/api/v1/admin/analytics/overview", headers=auth(make_user())).status_code == 403
    stations = client.get("/api/v1/admin/analytics/stations", headers=auth(admin), params={"days": 30}).json()
    assert stations[0]["name"] == "Morabadi" and stations[0]["pickups"] == 1
    assert stations[0]["late_return_pct"] == 100.0 and stations[0]["no_show_pct"] == 0.0
    assert stations[0]["utilisation_pct"] == round(100 * 160 / (16 * 60 * 30), 1)  # 160 ridden minutes, one bike open 16 h a day
    peak = client.get("/api/v1/admin/analytics/peak-hours", headers=auth(admin)).json()
    assert peak == [{"day_of_week": at(-2, 10).isoweekday(), "hour": 10, "pickups": 1}]
    revenue = client.get("/api/v1/admin/analytics/revenue", headers=auth(admin), params={"days": 7}).json()
    assert len(revenue) == 7 and sum(d["pickups"] for d in revenue) == 1
    overview = client.get("/api/v1/admin/analytics/overview", headers=auth(admin)).json()
    assert overview["bikes"] == 1 and overview["rides_now"] == 0
