import json
import secrets
import time

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app import db, files
from app.bookings.routes import PaymentOut
from app.bookings.states import get_booking
from app.config import settings
from app.deps import CurrentUser
from app.errors import Problem
from app.jsonutil import dumps
from app.payments import razorpay, service
from app.timeutil import utcnow

router = APIRouter(tags=["payments"])


class CheckoutOut(BaseModel):
    payment_id: int
    order_id: str
    amount_paise: int
    currency: str
    key_id: str
    fake: bool  # true when no Razorpay key is configured: the page shows a test pay button instead of Checkout
    description: str


def _with_order(payment: dict) -> dict:
    order_id = payment["razorpay_order_id"]
    if order_id is None:
        # Outside any transaction: a slow payment provider must not hold a database connection or a row lock.
        try:
            new = razorpay.create_order(payment["amount_paise"], f"pay-{payment['id']}", {"payment_id": str(payment["id"])})
        except httpx.HTTPError:
            raise Problem(502, "The payment provider did not answer", "Try again in a moment.") from None
        with db.tx() as conn:
            order_id = service.attach_order(conn, payment["id"], new)
    return {"payment_id": payment["id"], "order_id": order_id, "amount_paise": payment["amount_paise"], "currency": "INR",
            "key_id": settings.razorpay_key_id, "fake": settings.payments_fake, "description": payment["description"]}


@router.post("/bookings/{booking_id}/checkout", response_model=CheckoutOut)
def checkout_booking(booking_id: int, user: CurrentUser):
    with db.tx() as conn:
        booking = get_booking(conn, booking_id, user["id"], for_update=True)
        payment = service.rental_payment(conn, booking, utcnow())
    return _with_order(payment)


@router.post("/payments/{payment_id}/checkout", response_model=CheckoutOut)
def checkout_due(payment_id: int, user: CurrentUser):
    with db.tx() as conn:
        payment = service.get_payment(conn, payment_id, user["id"])
    if payment["status"] != "due":
        raise Problem(409, "Nothing to pay on this item")
    return _with_order(payment)


@router.get("/me/dues", response_model=list[PaymentOut])
def dues(user: CurrentUser):
    with db.tx() as conn:
        return service.list_payments(conn, user["id"], status="due")


@router.get("/me/payments", response_model=list[PaymentOut])
def my_payments(user: CurrentUser):
    with db.tx() as conn:
        return service.list_payments(conn, user["id"])


@router.get("/bookings/{booking_id}/receipt.pdf")
def receipt(booking_id: int, user: CurrentUser):
    with db.tx() as conn:
        booking = get_booking(conn, booking_id, user["id"])
        if booking["receipt_file_id"] is None:
            raise Problem(404, "Receipt not ready", "It is made a few seconds after the bike is returned.")
        name, content_type, data = files.get(conn, booking["receipt_file_id"])
    return Response(data, media_type=content_type, headers={"Content-Disposition": f'inline; filename="{name}"'})


def _apply(event_id: str, body: dict) -> None:
    with db.tx() as conn:
        service.handle_event(conn, event_id, body["event"], body, utcnow())


@router.post("/webhooks/razorpay", include_in_schema=False)
async def razorpay_webhook(request: Request):
    """Razorpay calls this. Only a body signed with the webhook secret is believed."""
    raw = await request.body()  # the signature covers these exact bytes
    if not razorpay.verify_signature(raw, request.headers.get("x-razorpay-signature", "")):
        raise Problem(400, "Bad signature")
    event_id = request.headers.get("x-razorpay-event-id")
    if not event_id:
        raise Problem(400, "Missing event id")
    await run_in_threadpool(_apply, event_id, json.loads(raw))
    return {"ok": True}


@router.post("/payments/fake/{order_id}/pay", include_in_schema=False)
def fake_pay(order_id: str, user: CurrentUser):
    """Dev only: stands in for Razorpay by building the event it would send and handling it the same way."""
    if not settings.payments_fake:
        raise Problem(404, "Not found")
    with db.tx() as conn:
        payment = conn.execute(
            "SELECT * FROM payment WHERE razorpay_order_id = %s AND user_id = %s", (order_id, user["id"])
        ).fetchone()
    if payment is None:
        raise Problem(404, "Payment not found")
    raw = dumps({
        "entity": "event", "event": "payment.captured", "contains": ["payment"], "created_at": int(time.time()),
        "payload": {"payment": {"entity": {"id": f"pay_fake_{secrets.token_hex(8)}", "order_id": order_id,
                                           "amount": payment["amount_paise"], "currency": "INR", "status": "captured"}}},
    }).encode()
    assert razorpay.verify_signature(raw, razorpay.sign(raw))
    _apply(f"evt_fake_{secrets.token_hex(8)}", json.loads(raw))
    return {"ok": True}
