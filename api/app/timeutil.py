from datetime import UTC, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def utcnow() -> datetime:
    return datetime.now(UTC)


def fmt(dt: datetime) -> str:
    """A time as customers read it, in Ranchi time: Fri 14 Nov 2026, 10:00 am."""
    local = dt.astimezone(IST)
    half = "am" if local.hour < 12 else "pm"
    return f"{local:%a} {local.day} {local:%b %Y}, {local.hour % 12 or 12}:{local:%M} {half}"


def rupees(paise: int) -> str:
    whole, part = divmod(abs(paise), 100)
    text = f"₹{whole:,}" if part == 0 else f"₹{whole:,}.{part:02d}"
    return f"-{text}" if paise < 0 else text
