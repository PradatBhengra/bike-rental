"""The few Razorpay calls the app makes. With no key configured, everything is faked locally (dev and tests)."""

import hashlib
import hmac
import secrets

import httpx

from app.config import settings

API = "https://api.razorpay.com/v1"


def _client() -> httpx.Client:
    return httpx.Client(base_url=API, auth=(settings.razorpay_key_id, settings.razorpay_key_secret), timeout=10)


def create_order(amount_paise: int, receipt: str, notes: dict) -> str:
    if settings.payments_fake:
        return f"order_fake_{secrets.token_hex(8)}"
    with _client() as client:
        r = client.post("/orders", json={"amount": amount_paise, "currency": "INR", "receipt": receipt, "notes": notes})
        r.raise_for_status()
        return r.json()["id"]


def find_refund(razorpay_payment_id: str, key: str) -> dict | None:
    """A refund we already asked for, found by our own key in its notes."""
    if settings.payments_fake:
        return None
    with _client() as client:
        r = client.get(f"/payments/{razorpay_payment_id}/refunds")
        r.raise_for_status()
        return next((x for x in r.json()["items"] if x.get("notes", {}).get("key") == key), None)


def create_refund(razorpay_payment_id: str, amount_paise: int, key: str) -> dict:
    if settings.payments_fake:
        return {"id": f"rfnd_fake_{secrets.token_hex(8)}", "status": "processed"}
    with _client() as client:
        r = client.post(f"/payments/{razorpay_payment_id}/refund", json={"amount": amount_paise, "notes": {"key": key}})
        r.raise_for_status()
        return r.json()


def sign(body: bytes) -> str:
    return hmac.new(settings.razorpay_webhook_secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str) -> bool:
    """Razorpay signs the raw request body; parsing and re-encoding the JSON first would change the bytes."""
    return hmac.compare_digest(sign(body), signature)
