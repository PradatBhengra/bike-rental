from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel

from app import db
from app.bookings import service
from app.bookings.states import get_booking
from app.deps import CurrentUser
from app.idempotency import remember
from app.payments import service as payments
from app.timeutil import utcnow

router = APIRouter(tags=["bookings"])
Status = Literal["pending_payment", "confirmed", "picked_up", "returned", "cancelled", "expired", "no_show"]


class BookingIn(BaseModel):
    station_id: int
    bike_type_id: int
    start: AwareDatetime  # a time with no UTC offset is refused: the server will not guess the timezone
    end: AwareDatetime


class ExtendIn(BaseModel):
    end: AwareDatetime


class BookingOut(BaseModel):
    id: int
    code: str
    status: Status
    start_at: datetime
    end_at: datetime
    hold_expires_at: datetime | None
    quoted_paise: int
    price_snapshot: dict
    picked_up_at: datetime | None
    returned_at: datetime | None
    late_minutes: int | None
    overdue: bool = False
    station_id: int
    station_name: str
    bike_code: str
    bike_type_id: int
    bike_type_slug: str
    bike_type_name: str
    has_receipt: bool = False
    created_at: datetime


class PaymentOut(BaseModel):
    id: int
    booking_id: int | None
    booking_code: str | None
    kind: str
    amount_paise: int
    status: str
    description: str
    refunded_paise: int
    paid_at: datetime | None
    created_at: datetime


class HistoryOut(BaseModel):
    from_status: str | None
    to_status: str
    reason: str | None
    at: datetime


class BookingDetailOut(BookingOut):
    payments: list[PaymentOut]
    history: list[HistoryOut]


class BookingPage(BaseModel):
    items: list[BookingOut]
    next_cursor: int | None


class CancelQuoteOut(BaseModel):
    booking_id: int
    code: str
    paid_paise: int
    refund_pct: int
    refund_paise: int
    rule: str
    hours_before_start: float


class ExtendOut(BaseModel):
    booking: BookingOut
    extra_paise: int


def present(booking: dict) -> dict:
    return {**booking, "overdue": booking["overdue_notified_at"] is not None and booking["status"] == "picked_up",
            "has_receipt": booking["receipt_file_id"] is not None}


@router.post("/bookings", status_code=201, response_model=BookingOut)
def create_booking(body: BookingIn, user: CurrentUser,
                   idempotency_key: Annotated[str, Header(min_length=8, max_length=100)]):
    """Holds a bike for 15 minutes. A retry with the same Idempotency-Key returns the first booking, not a second."""
    def run():
        booking = service.create_booking(conn, user["id"], body.station_id, body.bike_type_id, body.start, body.end, utcnow())
        return 201, BookingOut(**present(booking)).model_dump(mode="json")

    with db.tx() as conn:
        status, response = remember(conn, user["id"], "POST /bookings", idempotency_key, body.model_dump(mode="json"), run)
    return JSONResponse(response, status)


@router.get("/bookings", response_model=BookingPage)
def list_bookings(user: CurrentUser, status: Status | None = None, cursor: int | None = None, limit: int = 20):
    limit = max(1, min(limit, 50))
    with db.tx() as conn:
        rows = service.list_bookings(conn, user["id"], status, cursor, limit + 1)
    items = [present(r) for r in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}


@router.get("/bookings/{booking_id}", response_model=BookingDetailOut)
def booking_detail(booking_id: int, user: CurrentUser):
    with db.tx() as conn:
        booking = get_booking(conn, booking_id, user["id"])
        history = conn.execute(
            "SELECT from_status, to_status, reason, at FROM booking_status_history WHERE booking_id = %s ORDER BY id",
            (booking_id,),
        ).fetchall()
        return {**present(booking), "history": history,
                "payments": payments.list_payments(conn, user["id"], booking_id=booking_id)}


@router.get("/bookings/{booking_id}/cancel-quote", response_model=CancelQuoteOut)
def cancel_quote(booking_id: int, user: CurrentUser):
    with db.tx() as conn:
        return service.cancel_quote(conn, get_booking(conn, booking_id, user["id"]), utcnow())


@router.post("/bookings/{booking_id}/cancel", response_model=CancelQuoteOut)
def cancel(booking_id: int, user: CurrentUser):
    with db.tx() as conn:
        booking = get_booking(conn, booking_id, user["id"], for_update=True)
        return service.cancel_booking(conn, booking, user["id"], utcnow())


@router.post("/bookings/{booking_id}/extend", response_model=ExtendOut)
def extend(booking_id: int, body: ExtendIn, user: CurrentUser):
    with db.tx() as conn:
        booking = get_booking(conn, booking_id, user["id"], for_update=True)
        result = service.extend_booking(conn, booking, body.end, utcnow())
        return {"booking": present(result["booking"]), "extra_paise": result["extra_paise"]}
