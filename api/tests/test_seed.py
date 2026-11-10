from app import db, seed
from tests.conftest import rows


def test_seed_runs_on_an_empty_database(client):
    with db.tx() as conn:
        counts = seed.run(conn, days=3, customers=4)
    assert counts["stations"] == len(seed.STATIONS) and counts["bookings"] > 5
    assert rows("SELECT count(*) AS n FROM bike")[0]["n"] == len(seed.STATIONS) * sum(t["per_station"] for t in seed.BIKE_TYPES)
    assert rows("SELECT count(*) AS n FROM app_user WHERE role = 'staff'")[0]["n"] == len(seed.STATIONS)
    assert rows("SELECT count(*) AS n FROM user_daily_stats")[0]["n"] > 0
    for email, role in (("admin@saalbikes.test", "admin"), ("staff.ranchi-lake@saalbikes.test", "staff"), ("asha@example.com", "customer")):
        r = client.post("/api/v1/auth/login", json={"email": email, "password": seed.PASSWORDS[role]})
        assert r.status_code == 200 and r.json()["user"]["role"] == role
    assert rows("SELECT count(*) AS n FROM job")[0]["n"] == 0  # history sends no emails
