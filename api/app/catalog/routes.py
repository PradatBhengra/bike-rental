from datetime import time

from fastapi import APIRouter
from pydantic import AwareDatetime, BaseModel

from app import db
from app.catalog import service
from app.errors import Problem
from app.timeutil import utcnow

router = APIRouter(tags=["catalogue"])


class StationOut(BaseModel):
    id: int
    name: str
    address: str
    lat: float
    lng: float
    opens_at: time
    closes_at: time
    capacity: int
    active: bool
    free_bikes: int | None = None
    distance_m: int | None = None


class BikeTypeOut(BaseModel):
    id: int
    slug: str
    name: str
    category: str
    description: str
    specs: dict
    hourly_paise: int | None
    daily_cap_paise: int | None
    late_fee_per_hour_paise: int | None
    manual_slug: str | None = None


class QuoteLine(BaseModel):
    label: str
    hours: int
    amount_paise: int
    capped: bool


class QuoteOut(BaseModel):
    total_paise: int
    hours: int
    lines: list[QuoteLine]
    hourly_paise: int
    daily_cap_paise: int
    late_fee_per_hour_paise: int


class AvailabilityOut(BaseModel):
    free: int
    quote: QuoteOut


@router.get("/stations", response_model=list[StationOut])
def stations(near: str | None = None):
    """`near` is `lat,lng`; with it the list is sorted nearest first and carries distances."""
    point = None
    if near:
        try:
            lat, lng = (float(x) for x in near.split(","))
        except ValueError:
            raise Problem(422, "Invalid request", "near must be lat,lng") from None
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise Problem(422, "Invalid request", "near must be lat,lng")
        point = (lat, lng)
    with db.tx() as conn:
        return service.list_stations(conn, utcnow(), point)


@router.get("/bike-types", response_model=list[BikeTypeOut])
def bike_types():
    with db.tx() as conn:
        return service.list_bike_types(conn, utcnow())


@router.get("/bike-types/{slug}", response_model=BikeTypeOut)
def bike_type(slug: str):
    with db.tx() as conn:
        return service.get_bike_type(conn, slug, utcnow())


@router.get("/availability", response_model=AvailabilityOut)
def availability(station_id: int, bike_type_id: int, start: AwareDatetime, end: AwareDatetime):
    with db.tx() as conn:
        return service.availability(conn, station_id, bike_type_id, start, end, utcnow())
