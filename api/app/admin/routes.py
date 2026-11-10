"""Admin screens: fleet, pricing, settings, bookings, users, background jobs. Every change is written to audit_log."""

from datetime import datetime, time
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, Field

from app import audit, db
from app.auth.routes import UserOut
from app.bookings import service as bookings
from app.bookings import states
from app.bookings.routes import BookingOut, Status, present
from app.catalog import service as catalog
from app.catalog.routes import StationOut
from app.deps import Admin, client_ip, require_role
from app.errors import Problem
from app.jsonutil import dumps
from app.timeutil import utcnow

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])


def _plain(row: dict | None) -> dict | None:
    import json

    return json.loads(dumps(row)) if row else None


def _create(conn, table: str, values: dict, admin: dict, request: Request) -> dict:
    cols = ", ".join(values)
    marks = ", ".join(f"%({c})s" for c in values)
    row = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks}) RETURNING *", values).fetchone()
    audit.record(conn, admin["id"], f"{table}.create", table, row["id"], after=_plain(row), ip=client_ip(request))
    return row


def _update(conn, table: str, row_id: int, changes: dict, admin: dict, request: Request, reason: str | None = None) -> dict:
    """Table and column names come from this file and the Pydantic models, never from the request."""
    before = conn.execute(f"SELECT * FROM {table} WHERE id = %s FOR UPDATE", (row_id,)).fetchone()
    if before is None:
        raise Problem(404, "Not found")
    if not changes:
        return before
    sets = ", ".join(f"{c} = %({c})s" for c in changes)
    after = conn.execute(f"UPDATE {table} SET {sets} WHERE id = %(id)s RETURNING *", {**changes, "id": row_id}).fetchone()
    audit.record(conn, admin["id"], f"{table}.update", table, row_id, before=_plain(before), after=_plain(after),
                 reason=reason, ip=client_ip(request))
    return after


# ---- stations

class StationIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    address: str = Field(min_length=2, max_length=200)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    opens_at: time
    closes_at: time
    capacity: int = Field(gt=0, le=500)
    active: bool = True


@router.get("/stations", response_model=list[StationOut])
def stations():
    with db.tx() as conn:
        return conn.execute(f"SELECT {catalog.STATION_COLS} FROM station s ORDER BY s.name").fetchall()


def _save_station(conn, station_id: int | None, body: StationIn, admin: dict, request: Request) -> dict:
    values = body.model_dump(exclude={"lat", "lng"})
    if station_id is None:
        # a geography point is written longitude first: ST_MakePoint(lng, lat)
        station_id = conn.execute(
            """INSERT INTO station (name, address, location, opens_at, closes_at, capacity, active)
               VALUES (%(name)s, %(address)s, ST_MakePoint(%(lng)s, %(lat)s)::geography, %(opens_at)s, %(closes_at)s,
                       %(capacity)s, %(active)s) RETURNING id""",
            body.model_dump(),
        ).fetchone()["id"]
        audit.record(conn, admin["id"], "station.create", "station", station_id, after=_plain(body.model_dump()),
                     ip=client_ip(request))
    else:
        _update(conn, "station", station_id, values, admin, request)
        conn.execute("UPDATE station SET location = ST_MakePoint(%s, %s)::geography WHERE id = %s",
                     (body.lng, body.lat, station_id))
    catalog.invalidate_stations_job(conn, {})
    return catalog.get_station(conn, station_id)


@router.post("/stations", status_code=201, response_model=StationOut)
def create_station(body: StationIn, admin: Admin, request: Request):
    with db.tx() as conn:
        return _save_station(conn, None, body, admin, request)


@router.put("/stations/{station_id}", response_model=StationOut)
def update_station(station_id: int, body: StationIn, admin: Admin, request: Request):
    with db.tx() as conn:
        return _save_station(conn, station_id, body, admin, request)


# ---- bike types and bikes

class BikeTypeIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9-]{2,40}$")
    name: str = Field(min_length=2, max_length=60)
    category: Literal["city", "hybrid", "mountain", "ebike"]
    description: str = ""
    specs: dict[str, Any] = {}
    active: bool = True


class BikeIn(BaseModel):
    code: str = Field(pattern=r"^[A-Z0-9-]{2,20}$")
    bike_type_id: int
    home_station_id: int
    status: Literal["available", "maintenance", "retired"] = "available"


class BikePatch(BaseModel):
    home_station_id: int | None = None
    status: Literal["available", "maintenance", "retired"] | None = None


@router.get("/bike-types")
def bike_types():
    with db.tx() as conn:
        return conn.execute(
            """SELECT t.*, (SELECT count(*) FROM bike k WHERE k.bike_type_id = t.id AND k.status <> 'retired')::int AS bikes
               FROM bike_type t ORDER BY t.name"""
        ).fetchall()


@router.post("/bike-types", status_code=201)
def create_bike_type(body: BikeTypeIn, admin: Admin, request: Request):
    with db.tx() as conn:
        return _create(conn, "bike_type", body.model_dump(), admin, request)


@router.put("/bike-types/{type_id}")
def update_bike_type(type_id: int, body: BikeTypeIn, admin: Admin, request: Request):
    with db.tx() as conn:
        return _update(conn, "bike_type", type_id, body.model_dump(), admin, request)


@router.get("/bikes")
def bikes(station_id: int | None = None, status: str | None = None):
    with db.tx() as conn:
        return conn.execute(
            """SELECT k.id, k.code, k.status, k.odometer_km, k.bike_type_id, t.name AS bike_type_name,
                      k.home_station_id, s.name AS station_name,
                      EXISTS (SELECT 1 FROM booking b WHERE b.bike_id = k.id AND b.status = 'picked_up') AS on_ride
               FROM bike k JOIN bike_type t ON t.id = k.bike_type_id JOIN station s ON s.id = k.home_station_id
               WHERE (%(station)s::bigint IS NULL OR k.home_station_id = %(station)s)
                 AND (%(status)s::text IS NULL OR k.status::text = %(status)s)
               ORDER BY s.name, t.name, k.code""",
            {"station": station_id, "status": status},
        ).fetchall()


@router.post("/bikes", status_code=201)
def create_bike(body: BikeIn, admin: Admin, request: Request):
    with db.tx() as conn:
        return _create(conn, "bike", body.model_dump(), admin, request)


@router.patch("/bikes/{bike_id}")
def update_bike(bike_id: int, body: BikePatch, admin: Admin, request: Request):
    with db.tx() as conn:
        row = _update(conn, "bike", bike_id, body.model_dump(exclude_unset=True), admin, request)
        catalog.invalidate_stations_job(conn, {})
        return row


# ---- pricing

class PlanIn(BaseModel):
    bike_type_id: int
    hourly_paise: int = Field(gt=0)
    daily_cap_paise: int = Field(gt=0)
    late_fee_per_hour_paise: int = Field(ge=0)
    starts_at: AwareDatetime


class ModifierIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    pct: int = Field(ge=-90, le=300)
    days_of_week: list[int] = Field(min_length=1)  # ISO: 1 = Monday ... 7 = Sunday
    starts_at: time
    ends_at: time
    bike_type_id: int | None = None
    active: bool = True


PLAN_COLS = """p.id, p.bike_type_id, t.name AS bike_type_name, p.hourly_paise, p.daily_cap_paise,
               p.late_fee_per_hour_paise, lower(p.valid_during) AS starts_at, upper(p.valid_during) AS ends_at"""


@router.get("/pricing-plans")
def pricing_plans():
    with db.tx() as conn:
        return conn.execute(
            f"SELECT {PLAN_COLS} FROM pricing_plan p JOIN bike_type t ON t.id = p.bike_type_id ORDER BY t.name, lower(p.valid_during) DESC"
        ).fetchall()


@router.post("/pricing-plans", status_code=201)
def create_pricing_plan(body: PlanIn, admin: Admin, request: Request):
    """A new price from starts_at onward. The plan in force then is ended at that moment, so plans never overlap."""
    with db.tx() as conn:
        conn.execute(
            """UPDATE pricing_plan SET valid_during = tstzrange(lower(valid_during), %(at)s)
               WHERE bike_type_id = %(type)s AND valid_during @> %(at)s::timestamptz AND lower(valid_during) < %(at)s""",
            {"type": body.bike_type_id, "at": body.starts_at},
        )
        plan_id = conn.execute(
            """INSERT INTO pricing_plan (bike_type_id, hourly_paise, daily_cap_paise, late_fee_per_hour_paise, valid_during)
               VALUES (%(bike_type_id)s, %(hourly_paise)s, %(daily_cap_paise)s, %(late_fee_per_hour_paise)s,
                       tstzrange(%(starts_at)s, NULL)) RETURNING id""",
            body.model_dump(),
        ).fetchone()["id"]
        audit.record(conn, admin["id"], "pricing_plan.create", "pricing_plan", plan_id,
                     after=_plain(body.model_dump()), ip=client_ip(request))
        return conn.execute(
            f"SELECT {PLAN_COLS} FROM pricing_plan p JOIN bike_type t ON t.id = p.bike_type_id WHERE p.id = %s", (plan_id,)
        ).fetchone()


@router.get("/price-modifiers")
def price_modifiers():
    with db.tx() as conn:
        return conn.execute("SELECT * FROM price_modifier ORDER BY id").fetchall()


@router.post("/price-modifiers", status_code=201)
def create_price_modifier(body: ModifierIn, admin: Admin, request: Request):
    with db.tx() as conn:
        return _create(conn, "price_modifier", body.model_dump(), admin, request)


@router.put("/price-modifiers/{modifier_id}")
def update_price_modifier(modifier_id: int, body: ModifierIn, admin: Admin, request: Request):
    with db.tx() as conn:
        return _update(conn, "price_modifier", modifier_id, body.model_dump(), admin, request)


# ---- settings

class SettingIn(BaseModel):
    value: Any


@router.get("/settings")
def settings_list():
    with db.tx() as conn:
        return conn.execute("SELECT key, value, updated_at FROM setting ORDER BY key").fetchall()


@router.put("/settings/{key}")
def update_setting(key: str, body: SettingIn, admin: Admin, request: Request):
    with db.tx() as conn:
        before = conn.execute("SELECT value FROM setting WHERE key = %s FOR UPDATE", (key,)).fetchone()
        if before is None:
            raise Problem(404, "Setting not found")
        if type(body.value) is not type(before["value"]):
            raise Problem(422, "Invalid request", f"{key} must stay a {type(before['value']).__name__}.")
        conn.execute("UPDATE setting SET value = %s, updated_by = %s, updated_at = now() WHERE key = %s",
                     (Jsonb(body.value), admin["id"], key))
        audit.record(conn, admin["id"], "setting.update", "setting", None, before={key: before["value"]},
                     after={key: body.value}, ip=client_ip(request))
    return {"key": key, "value": body.value}


# ---- bookings

class AdminBookingOut(BookingOut):
    customer_name: str
    customer_email: str


class AdminBookingPage(BaseModel):
    items: list[AdminBookingOut]
    next_cursor: int | None


class StatusIn(BaseModel):
    status: Literal["confirmed", "picked_up", "returned", "cancelled", "expired", "no_show"]
    reason: str = Field(min_length=3, max_length=300)
    refund_pct: int = Field(100, ge=0, le=100)  # used when cancelling


@router.get("/bookings", response_model=AdminBookingPage)
def list_bookings(status: Status | None = None, station_id: int | None = None, q: str | None = None,
                  cursor: int | None = None, limit: int = 30):
    limit = max(1, min(limit, 100))
    with db.tx() as conn:
        rows = conn.execute(
            states.BOOKING + """
            WHERE (%(status)s::text IS NULL OR b.status::text = %(status)s)
              AND (%(station)s::bigint IS NULL OR b.station_id = %(station)s)
              AND (%(q)s::text IS NULL OR b.code ILIKE %(q)s OR u.email ILIKE %(q)s OR u.full_name ILIKE %(q)s)
              AND (%(cursor)s::bigint IS NULL OR b.id < %(cursor)s)
            ORDER BY b.id DESC LIMIT %(limit)s""",
            {"status": status, "station": station_id, "q": f"%{q}%" if q else None, "cursor": cursor, "limit": limit + 1},
        ).fetchall()
    items = [present(r) for r in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}


@router.post("/bookings/{booking_id}/status", response_model=AdminBookingOut)
def set_booking_status(booking_id: int, body: StatusIn, admin: Admin, request: Request):
    """An override still goes through the state machine: only its allowed moves work, and the reason is recorded."""
    now = utcnow()
    with db.tx() as conn:
        booking = states.get_booking(conn, booking_id, for_update=True)
        if body.status == "cancelled":
            bookings.cancel_booking(conn, booking, admin["id"], now, reason=body.reason, refund_pct=body.refund_pct)
        elif body.status == "picked_up":
            states.transition(conn, booking_id, "picked_up", actor_id=admin["id"], reason=body.reason, now=now, picked_up_at=now)
        elif body.status == "returned":
            states.transition(conn, booking_id, "returned", actor_id=admin["id"], reason=body.reason, now=now,
                              returned_at=now, late_minutes=0)
        else:
            states.transition(conn, booking_id, body.status, actor_id=admin["id"], reason=body.reason, now=now)
        audit.record(conn, admin["id"], "booking.status", "booking", booking_id, before={"status": booking["status"]},
                     after={"status": body.status}, reason=body.reason, ip=client_ip(request))
        return present(states.get_booking(conn, booking_id))


# ---- users

class AdminUserOut(UserOut):
    blocked_at: datetime | None


class UserPatch(BaseModel):
    role: Literal["customer", "staff", "admin"] | None = None
    staff_station_id: int | None = None
    blocked: bool | None = None


@router.get("/users", response_model=list[AdminUserOut])
def users(q: str | None = None, role: str | None = None, limit: int = 100):
    with db.tx() as conn:
        return conn.execute(
            """SELECT * FROM app_user
               WHERE (%(q)s::text IS NULL OR email ILIKE %(q)s OR full_name ILIKE %(q)s)
                 AND (%(role)s::text IS NULL OR role::text = %(role)s)
               ORDER BY id DESC LIMIT %(limit)s""",
            {"q": f"%{q}%" if q else None, "role": role, "limit": max(1, min(limit, 200))},
        ).fetchall()


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def update_user(user_id: int, body: UserPatch, admin: Admin, request: Request):
    changes = body.model_dump(exclude_unset=True, exclude={"blocked"})
    if user_id == admin["id"] and (body.blocked or changes.get("role", "admin") != "admin"):
        raise Problem(409, "You cannot block or demote yourself")
    if changes.get("role") in ("customer", "admin"):
        changes["staff_station_id"] = None
    if body.blocked is not None:
        changes["blocked_at"] = utcnow() if body.blocked else None
    with db.tx() as conn:
        user = _update(conn, "app_user", user_id, changes, admin, request)
        if body.blocked:  # a blocked user's sessions end now, not when their access token runs out
            conn.execute("UPDATE refresh_token SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
        return user


# ---- background jobs and the audit trail

@router.get("/jobs")
def jobs(status: Literal["queued", "running", "done", "failed", "dead"] = "dead", limit: int = 50):
    with db.tx() as conn:
        counts = conn.execute("SELECT status, count(*)::int AS n FROM job GROUP BY status").fetchall()
        items = conn.execute(
            """SELECT id, kind, payload, status, attempts, max_attempts, run_at, last_error, created_at, finished_at
               FROM job WHERE status = %s ORDER BY id DESC LIMIT %s""",
            (status, max(1, min(limit, 200))),
        ).fetchall()
    return {"counts": {c["status"]: c["n"] for c in counts}, "items": items}


@router.post("/jobs/{job_id}/retry", status_code=204)
def retry_job(job_id: int, admin: Admin, request: Request):
    with db.tx() as conn:
        row = conn.execute(
            """UPDATE job SET status = 'queued', attempts = 0, run_at = now(), last_error = NULL, finished_at = NULL
               WHERE id = %s AND status IN ('dead', 'failed') RETURNING id""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise Problem(404, "No failed job with that id")
        audit.record(conn, admin["id"], "job.retry", "job", job_id, ip=client_ip(request))


@router.get("/audit")
def audit_log(entity: str | None = None, entity_id: int | None = None, limit: int = 50):
    with db.tx() as conn:
        return conn.execute(
            """SELECT a.id, a.action, a.entity, a.entity_id, a.before, a.after, a.reason, a.at, u.email AS actor
               FROM audit_log a LEFT JOIN app_user u ON u.id = a.actor_id
               WHERE (%(entity)s::text IS NULL OR a.entity = %(entity)s)
                 AND (%(id)s::bigint IS NULL OR a.entity_id = %(id)s)
               ORDER BY a.id DESC LIMIT %(limit)s""",
            {"entity": entity, "id": entity_id, "limit": max(1, min(limit, 200))},
        ).fetchall()
