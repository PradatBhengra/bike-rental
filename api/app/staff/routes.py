from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import audit, db
from app.bookings import service, states
from app.bookings.routes import BookingOut, present
from app.deps import Staff
from app.errors import Problem
from app.timeutil import IST, utcnow

router = APIRouter(prefix="/staff", tags=["staff"])


class DeskBookingOut(BookingOut):
    customer_name: str
    customer_phone: str | None


class DeskOut(BaseModel):
    station_name: str
    date: date
    pickups: list[DeskBookingOut]
    out: list[DeskBookingOut]
    returned_today: int


class ReturnIn(BaseModel):
    note: str | None = Field(None, max_length=500)
    odometer_km: float | None = Field(None, ge=0, le=999999)


class ReturnOut(BaseModel):
    booking: DeskBookingOut
    late_minutes: int
    late_fee_paise: int


class StationBikeOut(BaseModel):
    id: int
    code: str
    bike_type_name: str
    status: str
    odometer_km: float
    out_with: str | None
    due_back: datetime | None


class BikeStatusIn(BaseModel):
    status: Literal["available", "maintenance"]
    reason: str = Field(min_length=3, max_length=200)


@router.get("/desk", response_model=DeskOut)
def desk(staff: Staff, date: date | None = None):
    """Bookings to hand over on the given day (Ranchi time) and every bike from this station that is still out."""
    day = date or utcnow().astimezone(IST).date()
    params = {"station": staff["staff_station_id"], "day": day}
    with db.tx() as conn:
        pickups = conn.execute(
            states.BOOKING + """ WHERE b.station_id = %(station)s AND b.status = 'confirmed'
                AND (lower(b.during) AT TIME ZONE 'Asia/Kolkata')::date = %(day)s ORDER BY lower(b.during)""",
            params,
        ).fetchall()
        out = conn.execute(
            states.BOOKING + " WHERE b.station_id = %(station)s AND b.status = 'picked_up' ORDER BY upper(b.during)", params
        ).fetchall()
        returned = conn.execute(
            """SELECT count(*) AS n FROM booking WHERE station_id = %(station)s AND status = 'returned'
               AND (returned_at AT TIME ZONE 'Asia/Kolkata')::date = %(day)s""",
            params,
        ).fetchone()["n"]
        station = conn.execute("SELECT name FROM station WHERE id = %s", (staff["staff_station_id"],)).fetchone()
    return {"station_name": station["name"], "date": day, "pickups": [present(b) for b in pickups],
            "out": [present(b) for b in out], "returned_today": returned}


@router.get("/bookings/{code}", response_model=DeskBookingOut)
def lookup(code: str, staff: Staff):
    with db.tx() as conn:
        return present(states.get_by_code(conn, code, staff["staff_station_id"]))


@router.post("/bookings/{code}/pickup", response_model=DeskBookingOut)
def pickup(code: str, staff: Staff):
    with db.tx() as conn:
        booking = states.get_by_code(conn, code, staff["staff_station_id"])
        return present(service.pickup(conn, booking, staff["id"], utcnow()))


@router.post("/bookings/{code}/return", response_model=ReturnOut)
def return_bike(code: str, body: ReturnIn, staff: Staff):
    with db.tx() as conn:
        booking = states.get_by_code(conn, code, staff["staff_station_id"])
        result = service.return_bike(conn, booking, staff["id"], utcnow(), body.note, body.odometer_km)
        return {**result, "booking": present(result["booking"])}


@router.get("/bikes", response_model=list[StationBikeOut])
def bikes(staff: Staff):
    with db.tx() as conn:
        return conn.execute(
            """SELECT k.id, k.code, t.name AS bike_type_name, k.status, k.odometer_km,
                      b.code AS out_with, upper(b.during) AS due_back
               FROM bike k JOIN bike_type t ON t.id = k.bike_type_id
               LEFT JOIN booking b ON b.bike_id = k.id AND b.status = 'picked_up'
               WHERE k.home_station_id = %s AND k.status <> 'retired' ORDER BY t.name, k.code""",
            (staff["staff_station_id"],),
        ).fetchall()


@router.patch("/bikes/{bike_id}", status_code=204)
def set_bike_status(bike_id: int, body: BikeStatusIn, staff: Staff):
    """Takes a bike out of service or puts it back. Bikes at other stations are 'not found'."""
    with db.tx() as conn:
        before = conn.execute(
            "SELECT id, status FROM bike WHERE id = %s AND home_station_id = %s AND status <> 'retired' FOR UPDATE",
            (bike_id, staff["staff_station_id"]),
        ).fetchone()
        if before is None:
            raise Problem(404, "Bike not found")
        conn.execute("UPDATE bike SET status = %s WHERE id = %s", (body.status, bike_id))
        audit.record(conn, staff["id"], "bike.status", "bike", bike_id, before={"status": before["status"]},
                     after={"status": body.status}, reason=body.reason)
