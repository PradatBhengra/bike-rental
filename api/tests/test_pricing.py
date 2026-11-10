from datetime import datetime, time, timedelta

import psycopg
import pytest

from app import db
from app.catalog.pricing import late_fee, quote, refund_pct
from app.timeutil import IST

PLAN = {"id": 1, "bike_type_id": 7, "hourly_paise": 4000, "daily_cap_paise": 25000, "late_fee_per_hour_paise": 6000}
MONDAY_10 = datetime(2026, 11, 9, 10, 0, tzinfo=IST)
SATURDAY_10 = datetime(2026, 11, 14, 10, 0, tzinfo=IST)
WEEKEND = {"pct": 25, "days_of_week": [6, 7], "starts_at": time(0, 0), "ends_at": time(23, 59, 59), "bike_type_id": None}


def total(start, hours=0, minutes=0, modifiers=()):
    return quote(start, start + timedelta(hours=hours, minutes=minutes), PLAN, list(modifiers))["total_paise"]


def test_each_started_hour_is_charged():
    assert total(MONDAY_10, hours=1) == 4000
    assert total(MONDAY_10, hours=1, minutes=10) == 8000  # 1 h 10 min is two started hours
    assert total(MONDAY_10, hours=2) == 8000


def test_daily_cap_applies_to_each_24_hours():
    assert total(MONDAY_10, hours=7) == 25000  # 7 h would be Rs 280; the cap is Rs 250
    assert total(MONDAY_10, hours=24) == 25000
    q = quote(MONDAY_10, MONDAY_10 + timedelta(hours=30), PLAN, [])
    assert q["total_paise"] == 25000 + 6 * 4000  # one capped day, then six more hours
    assert [line["capped"] for line in q["lines"]] == [True, False]


def test_modifier_applies_by_day_and_type():
    assert total(SATURDAY_10, hours=2, modifiers=[WEEKEND]) == 10000  # +25% on both hours
    assert total(MONDAY_10, hours=2, modifiers=[WEEKEND]) == 8000
    other_type = {**WEEKEND, "bike_type_id": 99}
    assert total(SATURDAY_10, hours=2, modifiers=[other_type]) == 8000


def test_modifier_applies_hour_by_hour():
    evening = {"pct": 50, "days_of_week": [1, 2, 3, 4, 5], "starts_at": time(17, 0), "ends_at": time(20, 0), "bike_type_id": 7}
    start = MONDAY_10.replace(hour=16)
    assert total(start, hours=2, modifiers=[evening]) == 4000 + 6000  # 16:00 plain, 17:00 at +50%


def test_late_fee():
    assert late_fee(0, 6000) == 0
    assert late_fee(20, 6000) == 6000
    assert late_fee(61, 6000) == 12000


@pytest.mark.parametrize("hours_before, pct", [(48, 100), (24, 100), (23.9, 50), (2, 50), (1.9, 0), (-1, 0)])
def test_refund_tiers(hours_before, pct):
    tiers = [{"min_hours_before": 24, "pct": 100}, {"min_hours_before": 2, "pct": 50}, {"min_hours_before": 0, "pct": 0}]
    start = MONDAY_10
    assert refund_pct(tiers, start, start - timedelta(hours=hours_before))[0] == pct


def test_database_refuses_overlapping_price_plans(world):
    with pytest.raises(psycopg.errors.ExclusionViolation), db.tx() as conn:
        conn.execute(
            """INSERT INTO pricing_plan (bike_type_id, hourly_paise, daily_cap_paise, late_fee_per_hour_paise, valid_during)
               VALUES (%s, 5000, 30000, 6000, tstzrange('2026-01-01', NULL))""",
            (world.type_id,),
        )


def test_new_plan_ends_the_old_one(client, world, make_user):
    from tests.conftest import auth, rows

    admin = make_user("admin")
    r = client.post("/api/v1/admin/pricing-plans", headers=auth(admin), json={
        "bike_type_id": world.type_id, "hourly_paise": 5000, "daily_cap_paise": 30000, "late_fee_per_hour_paise": 7000,
        "starts_at": "2027-01-01T00:00:00+05:30"})
    assert r.status_code == 201
    plans = rows("SELECT hourly_paise, upper(valid_during) AS ends FROM pricing_plan ORDER BY id")
    assert plans[0]["ends"] is not None and plans[1]["ends"] is None
    assert rows("SELECT action FROM audit_log")[0]["action"] == "pricing_plan.create"
