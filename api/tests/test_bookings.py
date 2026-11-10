import threading
import time
from datetime import timedelta

import pytest

from app import db
from app.bookings import service
from app.bookings.states import get_booking
from app.errors import Problem
from app.timeutil import utcnow
from tests.conftest import at, auth, book, pay, rows


def make_booking(user, world, start, end, now=None) -> dict:
    with db.tx() as conn:
        return service.create_booking(conn, user["id"], world.station_id, world.type_id, start, end, now or utcnow())


def confirm(booking_id: int) -> None:
    from app.bookings.states import transition

    with db.tx() as conn:
        transition(conn, booking_id, "confirmed")


def test_booking_holds_a_bike(client, world, make_user):
    world.add_bike("CC-1")
    user = make_user()
    r = book(client, user, world, at(1, 10), at(1, 12))
    assert r.status_code == 201
    b = r.json()
    assert b["status"] == "pending_payment" and b["quoted_paise"] == 8000 and b["bike_code"] == "CC-1"
    assert b["code"].startswith("BK-") and b["price_snapshot"]["hourly_paise"] == 4000
    assert rows("SELECT to_status FROM booking_status_history") == [{"to_status": "pending_payment"}]
    assert client.get("/api/v1/availability", params={
        "station_id": world.station_id, "bike_type_id": world.type_id,
        "start": at(1, 11).isoformat(), "end": at(1, 13).isoformat()}).json()["free"] == 0


def test_back_to_back_bookings_do_not_overlap(client, world, make_user):
    world.add_bike("CC-1")
    assert book(client, make_user(), world, at(1, 10), at(1, 12)).status_code == 201
    assert book(client, make_user(), world, at(1, 12), at(1, 14)).status_code == 201  # [10,12) and [12,14)
    r = book(client, make_user(), world, at(1, 11), at(1, 13))
    assert r.status_code == 409 and r.json()["code"] == "none_free"


def test_many_customers_race_for_the_last_bike(world, make_user):
    world.add_bike("CC-1")
    users = [make_user() for _ in range(8)]
    barrier = threading.Barrier(len(users))
    outcomes = []

    def attempt(user):
        barrier.wait()  # every thread starts its booking at the same moment
        try:
            make_booking(user, world, at(1, 10), at(1, 12))
            outcomes.append("booked")
        except Problem as p:
            outcomes.append(p.status)

    threads = [threading.Thread(target=attempt, args=(u,)) for u in users]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes, key=str) == [409] * 7 + ["booked"]
    assert rows("SELECT count(*) AS n FROM booking")[0]["n"] == 1


def test_the_constraint_decides_when_two_transactions_overlap(world, make_user):
    """The second customer cannot see the first one's uncommitted hold, so only the exclusion constraint stops them."""
    world.add_bike("CC-1")
    first, second = make_user(), make_user()
    result = {}

    def late_comer():
        try:
            make_booking(second, world, at(1, 11), at(1, 13))
            result["second"] = "booked"
        except Problem as p:
            result["second"] = p.status

    with db.tx() as conn:
        service.create_booking(conn, first["id"], world.station_id, world.type_id, at(1, 10), at(1, 12), utcnow())
        thread = threading.Thread(target=late_comer)
        thread.start()
        time.sleep(0.5)  # the second INSERT is now waiting on the first transaction
        assert thread.is_alive()
    thread.join()  # the first committed; the constraint refuses the second
    assert result == {"second": 409}


def test_same_idempotency_key_returns_the_same_booking(client, world, make_user):
    world.add_bike("CC-1"), world.add_bike("CC-2")
    user = make_user()
    first = book(client, user, world, at(1, 10), at(1, 12), key="click-0001")
    again = book(client, user, world, at(1, 10), at(1, 12), key="click-0001")
    assert first.status_code == again.status_code == 201 and first.json() == again.json()
    assert rows("SELECT count(*) AS n FROM booking")[0]["n"] == 1
    other_body = book(client, user, world, at(2, 10), at(2, 12), key="click-0001")
    assert other_body.status_code == 422
    no_key = client.post("/api/v1/bookings", headers=auth(user), json={
        "station_id": world.station_id, "bike_type_id": world.type_id, "start": at(1, 10).isoformat(), "end": at(1, 12).isoformat()})
    assert no_key.status_code == 422


@pytest.mark.parametrize("start, end, expect", [
    ("2030-01-05T10:00:00", "2030-01-05T12:00:00", "timezone"),  # no UTC offset
    (at(1, 12).isoformat(), at(1, 10).isoformat(), "after the start"),
    (at(1, 10).isoformat(), at(1, 10, 30).isoformat(), "shortest booking"),
    (at(1, 4).isoformat(), at(1, 8).isoformat(), "opening hours"),
    (at(1, 20).isoformat(), at(1, 23).isoformat(), "opening hours"),
    (at(-1, 10).isoformat(), at(-1, 12).isoformat(), "in the past"),
    (at(1, 10).isoformat(), at(5, 10).isoformat(), "longest booking"),
    (at(40, 10).isoformat(), at(40, 12).isoformat(), "days ahead"),
])
def test_booking_time_validation(client, world, make_user, start, end, expect):
    world.add_bike("CC-1")
    r = client.post("/api/v1/bookings", headers={**auth(make_user()), "Idempotency-Key": "k" * 12},
                    json={"station_id": world.station_id, "bike_type_id": world.type_id, "start": start, "end": end})
    assert r.status_code == 422 and expect in r.json()["detail"]
    assert rows("SELECT count(*) AS n FROM booking")[0]["n"] == 0


def test_dues_block_new_bookings(client, world, make_user):
    world.add_bike("CC-1")
    user = make_user()
    with db.tx() as conn:
        conn.execute("INSERT INTO payment (user_id, kind, amount_paise, status) VALUES (%s, 'late_fee', 6000, 'due')", (user["id"],))
    r = book(client, user, world, at(1, 10), at(1, 12))
    assert r.status_code == 409 and r.json()["code"] == "dues"


def test_unpaid_holds_are_limited(client, world, make_user):
    for n in range(3):
        world.add_bike(f"CC-{n}")
    user = make_user()
    assert book(client, user, world, at(1, 10), at(1, 12)).status_code == 201
    assert book(client, user, world, at(2, 10), at(2, 12)).status_code == 201
    assert book(client, user, world, at(3, 10), at(3, 12)).json()["code"] == "holds"


def test_another_customers_booking_is_not_found(client, world, make_user):
    world.add_bike("CC-1")
    owner, stranger = make_user(), make_user()
    booking_id = book(client, owner, world, at(1, 10), at(1, 12)).json()["id"]
    for method, path in [("get", ""), ("get", "/cancel-quote"), ("post", "/cancel"), ("post", "/checkout"), ("get", "/receipt.pdf")]:
        r = getattr(client, method)(f"/api/v1/bookings/{booking_id}{path}", headers=auth(stranger))
        missing = getattr(client, method)(f"/api/v1/bookings/999999{path}", headers=auth(stranger))
        assert r.status_code == missing.status_code == 404 and r.json() == missing.json()
    assert client.get("/api/v1/bookings", headers=auth(stranger)).json()["items"] == []


def test_list_pages_with_a_cursor(client, world, make_user):
    world.add_bike("CC-1")
    user = make_user()
    ids = []
    for day in range(1, 4):
        ids.append(make_booking(user, world, at(day, 10), at(day, 12))["id"])
        confirm(ids[-1])
    page1 = client.get("/api/v1/bookings", params={"limit": 2}, headers=auth(user)).json()
    assert [b["id"] for b in page1["items"]] == [ids[2], ids[1]] and page1["next_cursor"] == ids[1]
    page2 = client.get("/api/v1/bookings", params={"limit": 2, "cursor": page1["next_cursor"]}, headers=auth(user)).json()
    assert [b["id"] for b in page2["items"]] == [ids[0]] and page2["next_cursor"] is None


def test_cancel_refunds_by_notice_given(client, world, make_user, drain):
    world.add_bike("CC-1")
    user = make_user()
    far = book(client, user, world, at(3, 10), at(3, 12)).json()["id"]
    pay(client, user, far)
    q = client.get(f"/api/v1/bookings/{far}/cancel-quote", headers=auth(user)).json()
    assert (q["paid_paise"], q["refund_pct"], q["refund_paise"]) == (8000, 100, 8000) and "24 hours or more" in q["rule"]
    done = client.post(f"/api/v1/bookings/{far}/cancel", headers=auth(user))
    assert done.status_code == 200 and done.json()["refund_paise"] == 8000
    assert client.post(f"/api/v1/bookings/{far}/cancel", headers=auth(user)).status_code == 409  # already cancelled
    assert rows("SELECT amount_paise, status FROM refund") == [{"amount_paise": 8000, "status": "pending"}]
    subjects = [e["subject"] for e in drain()]
    assert rows("SELECT status FROM refund") == [{"status": "succeeded"}]
    assert any(s.startswith("Booking cancelled") for s in subjects) and any(s.startswith("Refund of ₹80") for s in subjects)


def test_cancel_close_to_the_start(world, make_user):
    world.add_bike("CC-1")
    user = make_user()
    booking = make_booking(user, world, at(2, 10), at(2, 12))
    confirm(booking["id"])
    with db.tx() as conn:
        conn.execute("INSERT INTO payment (user_id, booking_id, kind, amount_paise, status, paid_at) VALUES (%s, %s, 'rental', 8000, 'succeeded', now())",
                     (user["id"], booking["id"]))
        booking = get_booking(conn, booking["id"])
        half = service.cancel_quote(conn, booking, booking["start_at"] - timedelta(hours=5))
        none = service.cancel_quote(conn, booking, booking["start_at"] - timedelta(minutes=30))
    assert (half["refund_pct"], half["refund_paise"]) == (50, 4000) and "24 hours or more" not in half["rule"]
    assert (none["refund_pct"], none["refund_paise"]) == (0, 0) and "less than 2 hours" in none["rule"]


def test_extend_is_refused_by_the_next_booking(client, world, make_user):
    world.add_bike("CC-1")
    user, other = make_user(), make_user()
    mine = make_booking(user, world, at(1, 10), at(1, 12))["id"]
    confirm(mine)
    make_booking(other, world, at(1, 13), at(1, 15))
    ok = client.post(f"/api/v1/bookings/{mine}/extend", json={"end": at(1, 13).isoformat()}, headers=auth(user))
    assert ok.status_code == 200 and ok.json()["extra_paise"] == 4000 and ok.json()["booking"]["quoted_paise"] == 12000
    assert rows("SELECT kind, amount_paise, status FROM payment") == [{"kind": "extension", "amount_paise": 4000, "status": "due"}]
    blocked = client.post(f"/api/v1/bookings/{mine}/extend", json={"end": at(1, 14).isoformat()}, headers=auth(user))
    assert blocked.status_code == 409 and "next booking starts" in blocked.json()["detail"]
    assert client.get(f"/api/v1/bookings/{mine}", headers=auth(user)).json()["end_at"] == ok.json()["booking"]["end_at"]


def test_pickup_and_late_return(world, make_user):
    bike_id = world.add_bike("CC-1")
    user, staff = make_user(), make_user("staff", station_id=world.station_id)
    booking = make_booking(user, world, at(1, 10), at(1, 12))
    confirm(booking["id"])
    with db.tx() as conn:
        booking = get_booking(conn, booking["id"])
        with pytest.raises(Problem, match="Too early"):
            service.pickup(conn, booking, staff["id"], at(1, 9))
        out = service.pickup(conn, booking, staff["id"], at(1, 10))
        assert out["status"] == "picked_up" and out["picked_up_at"] == at(1, 10)
        result = service.return_bike(conn, out, staff["id"], at(1, 13, 20), note="scratched bell", odometer_km=412.5)
    assert (result["late_minutes"], result["late_fee_paise"]) == (80, 12000)  # two started hours late
    assert result["booking"]["status"] == "returned"
    assert rows("SELECT kind, amount_paise, status FROM payment") == [{"kind": "late_fee", "amount_paise": 12000, "status": "due"}]
    assert float(rows("SELECT odometer_km FROM bike WHERE id = %s", (bike_id,))[0]["odometer_km"]) == 412.5
    history = [h["to_status"] for h in rows("SELECT to_status FROM booking_status_history ORDER BY id")]
    assert history == ["pending_payment", "confirmed", "picked_up", "returned"]


def test_return_inside_the_grace_period_is_free(world, make_user):
    world.add_bike("CC-1")
    user = make_user()
    booking = make_booking(user, world, at(1, 10), at(1, 12))
    confirm(booking["id"])
    with db.tx() as conn:
        out = service.pickup(conn, get_booking(conn, booking["id"]), None, at(1, 10))
        result = service.return_bike(conn, out, None, at(1, 12, 15))
    assert (result["late_minutes"], result["late_fee_paise"]) == (0, 0)
    assert rows("SELECT count(*) AS n FROM payment")[0]["n"] == 0


def test_pickup_moves_the_customer_when_their_bike_is_still_out(world, make_user):
    world.add_bike("CC-1")
    first, second = make_user(), make_user()
    a = make_booking(first, world, at(1, 8), at(1, 10))
    confirm(a["id"])
    b = make_booking(second, world, at(1, 10), at(1, 12))
    confirm(b["id"])
    with db.tx() as conn:
        service.pickup(conn, get_booking(conn, a["id"]), None, at(1, 8))  # ...and never returned
    with db.tx() as conn, pytest.raises(Problem, match="not back yet"):
        service.pickup(conn, get_booking(conn, b["id"]), None, at(1, 10))
    world.add_bike("CC-2")
    with db.tx() as conn:
        moved = service.pickup(conn, get_booking(conn, b["id"]), None, at(1, 10))
    assert moved["bike_code"] == "CC-2" and moved["status"] == "picked_up"


def test_staff_desk_is_scoped_to_their_station(client, world, make_user):
    world.add_bike("CC-1")
    user = make_user()
    start = utcnow() + timedelta(minutes=5)
    with db.tx() as conn:  # a booking that starts in five minutes, placed directly: the API would check opening hours
        bike_id = conn.execute("SELECT id FROM bike").fetchone()["id"]
        code = conn.execute(
            """INSERT INTO booking (code, user_id, bike_id, station_id, during, status, price_snapshot, quoted_paise)
               VALUES ('BK-TEST22', %s, %s, %s, tstzrange(%s, %s), 'confirmed', %s, 8000) RETURNING code""",
            (user["id"], bike_id, world.station_id, start, start + timedelta(hours=2), {"late_fee_per_hour_paise": 6000}),
        ).fetchone()["code"]
        other_station = conn.execute(
            """INSERT INTO station (name, address, location, opens_at, closes_at, capacity)
               VALUES ('Doranda', 'Doranda', ST_MakePoint(85.32, 23.33)::geography, '06:00', '22:00', 5) RETURNING id"""
        ).fetchone()["id"]
    here, elsewhere = make_user("staff", station_id=world.station_id), make_user("staff", station_id=other_station)

    assert client.get("/api/v1/staff/desk", headers=auth(user)).status_code == 403
    assert client.post(f"/api/v1/staff/bookings/{code}/pickup", headers=auth(elsewhere)).status_code == 404
    desk = client.get("/api/v1/staff/desk", params={"date": start.astimezone().date().isoformat()}, headers=auth(here))
    assert desk.status_code == 200 and desk.json()["station_name"] == "Morabadi"
    picked = client.post(f"/api/v1/staff/bookings/{code.lower()}/pickup", headers=auth(here))
    assert picked.status_code == 200 and picked.json()["status"] == "picked_up"
    assert [b["code"] for b in client.get("/api/v1/staff/desk", headers=auth(here)).json()["out"]] == [code]
    returned = client.post(f"/api/v1/staff/bookings/{code}/return", json={"note": "all fine"}, headers=auth(here))
    assert returned.status_code == 200 and returned.json()["late_fee_paise"] == 0
    assert client.post(f"/api/v1/staff/bookings/{code}/return", json={}, headers=auth(here)).status_code == 409


def test_admin_override_records_who_and_why(client, world, make_user):
    world.add_bike("CC-1")
    admin, user = make_user("admin"), make_user()
    booking_id = make_booking(user, world, at(1, 10), at(1, 12))["id"]
    url = f"/api/v1/admin/bookings/{booking_id}/status"
    assert client.post(url, json={"status": "confirmed", "reason": "x"}, headers=auth(admin)).status_code == 422  # reason too short
    assert client.post(url, json={"status": "returned", "reason": "paid in cash"}, headers=auth(admin)).status_code == 409
    assert client.post(url, json={"status": "confirmed", "reason": "paid in cash"}, headers=auth(user)).status_code == 403
    ok = client.post(url, json={"status": "confirmed", "reason": "paid in cash"}, headers=auth(admin))
    assert ok.status_code == 200 and ok.json()["status"] == "confirmed"
    last = rows("SELECT actor_id, reason, to_status FROM booking_status_history ORDER BY id DESC LIMIT 1")[0]
    assert last == {"actor_id": admin["id"], "reason": "paid in cash", "to_status": "confirmed"}
    log = rows("SELECT actor_id, action, entity_id, reason, before, after FROM audit_log")[0]
    assert log == {"actor_id": admin["id"], "action": "booking.status", "entity_id": booking_id, "reason": "paid in cash",
                   "before": {"status": "pending_payment"}, "after": {"status": "confirmed"}}


def test_stations_nearest_first(client, world):
    with db.tx() as conn:
        conn.execute("""INSERT INTO station (name, address, location, opens_at, closes_at, capacity)
                        VALUES ('Doranda', 'Doranda', ST_MakePoint(85.3200, 23.3300)::geography, '06:00', '22:00', 5)""")
    world.add_bike("CC-1")
    plain = client.get("/api/v1/stations").json()
    assert [s["name"] for s in plain] == ["Doranda", "Morabadi"] and plain[1]["free_bikes"] == 1
    near = client.get("/api/v1/stations", params={"near": "23.3885,85.3275"}).json()
    assert [s["name"] for s in near] == ["Morabadi", "Doranda"] and near[0]["distance_m"] < 200 < near[1]["distance_m"]
    assert abs(near[0]["lat"] - 23.388) < 1e-6 and abs(near[0]["lng"] - 85.327) < 1e-6
    assert client.get("/api/v1/stations", params={"near": "ranchi"}).status_code == 422
