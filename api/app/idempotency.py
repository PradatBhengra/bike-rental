import hashlib
from collections.abc import Callable

from app.errors import Problem
from app.jsonutil import dumps


def remember(conn, user_id: int, endpoint: str, key: str, body: dict, run: Callable[[], tuple[int, dict]]) -> tuple[int, dict]:
    """Runs `run` once per (user, endpoint, key) and replays its response for a repeat of the same request.

    The key row and the work commit together. A second request with the same key waits on the unique index
    until the first commits, then finds the stored response. If the first fails, its row rolls back and the
    retry simply runs.
    """
    request_hash = hashlib.sha256(dumps(body).encode()).hexdigest()
    row = conn.execute(
        """INSERT INTO idempotency_key (key, user_id, endpoint, request_hash) VALUES (%s, %s, %s, %s)
           ON CONFLICT (user_id, endpoint, key) DO NOTHING RETURNING id""",
        (key, user_id, endpoint, request_hash),
    ).fetchone()
    if row is None:
        seen = conn.execute(
            "SELECT request_hash, status_code, response FROM idempotency_key WHERE user_id = %s AND endpoint = %s AND key = %s",
            (user_id, endpoint, key),
        ).fetchone()
        if seen["request_hash"] != request_hash:
            raise Problem(422, "Idempotency-Key reused", "This key was already used for a different request.")
        return seen["status_code"], seen["response"]
    status, response = run()
    conn.execute("UPDATE idempotency_key SET status_code = %s, response = %s WHERE id = %s", (status, response, row["id"]))
    return status, response
