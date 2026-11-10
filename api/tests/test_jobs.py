import threading
from datetime import timedelta

import pytest

from app import db, events, jobs, worker
from app.timeutil import utcnow
from tests.conftest import rows


def test_rolled_back_change_dispatches_nothing():
    with pytest.raises(RuntimeError), db.tx() as conn:
        events.emit(conn, "booking.confirmed", 1, {"booking_id": 1})
        raise RuntimeError("the change failed after the event was written")
    assert events.dispatch_once() == 0
    assert rows("SELECT count(*) AS n FROM job")[0]["n"] == 0


def test_one_job_per_consumer_and_dispatching_twice_is_harmless():
    with db.tx() as conn:
        events.emit(conn, "booking.confirmed", 1, {"booking_id": 1})
    assert events.dispatch_once() == 1
    kinds = sorted(r["kind"] for r in rows("SELECT kind FROM job"))
    assert kinds == sorted(events.SUBSCRIBERS["booking.confirmed"])
    with db.tx() as conn:  # simulate a crash that left the event looking undispatched
        conn.execute("UPDATE outbox_event SET dispatched_at = NULL")
    assert events.dispatch_once() == 1
    assert rows("SELECT count(*) AS n FROM job")[0]["n"] == len(kinds)


def test_two_workers_never_run_the_same_job():
    with db.tx() as conn:
        for n in range(40):
            jobs.enqueue(conn, "count", {"n": n})
    ran, lock = [], threading.Lock()

    def count(conn, payload):
        with lock:
            ran.append(payload["n"])

    def work(worker_id):
        while jobs.run_one({"count": count}, worker_id):
            pass

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(ran) == list(range(40))
    assert rows("SELECT count(*) AS n FROM job WHERE status = 'done'")[0]["n"] == 40


def test_failing_job_retries_with_backoff_then_dies():
    with db.tx() as conn:
        job_id = jobs.enqueue(conn, "flaky", {}, max_attempts=3)

    def flaky(conn, payload):
        conn.execute("INSERT INTO outbox_event (type) VALUES ('half-done work')")  # must be rolled back
        raise ValueError("provider down")

    now = utcnow()
    assert jobs.run_one({"flaky": flaky}, "w", now)
    job = rows("SELECT * FROM job WHERE id = %s", (job_id,))[0]
    assert job["status"] == "failed" and job["attempts"] == 1 and job["run_at"] > now and "provider down" in job["last_error"]
    assert not jobs.run_one({"flaky": flaky}, "w", now)  # not due yet
    assert jobs.run_one({"flaky": flaky}, "w", now + timedelta(hours=1))
    assert jobs.run_one({"flaky": flaky}, "w", now + timedelta(hours=2))
    assert rows("SELECT status, attempts FROM job")[0] == {"status": "dead", "attempts": 3}
    assert rows("SELECT count(*) AS n FROM outbox_event")[0]["n"] == 0


def test_stuck_job_is_requeued():
    with db.tx() as conn:
        jobs.enqueue(conn, "slow", {}, run_at=utcnow() - timedelta(minutes=20))
        assert jobs.claim(conn, "crashed-worker", utcnow() - timedelta(minutes=10))
        assert jobs.requeue_stuck(conn, utcnow()) == 1
    assert rows("SELECT status, locked_by FROM job")[0] == {"status": "queued", "locked_by": None}


def test_a_scheduled_minute_runs_once():
    now = utcnow()
    worker.schedule(now)
    worker.schedule(now.replace(second=(now.second + 30) % 60))  # a second worker, same minute
    assert rows("SELECT count(*) AS n FROM job")[0]["n"] == len(worker.EVERY_MINUTE)
    worker.schedule(now + timedelta(minutes=1))
    assert rows("SELECT count(*) AS n FROM job")[0]["n"] == 2 * len(worker.EVERY_MINUTE)


def test_dead_jobs_show_up_for_the_admin(client, make_user):
    from tests.conftest import auth

    with db.tx() as conn:
        job_id = jobs.enqueue(conn, "gone", {}, max_attempts=1)
    jobs.run_one({"gone": lambda conn, p: 1 / 0}, "w")
    admin = make_user("admin")
    listed = client.get("/api/v1/admin/jobs", headers=auth(admin)).json()
    assert [j["id"] for j in listed["items"]] == [job_id] and listed["counts"] == {"dead": 1}
    assert client.post(f"/api/v1/admin/jobs/{job_id}/retry", headers=auth(admin)).status_code == 204
    assert rows("SELECT status, attempts FROM job")[0] == {"status": "queued", "attempts": 0}
