import pathlib
import secrets
from datetime import datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
import redis
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from app import cache, db, events, jobs
from app.auth.security import access_token, hash_password
from app.config import settings
from app.jsonutil import dumps
from app.payments import razorpay
from app.timeutil import IST, utcnow

MIGRATIONS = pathlib.Path(__file__).parents[2] / "db" / "migrations"
PASSWORD = "correct horse battery"
_state = {}


def apply_migrations(conninfo: str) -> None:
    """Runs the up half of every dbmate file, in name order."""
    with psycopg.connect(conninfo, autocommit=True) as conn:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(path.read_text(encoding="utf-8").split("-- migrate:down")[0])


@pytest.fixture(scope="session", autouse=True)
def stack():
    """One throwaway Postgres (PostGIS + pgvector) and Redis for the whole test run."""
    pg = PostgresContainer("bikerental-db:dev", username="bike", password="bike", dbname="bike", driver=None)
    rd = RedisContainer("redis:8.8-alpine")
    with pg, rd:
        url = pg.get_connection_url()
        apply_migrations(url)
        settings.fake_models = True
        settings.bcrypt_rounds = 4  # the cheapest bcrypt allows; real hashing would dominate the run time
        _state["password_hash"] = hash_password(PASSWORD)
        db.open_pool(url)
        settings.redis_url = f"redis://{rd.get_container_host_ip()}:{rd.get_exposed_port(6379)}/0"
        cache.r = redis.Redis(host=rd.get_container_host_ip(), port=int(rd.get_exposed_port(6379)), decode_responses=True)
        with db.tx() as conn:
            _state["settings"] = conn.execute("SELECT key, value FROM setting").fetchall()
        yield
        db.close_pool()


@pytest.fixture(autouse=True)
def clean(stack):
    with db.tx() as conn:
        tables = conn.execute(
            "SELECT string_agg(quote_ident(tablename), ', ') AS names FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename NOT IN ('spatial_ref_sys', 'schema_migrations')"
        ).fetchone()["names"]
        conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
        conn.execute("INSERT INTO corpus_state DEFAULT VALUES")
        for row in _state["settings"]:
            conn.execute("INSERT INTO setting (key, value) VALUES (%s, %s)", (row["key"], Jsonb(row["value"])))
    cache.r.flushdb()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_user():
    count = 0

    def make(role: str = "customer", station_id: int | None = None, email: str | None = None, **cols) -> dict:
        nonlocal count
        count += 1
        with db.tx() as conn:
            user = conn.execute(
                """INSERT INTO app_user (email, password_hash, full_name, role, staff_station_id, email_verified_at)
                   VALUES (%s, %s, %s, %s, %s, now()) RETURNING *""",
                (email or f"{role}{count}@example.com", _state["password_hash"], f"{role.title()} {count}", role, station_id),
            ).fetchone()
            for col, value in cols.items():
                conn.execute(f"UPDATE app_user SET {col} = %s WHERE id = %s", (value, user["id"]))
        return user

    return make


def auth(user: dict) -> dict:
    return {"Authorization": f"Bearer {access_token(user)}"}


def at(days: int, hour: int, minute: int = 0) -> datetime:
    """A Ranchi wall-clock time `days` from today, e.g. at(1, 10) is tomorrow 10:00."""
    day = utcnow().astimezone(IST).date() + timedelta(days=days)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


@pytest.fixture
def world():
    """One station open 06:00 to 22:00 and one bike type at Rs 40 an hour (cap Rs 250 a day, late fee Rs 60 an hour)."""
    with db.tx() as conn:
        station_id = conn.execute(
            """INSERT INTO station (name, address, location, opens_at, closes_at, capacity)
               VALUES ('Morabadi', 'Morabadi Ground, Ranchi', ST_MakePoint(85.3270, 23.3880)::geography, '06:00', '22:00', 10)
               RETURNING id"""
        ).fetchone()["id"]
        type_id = conn.execute(
            "INSERT INTO bike_type (slug, name, category) VALUES ('city-cruiser', 'City Cruiser', 'city') RETURNING id"
        ).fetchone()["id"]
        conn.execute(
            """INSERT INTO pricing_plan (bike_type_id, hourly_paise, daily_cap_paise, late_fee_per_hour_paise, valid_during)
               VALUES (%s, 4000, 25000, 6000, tstzrange('2020-01-01', NULL))""",
            (type_id,),
        )

    def add_bike(code: str) -> int:
        with db.tx() as conn:
            return conn.execute(
                "INSERT INTO bike (code, bike_type_id, home_station_id) VALUES (%s, %s, %s) RETURNING id",
                (code, type_id, station_id),
            ).fetchone()["id"]

    return SimpleNamespace(station_id=station_id, type_id=type_id, add_bike=add_bike)


def book(client, user: dict, world, start: datetime, end: datetime, key: str | None = None):
    return client.post(
        "/api/v1/bookings",
        json={"station_id": world.station_id, "bike_type_id": world.type_id, "start": start.isoformat(), "end": end.isoformat()},
        headers={**auth(user), "Idempotency-Key": key or secrets.token_hex(8)},
    )


def webhook(client, event: str, order_id: str, event_id: str | None = None, payment_id: str = "pay_test_1",
            signature: str | None = None):
    """Posts what Razorpay would send, signed with the webhook secret."""
    raw = dumps({"entity": "event", "event": event, "contains": ["payment"],
                 "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id, "status": "captured"}}}}).encode()
    return client.post(
        "/api/v1/webhooks/razorpay", content=raw,
        headers={"X-Razorpay-Signature": signature or razorpay.sign(raw), "x-razorpay-event-id": event_id or secrets.token_hex(8),
                 "Content-Type": "application/json"},
    )


def pay(client, user: dict, booking_id: int) -> str:
    """Checks out and delivers the capture webhook. Returns the order id."""
    order_id = client.post(f"/api/v1/bookings/{booking_id}/checkout", headers=auth(user)).json()["order_id"]
    assert webhook(client, "payment.captured", order_id, payment_id=f"pay_{order_id}").status_code == 200
    return order_id


@pytest.fixture
def drain():
    """Runs the outbox and the job queue until both are empty. Returns the emails that would have been sent."""
    from app import worker

    def run() -> list[dict]:
        sent = []
        handlers = {**worker.HANDLERS, "send_email": lambda conn, p: sent.append(p),
                    "make_receipt": lambda conn, p: None}
        for _ in range(500):
            if not events.dispatch_once() and not jobs.run_one(handlers, "test"):
                return sent
        raise AssertionError("the queue never emptied")

    return run


def rows(sql: str, params=()) -> list[dict]:
    with db.tx() as conn:
        return conn.execute(sql, params).fetchall()
