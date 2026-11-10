"""Price rules as pure functions: no database, so they are easy to test and to call from anywhere."""

from datetime import datetime, timedelta
from math import ceil

from app.timeutil import IST

HOUR = timedelta(hours=1)
DAY = timedelta(hours=24)


def _applies(modifier: dict, slot: datetime, bike_type_id: int) -> bool:
    local = slot.astimezone(IST)
    return (
        modifier["bike_type_id"] in (None, bike_type_id)
        and local.isoweekday() in modifier["days_of_week"]
        and modifier["starts_at"] <= local.time() < modifier["ends_at"]
    )


def quote(start: datetime, end: datetime, plan: dict, modifiers: list[dict]) -> dict:
    """Each started hour costs the hourly rate plus any modifier. Every 24 hours is capped at the daily cap."""
    lines, block_start = [], start
    while block_start < end:
        block_end = min(block_start + DAY, end)
        hours = ceil((block_end - block_start) / HOUR)
        cost = 0
        for h in range(hours):
            pct = sum(m["pct"] for m in modifiers if _applies(m, block_start + h * HOUR, plan["bike_type_id"]))
            cost += plan["hourly_paise"] * (100 + pct) // 100
        lines.append({
            "label": f"Day {len(lines) + 1}: {hours} h",
            "hours": hours,
            "amount_paise": min(cost, plan["daily_cap_paise"]),
            "capped": cost > plan["daily_cap_paise"],
        })
        block_start = block_end
    return {
        "total_paise": sum(line["amount_paise"] for line in lines),
        "hours": sum(line["hours"] for line in lines),
        "lines": lines,
        "plan_id": plan["id"],
        "bike_type_id": plan["bike_type_id"],
        "hourly_paise": plan["hourly_paise"],
        "daily_cap_paise": plan["daily_cap_paise"],
        "late_fee_per_hour_paise": plan["late_fee_per_hour_paise"],
    }


def late_fee(late_minutes: int, late_fee_per_hour_paise: int) -> int:
    """Each started hour late costs the late rate. late_minutes is 0 inside the grace period."""
    return ceil(late_minutes / 60) * late_fee_per_hour_paise


def refund_pct(tiers: list[dict], start: datetime, now: datetime) -> tuple[int, dict]:
    """The first tier whose notice the customer gave. Tiers are sorted by most notice first."""
    hours_before = (start - now) / HOUR
    for tier in sorted(tiers, key=lambda t: -t["min_hours_before"]):
        if hours_before >= tier["min_hours_before"]:
            return tier["pct"], tier
    return 0, {"min_hours_before": 0, "pct": 0}
