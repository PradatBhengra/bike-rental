"""The worker process: dispatches outbox events, runs jobs, and schedules the every-minute checks.

Run with `python -m app.worker`. Several workers can run at once: SKIP LOCKED gives each job and each event
to one of them, and the dedupe key gives each scheduled minute to one of them.
"""

import logging
import os
import signal
import socket
import time
from datetime import datetime

from app import db, events, jobs, mail, receipts, rides
from app.analytics import rollups
from app.bookings import jobs as booking_jobs
from app.catalog import service as catalog
from app.docs import ingest
from app.payments import service as payments
from app.timeutil import IST, utcnow

log = logging.getLogger("app.worker")


def _cleanup(conn, payload: dict) -> None:
    conn.execute("DELETE FROM job WHERE status = 'done' AND finished_at < now() - interval '7 days'")
    conn.execute("DELETE FROM idempotency_key WHERE created_at < now() - interval '2 days'")
    conn.execute("DELETE FROM outbox_event WHERE dispatched_at < now() - interval '30 days'")
    conn.execute("DELETE FROM email_token WHERE expires_at < now() - interval '7 days'")
    conn.execute("DELETE FROM refresh_token WHERE expires_at < now() - interval '7 days'")


HANDLERS = {
    "send_email": mail.send_email_job,
    "booking_email": booking_jobs.booking_email_job,
    "refund_email": booking_jobs.refund_email_job,
    "invalidate_stations": catalog.invalidate_stations_job,
    "make_receipt": receipts.make_receipt_job,
    "request_refund": payments.request_refund_job,
    "expire_holds": lambda conn, p: booking_jobs.expire_holds(conn, utcnow()),
    "mark_no_shows": lambda conn, p: booking_jobs.mark_no_shows(conn, utcnow()),
    "flag_overdue": lambda conn, p: booking_jobs.flag_overdue(conn, utcnow()),
    "requeue_stuck": lambda conn, p: jobs.requeue_stuck(conn, utcnow()),
    "cleanup": _cleanup,
    "rollup": rollups.rollup_job,
    "refresh_demand": rollups.refresh_demand_job,
    "build_path": rides.build_path_job,
    "ensure_partitions": rides.ensure_partitions_job,
    "index_version": ingest.index_version_job,
}

EVERY_MINUTE = ["expire_holds", "mark_no_shows", "flag_overdue", "requeue_stuck"]
NIGHTLY = ["cleanup", "refresh_demand", "ensure_partitions"]  # run once a day, at 03:00 Ranchi time


def schedule(now: datetime) -> None:
    """Scheduled checks are jobs too. The dedupe key means two workers never run the same minute twice."""
    minute = now.replace(second=0, microsecond=0)
    with db.tx() as conn:
        for kind in EVERY_MINUTE:
            jobs.enqueue(conn, kind, {}, dedupe_key=f"{kind}:{minute.isoformat()}", max_attempts=1)
        local = now.astimezone(IST)
        if local.hour == 3:
            for kind in NIGHTLY:
                jobs.enqueue(conn, kind, {}, dedupe_key=f"{kind}:{local.date()}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.open_pool()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("worker %s started", worker_id)
    last_minute = None
    while running:
        now = utcnow()
        try:
            if now.minute != last_minute:
                schedule(now)
                last_minute = now.minute
            busy = events.dispatch_once() > 0
            busy = jobs.run_one(HANDLERS, worker_id) or busy
        except Exception:
            log.exception("worker loop error")
            busy = False
        if not busy:
            time.sleep(1)
    db.close_pool()
    log.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
