"""The tracking service: takes GPS batches from riders' phones and pushes live positions over WebSockets.

A separate process (`uvicorn app.tracking:app --port 8001`) because it gets a steady stream of small writes and
holds long-lived connections: a slow chat answer in the api must not delay a GPS write. It owns ride_point.
"""

import asyncio
import json
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from pydantic import AwareDatetime, BaseModel, Field

from app import cache, db, errors, rides
from app.config import settings
from app.deps import CurrentUser
from app.errors import Problem
from app.jsonutil import dumps
from app.timeutil import utcnow

MAX_ACCURACY_M = 50  # a fix worse than this is noise
MAX_SPEED_MPS = 15  # 54 km/h: faster than any of our bikes, so a jump this big is a bad fix

state = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    opened_here = db.pool is None
    if opened_here:
        db.open_pool()
    state["redis"] = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    yield
    await state["redis"].aclose()
    if opened_here:
        db.close_pool()


app = FastAPI(title="Saal Bikes tracking", lifespan=lifespan, openapi_url=None)
errors.install(app)


class Point(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    recorded_at: AwareDatetime
    accuracy_m: float | None = Field(None, ge=0)
    speed_mps: float | None = Field(None, ge=0)


class Batch(BaseModel):
    points: list[Point] = Field(min_length=1, max_length=500)


def metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine distance between two (lat, lng) points."""
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def keep(points: list[Point], last: tuple[float, float, datetime] | None, start: datetime, now: datetime) -> list[Point]:
    """Drops inaccurate fixes, points outside the ride, and jumps faster than a bike can go."""
    kept = []
    for p in sorted(points, key=lambda p: p.recorded_at):
        if (p.accuracy_m or 0) > MAX_ACCURACY_M or not start - timedelta(minutes=1) <= p.recorded_at <= now + timedelta(minutes=1):
            continue
        if last:
            seconds = (p.recorded_at - last[2]).total_seconds()
            if seconds <= 0 or metres((last[0], last[1]), (p.lat, p.lng)) / seconds > MAX_SPEED_MPS:
                continue
        kept.append(p)
        last = (p.lat, p.lng, p.recorded_at)
    return kept


@app.post("/track/{booking_id}/points")
def add_points(booking_id: int, batch: Batch, user: CurrentUser):
    now = utcnow()
    with db.tx() as conn:
        booking = conn.execute(
            """SELECT b.status, b.picked_up_at, u.tracking_consent FROM booking b JOIN app_user u ON u.id = b.user_id
               WHERE b.id = %s AND b.user_id = %s""",
            (booking_id, user["id"]),
        ).fetchone()
        if booking is None:
            raise Problem(404, "Booking not found")
        if booking["status"] != "picked_up":
            raise Problem(409, "This ride is not active")
        if not booking["tracking_consent"]:
            raise Problem(403, "Tracking is off", "Turn on tracking consent in your account first.")
        last = conn.execute(
            """SELECT ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lng, recorded_at FROM ride_point
               WHERE booking_id = %s ORDER BY recorded_at DESC LIMIT 1""",
            (booking_id,),
        ).fetchone()
        kept = keep(batch.points, (last["lat"], last["lng"], last["recorded_at"]) if last else None,
                    booking["picked_up_at"], now)
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO ride_point (booking_id, recorded_at, location, accuracy_m, speed_mps)
                   VALUES (%s, %s, ST_MakePoint(%s, %s)::geography, %s, %s)
                   ON CONFLICT (booking_id, recorded_at) DO NOTHING""",
                [(booking_id, p.recorded_at, p.lng, p.lat, p.accuracy_m, p.speed_mps) for p in kept],
            )
    if kept:
        p = kept[-1]
        message = dumps({"type": "point", "booking_id": booking_id, "lat": p.lat, "lng": p.lng, "at": p.recorded_at})
        cache.r.publish(f"ride:{booking_id}", message)
        cache.r.publish("fleet", message)
    return {"accepted": len(kept), "rejected": len(batch.points) - len(kept)}


def _allowed(who: dict, channel: str) -> bool:
    try:
        with db.tx() as conn:
            return rides.can_watch(conn, who, channel)
    except Problem:
        return False


@app.websocket("/ws")
async def live(socket: WebSocket, ticket: str):
    """Send {"subscribe": "fleet"} or {"subscribe": "ride:123"}; positions and booking changes arrive as JSON."""
    redis = state["redis"]
    raw = await redis.getdel(f"ws:{ticket}")  # get and delete in one step: a ticket works once
    if raw is None:
        await socket.close(code=4401)
        return
    who = json.loads(raw)
    await socket.accept()
    pubsub, pump = redis.pubsub(), None

    async def forward():
        async for message in pubsub.listen():
            if message["type"] == "message":
                await socket.send_text(message["data"])

    try:
        while True:
            channel = str((await socket.receive_json()).get("subscribe", ""))
            if not await run_in_threadpool(_allowed, who, channel):
                await socket.send_json({"type": "error", "detail": f"not allowed: {channel}"})
                continue
            await pubsub.subscribe(channel)
            await socket.send_json({"type": "subscribed", "channel": channel})
            if pump is None:  # listen() ends when nothing is subscribed, so start it after the first subscribe
                pump = asyncio.create_task(forward())
    except WebSocketDisconnect:
        pass
    finally:
        if pump:
            pump.cancel()
        await pubsub.aclose()
