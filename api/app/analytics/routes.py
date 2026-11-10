from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends

from app import db
from app.analytics import service
from app.deps import CurrentUser, require_role
from app.timeutil import utcnow

router = APIRouter(tags=["analytics"])
admin = APIRouter(prefix="/admin/analytics", tags=["admin"], dependencies=[Depends(require_role("admin"))])


@router.get("/me/analytics/summary")
def summary(user: CurrentUser, start: date | None = None, end: date | None = None):
    with db.tx() as conn:
        return service.summary(conn, user["id"], start, end, utcnow())


@router.get("/me/analytics/spend")
def spend(user: CurrentUser, group_by: Literal["month", "bike_type", "station"] = "month",
          start: date | None = None, end: date | None = None):
    with db.tx() as conn:
        return service.spend(conn, user["id"], group_by, start, end, utcnow())


@router.get("/me/analytics/habits")
def habits(user: CurrentUser, start: date | None = None, end: date | None = None):
    with db.tx() as conn:
        return service.habits(conn, user["id"], start, end, utcnow())


@router.get("/me/analytics/fees")
def fees(user: CurrentUser, start: date | None = None, end: date | None = None):
    with db.tx() as conn:
        return service.fees_and_refunds(conn, user["id"], start, end, utcnow())


@admin.get("/overview")
def overview():
    with db.tx() as conn:
        return service.admin_overview(conn, utcnow())


@admin.get("/revenue")
def revenue(days: int = 30):
    with db.tx() as conn:
        return service.revenue_by_day(conn, max(1, min(days, 366)), utcnow())


@admin.get("/stations")
def stations(days: int = 30):
    with db.tx() as conn:
        return service.station_report(conn, max(1, min(days, 366)), utcnow())


@admin.get("/peak-hours")
def peak_hours(station_id: int | None = None):
    with db.tx() as conn:
        return service.peak_hours(conn, station_id)
