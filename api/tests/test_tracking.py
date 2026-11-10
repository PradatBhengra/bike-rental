from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import db, tracking
from app.bookings import service
from app.bookings.states import get_booking, transition
from app.timeutil import utcnow
from tests.conftest import auth, rows


@pytest.fixture
def ride(world, make_user):
    """A ride picked up ten minutes ago by a customer who allows tracking."""
    world.add_bike("CC-1")
    user = make_user(tracking_consent=True)
    now = utcnow()
    with db.tx() as conn:  # placed directly: the API would refuse a start outside opening hours at night
        booking_id = conn.execute(
            """INSERT INTO booking (code, user_id, bike_id, station_id, during, status, price_snapshot, quoted_paise)
               VALUES ('BK-RIDE22', %s, (SELECT id FROM bike), %s, tstzrange(%s, %s), 'confirmed', '{"late_fee_per_hour_paise": 6000}', 0) RETURNING id""",
            (user["id"], world.station_id, now - timedelta(minutes=10), now + timedelta(hours=1)),
        ).fetchone()["id"]
        transition(conn, booking_id, "picked_up", now=now - timedelta(minutes=10), picked_up_at=now - timedelta(minutes=10))
    return {"id": booking_id, "user": user}


@pytest.fixture
def tracker():
    with TestClient(tracking.app) as c:
        yield c


def points(start, n, step_m=50, every_s=10, **extra):
    """n points heading north from Morabadi, step_m metres apart."""
    return [{"lat": 23.3880 + i * step_m / 111_000, "lng": 85.3270, "recorded_at": (start + timedelta(seconds=i * every_s)).isoformat(),
             "accuracy_m": 8, **extra} for i in range(n)]


def test_points_are_filtered_and_idempotent(tracker, ride):
    start = utcnow() - timedelta(minutes=5)
    batch = points(start, 5)
    batch.append({**batch[-1], "recorded_at": (start + timedelta(seconds=60)).isoformat(), "lat": 23.5})  # a 12 km jump
    batch.append({**points(start, 1)[0], "recorded_at": (start + timedelta(seconds=70)).isoformat(), "accuracy_m": 400})
    url, headers = f"/track/{ride['id']}/points", auth(ride["user"])
    assert tracker.post(url, json={"points": batch}, headers=headers).json() == {"accepted": 5, "rejected": 2}
    assert tracker.post(url, json={"points": batch[:5]}, headers=headers).json()["accepted"] == 0  # a retried batch
    assert rows("SELECT count(*) AS n FROM ride_point")[0]["n"] == 5


def test_only_the_rider_with_consent_can_send(tracker, ride, make_user):
    batch = {"points": points(utcnow() - timedelta(minutes=1), 1)}
    assert tracker.post(f"/track/{ride['id']}/points", json=batch, headers=auth(make_user())).status_code == 404
    with db.tx() as conn:
        conn.execute("UPDATE app_user SET tracking_consent = false WHERE id = %s", (ride["user"]["id"],))
    assert tracker.post(f"/track/{ride['id']}/points", json=batch, headers=auth(ride["user"])).status_code == 403


def test_return_builds_the_path_and_distance(client, tracker, ride, drain):
    tracker.post(f"/track/{ride['id']}/points", json={"points": points(utcnow() - timedelta(minutes=5), 11)},
                 headers=auth(ride["user"]))
    live = client.get(f"/api/v1/bookings/{ride['id']}/path", headers=auth(ride["user"])).json()
    assert live["live"] is True and len(live["coordinates"]) == 11 and 480 < live["distance_m"] < 520
    with db.tx() as conn:
        service.return_bike(conn, get_booking(conn, ride["id"]), None, utcnow())
    drain()
    path = rows("SELECT distance_m, point_count, gap_count FROM ride_path")[0]
    assert 480 < path["distance_m"] < 520 and path["point_count"] == 11 and path["gap_count"] == 0
    assert 480 < rows("SELECT distance_m FROM user_daily_stats")[0]["distance_m"] < 520
    done = client.get(f"/api/v1/bookings/{ride['id']}/path", headers=auth(ride["user"])).json()
    assert done["live"] is False and len(done["coordinates"]) == 2  # a straight line simplifies to its ends
    assert client.delete("/api/v1/me/ride-paths", headers=auth(ride["user"])).status_code == 204
    assert rows("SELECT count(*) AS n FROM ride_path")[0]["n"] == 0


def test_live_positions_over_websocket(client, tracker, ride, make_user):
    admin = make_user("admin")
    ticket = client.post("/api/v1/ws-ticket", headers=auth(admin)).json()["ticket"]
    with tracker.websocket_connect(f"/ws?ticket={ticket}") as ws:
        ws.send_json({"subscribe": "fleet"})
        assert ws.receive_json() == {"type": "subscribed", "channel": "fleet"}
        tracker.post(f"/track/{ride['id']}/points", json={"points": points(utcnow() - timedelta(seconds=30), 1)},
                     headers=auth(ride["user"]))
        message = ws.receive_json()
        assert message["type"] == "point" and message["booking_id"] == ride["id"]
    with pytest.raises(WebSocketDisconnect), tracker.websocket_connect(f"/ws?ticket={ticket}") as ws:  # a ticket works once
        ws.receive_json()


def test_who_may_watch(client, tracker, ride, make_user):
    stranger = make_user()
    ticket = client.post("/api/v1/ws-ticket", headers=auth(stranger)).json()["ticket"]
    with tracker.websocket_connect(f"/ws?ticket={ticket}") as ws:
        ws.send_json({"subscribe": f"ride:{ride['id']}"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"subscribe": "fleet"})
        assert ws.receive_json()["type"] == "error"


def test_fleet_snapshot(client, tracker, ride, make_user):
    tracker.post(f"/track/{ride['id']}/points", json={"points": points(utcnow() - timedelta(minutes=1), 2)},
                 headers=auth(ride["user"]))
    snap = client.get("/api/v1/admin/fleet/live", headers=auth(make_user("admin"))).json()
    assert [r["code"] for r in snap["rides"]] == ["BK-RIDE22"] and snap["rides"][0]["lat"] > 23.388
    assert client.get("/api/v1/admin/fleet/live", headers=auth(make_user())).status_code == 403


def test_keep_unit():
    now = utcnow()
    p = tracking.Point(lat=23.4, lng=85.3, recorded_at=now)
    assert tracking.keep([p], None, now - timedelta(minutes=5), now) == [p]
    assert tracking.keep([p], None, now + timedelta(minutes=5), now) == []  # before the pickup
    assert round(tracking.metres((23.0, 85.0), (23.001, 85.0))) == 111
