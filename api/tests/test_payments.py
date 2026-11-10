from datetime import timedelta

from app import db
from app.bookings import jobs as booking_jobs
from app.timeutil import utcnow
from tests.conftest import at, auth, book, pay, rows, webhook


def test_checkout_reuses_one_order(client, world, make_user):
    world.add_bike("CC-1")
    user = make_user()
    booking_id = book(client, user, world, at(1, 10), at(1, 12)).json()["id"]
    first = client.post(f"/api/v1/bookings/{booking_id}/checkout", headers=auth(user)).json()
    again = client.post(f"/api/v1/bookings/{booking_id}/checkout", headers=auth(user)).json()
    assert first["order_id"] == again["order_id"] and first["amount_paise"] == 8000 and first["fake"] is True
    assert rows("SELECT kind, status FROM payment") == [{"kind": "rental", "status": "pending"}]


def test_capture_confirms_the_booking(client, world, make_user, drain):
    world.add_bike("CC-1")
    user = make_user()
    booking_id = book(client, user, world, at(1, 10), at(1, 12)).json()["id"]
    pay(client, user, booking_id)
    detail = client.get(f"/api/v1/bookings/{booking_id}", headers=auth(user)).json()
    assert detail["status"] == "confirmed"
    assert [(p["kind"], p["status"]) for p in detail["payments"]] == [("rental", "succeeded")]
    emails = drain()
    assert [e["subject"].split(":")[0] for e in emails] == ["Booking confirmed"]


def test_bad_signature_is_refused(client, world, make_user):
    world.add_bike("CC-1")
    user = make_user()
    booking_id = book(client, user, world, at(1, 10), at(1, 12)).json()["id"]
    order_id = client.post(f"/api/v1/bookings/{booking_id}/checkout", headers=auth(user)).json()["order_id"]
    assert webhook(client, "payment.captured", order_id, signature="0" * 64).status_code == 400
    assert rows("SELECT status FROM payment")[0]["status"] == "pending"
    assert rows("SELECT count(*) AS n FROM payment_event")[0]["n"] == 0


def test_the_same_event_twice_counts_once(client, world, make_user, drain):
    world.add_bike("CC-1")
    user = make_user()
    booking_id = book(client, user, world, at(1, 10), at(1, 12)).json()["id"]
    order_id = client.post(f"/api/v1/bookings/{booking_id}/checkout", headers=auth(user)).json()["order_id"]
    for _ in range(3):
        assert webhook(client, "payment.captured", order_id, event_id="evt_same").status_code == 200
    # order.paid is a different event for the same payment: it must not count twice either
    assert webhook(client, "order.paid", order_id, event_id="evt_other").status_code == 200
    assert rows("SELECT count(*) AS n FROM payment WHERE status = 'succeeded'")[0]["n"] == 1
    assert rows("SELECT count(*) AS n FROM outbox_event WHERE type = 'booking.confirmed'")[0]["n"] == 1
    assert len([e for e in drain() if e["subject"].startswith("Booking confirmed")]) == 1


def test_failed_after_captured_changes_nothing(client, world, make_user):
    world.add_bike("CC-1")
    user = make_user()
    booking_id = book(client, user, world, at(1, 10), at(1, 12)).json()["id"]
    order_id = pay(client, user, booking_id)
    assert webhook(client, "payment.failed", order_id).status_code == 200  # arrives late, out of order
    assert rows("SELECT status FROM payment")[0]["status"] == "succeeded"
    assert rows("SELECT status FROM booking")[0]["status"] == "confirmed"


def test_payment_after_hold_expired_is_refunded(client, world, make_user, drain):
    world.add_bike("CC-1")
    user = make_user()
    booking_id = book(client, user, world, at(1, 10), at(1, 12)).json()["id"]
    order_id = client.post(f"/api/v1/bookings/{booking_id}/checkout", headers=auth(user)).json()["order_id"]
    with db.tx() as conn:
        assert booking_jobs.expire_holds(conn, utcnow() + timedelta(minutes=16)) == 1
    assert rows("SELECT status FROM payment")[0]["status"] == "cancelled"
    # the customer paid anyway, just after the hold ran out
    assert webhook(client, "payment.captured", order_id).status_code == 200
    assert rows("SELECT status FROM booking")[0]["status"] == "expired"
    assert rows("SELECT status FROM payment")[0]["status"] == "succeeded"
    assert rows("SELECT amount_paise, reason FROM refund") == [
        {"amount_paise": 8000, "reason": "payment arrived after the booking was expired"}]
    drain()
    assert rows("SELECT status FROM refund")[0]["status"] == "succeeded"


def test_checkout_refused_after_the_hold(client, world, make_user):
    world.add_bike("CC-1")
    user = make_user()
    booking_id = book(client, user, world, at(1, 10), at(1, 12)).json()["id"]
    with db.tx() as conn:
        conn.execute("UPDATE booking SET hold_expires_at = now() - interval '1 minute'")
    r = client.post(f"/api/v1/bookings/{booking_id}/checkout", headers=auth(user))
    assert r.status_code == 409 and r.json()["title"] == "The hold has run out"


def test_dues_are_paid_through_checkout(client, world, make_user):
    user = make_user()
    with db.tx() as conn:
        due = conn.execute(
            "INSERT INTO payment (user_id, kind, amount_paise, status, description) VALUES (%s, 'late_fee', 6000, 'due', 'Late') RETURNING id",
            (user["id"],),
        ).fetchone()["id"]
    assert [d["id"] for d in client.get("/api/v1/me/dues", headers=auth(user)).json()] == [due]
    order_id = client.post(f"/api/v1/payments/{due}/checkout", headers=auth(user)).json()["order_id"]
    webhook(client, "payment.captured", order_id)
    assert client.get("/api/v1/me/dues", headers=auth(user)).json() == []
    assert client.post(f"/api/v1/payments/{due}/checkout", headers=auth(user)).status_code == 409


def test_fake_pay_runs_the_webhook_path(client, world, make_user):
    world.add_bike("CC-1")
    user, other = make_user(), make_user()
    booking_id = book(client, user, world, at(1, 10), at(1, 12)).json()["id"]
    order_id = client.post(f"/api/v1/bookings/{booking_id}/checkout", headers=auth(user)).json()["order_id"]
    assert client.post(f"/api/v1/payments/fake/{order_id}/pay", headers=auth(other)).status_code == 404
    assert client.post(f"/api/v1/payments/fake/{order_id}/pay", headers=auth(user)).status_code == 200
    assert rows("SELECT status FROM booking")[0]["status"] == "confirmed"
    assert rows("SELECT count(*) AS n FROM payment_event")[0]["n"] == 1


def test_no_show_and_overdue_checks(client, world, make_user, drain):
    world.add_bike("CC-1")
    user = make_user()
    late_rider = book(client, user, world, at(1, 10), at(1, 12)).json()["id"]
    pay(client, user, late_rider)
    never_came = book(client, user, world, at(2, 10), at(2, 12)).json()["id"]
    pay(client, user, never_came)
    drain()
    with db.tx() as conn:
        conn.execute("UPDATE booking SET status = 'picked_up', picked_up_at = lower(during) WHERE id = %s", (late_rider,))
        assert booking_jobs.flag_overdue(conn, at(1, 12, 10)) == 0  # inside the 15-minute grace period
        assert booking_jobs.flag_overdue(conn, at(1, 12, 40)) == 1
        assert booking_jobs.flag_overdue(conn, at(1, 13, 40)) == 0  # flagged once
        assert booking_jobs.mark_no_shows(conn, at(2, 10, 30)) == 0
        assert booking_jobs.mark_no_shows(conn, at(2, 11, 5)) == 1
    statuses = {r["id"]: r["status"] for r in rows("SELECT id, status FROM booking")}
    assert statuses == {late_rider: "picked_up", never_came: "no_show"}
    subjects = [e["subject"].split(":")[0] for e in drain()]
    assert sorted(subjects) == ["Missed booking", "Your ride is overdue"]
    assert rows("SELECT count(*) AS n FROM refund")[0]["n"] == 0  # the no-show refund is 0% by default
