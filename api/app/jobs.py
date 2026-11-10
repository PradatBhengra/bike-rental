"""Postgres job queue: workers claim rows with FOR UPDATE SKIP LOCKED, so each job runs on one worker."""

import logging
import traceback
from datetime import UTC, datetime, timedelta

from app import db

log = logging.getLogger("app.jobs")
STUCK_AFTER = timedelta(minutes=5)


def enqueue(conn, kind: str, payload: dict, run_at: datetime | None = None, dedupe_key: str | None = None,
            max_attempts: int = 5) -> int | None:
    """Adds a job in the caller's transaction. A repeated dedupe_key is a no-op and returns None."""
    row = conn.execute(
        """INSERT INTO job (kind, payload, run_at, dedupe_key, max_attempts)
           VALUES (%s, %s, coalesce(%s, now()), %s, %s)
           ON CONFLICT (dedupe_key) DO NOTHING RETURNING id""",
        (kind, payload, run_at, dedupe_key, max_attempts),
    ).fetchone()
    return row["id"] if row else None


def claim(conn, worker_id: str, now: datetime) -> dict | None:
    return conn.execute(
        """UPDATE job SET status = 'running', locked_by = %(w)s, locked_at = %(now)s, attempts = attempts + 1
           WHERE id = (SELECT id FROM job WHERE status IN ('queued', 'failed') AND run_at <= %(now)s
                       ORDER BY run_at, id FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING *""",
        {"w": worker_id, "now": now},
    ).fetchone()


def run_one(handlers: dict, worker_id: str, now: datetime | None = None) -> bool:
    """Claims and runs one job. Returns False when nothing was ready."""
    now = now or datetime.now(UTC)
    with db.tx() as conn:
        job = claim(conn, worker_id, now)
    if job is None:
        return False
    try:
        with db.tx() as conn:  # the work and the 'done' mark commit together
            handlers[job["kind"]](conn, job["payload"])
            conn.execute("UPDATE job SET status = 'done', finished_at = now(), last_error = NULL WHERE id = %s",
                         (job["id"],))
    except Exception:
        error = traceback.format_exc(limit=5)
        log.warning("job %s (%s) failed on attempt %s", job["id"], job["kind"], job["attempts"])
        dead = job["attempts"] >= job["max_attempts"]
        delay = timedelta(seconds=10 * 2 ** job["attempts"])
        with db.tx() as conn:
            conn.execute(
                """UPDATE job SET status = %s, run_at = %s, last_error = %s, locked_by = NULL, locked_at = NULL,
                          finished_at = CASE WHEN %s THEN now() END
                   WHERE id = %s""",
                ("dead" if dead else "failed", now + delay, error, dead, job["id"]),
            )
    return True


def requeue_stuck(conn, now: datetime) -> int:
    """A job left 'running' by a crashed worker goes back to the queue."""
    return conn.execute(
        "UPDATE job SET status = 'queued', locked_by = NULL, locked_at = NULL WHERE status = 'running' AND locked_at < %s",
        (now - STUCK_AFTER,),
    ).rowcount
