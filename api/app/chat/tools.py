"""The tools the model may call. Personal tools read the user id from the login, never from the model.

No tool writes: propose_* tools save a chat_action the customer must confirm with a click.
"""

from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from app.analytics import service as analytics
from app.bookings import service as bookings
from app.bookings.states import get_booking
from app.catalog import service as catalog
from app.docs import search
from app.errors import Problem
from app.timeutil import IST, fmt, rupees

ACTION_TTL = timedelta(minutes=10)


def _local(dt: datetime) -> datetime:
    """The model often drops the UTC offset; the prompt says times are Ranchi time, so read them that way."""
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def _my_booking(conn, ctx, code: str) -> dict:
    row = conn.execute("SELECT id FROM booking WHERE code = %s AND user_id = %s",
                       (code.strip().upper(), ctx.user["id"])).fetchone()
    if row is None:
        raise Problem(404, "Booking not found")  # someone else's code looks exactly like a missing one
    return get_booking(conn, row["id"])


def _station(conn, name: str) -> dict:
    rows = conn.execute("SELECT id, name FROM station WHERE active AND name ILIKE %s ORDER BY name",
                        (f"%{name.strip()}%",)).fetchall()
    if len(rows) != 1:
        options = ", ".join(s["name"] for s in conn.execute("SELECT name FROM station WHERE active ORDER BY name"))
        raise Problem(422, "Unknown station" if not rows else "Ambiguous station", f"Stations: {options}")
    return rows[0]


def _bike_type(conn, name: str, now: datetime) -> dict:
    wanted = name.strip().lower()
    for t in catalog.list_bike_types(conn, now):
        if wanted in (t["slug"], t["name"].lower(), t["category"]) or wanted in t["name"].lower():
            return t
    raise Problem(422, "Unknown bike type", "Types: " + ", ".join(t["name"] for t in catalog.list_bike_types(conn, now)))


def _booking_view(b: dict) -> dict:
    return {"code": b["code"], "status": b["status"], "station": b["station_name"], "bike_type": b["bike_type_name"],
            "start": fmt(b["start_at"]), "end": fmt(b["end_at"]), "price": rupees(b["quoted_paise"])}


def _propose(conn, ctx, kind: str, payload: dict, preview: dict) -> dict:
    action = conn.execute(
        """INSERT INTO chat_action (conversation_id, message_id, user_id, kind, payload, preview, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, expires_at""",
        (ctx.conversation_id, ctx.message_id, ctx.user["id"], kind, payload, preview, ctx.now + ACTION_TTL),
    ).fetchone()
    ctx.cards.append({"type": "confirm", "action_id": action["id"], "kind": kind, "preview": preview,
                      "status": "proposed", "expires_at": action["expires_at"]})
    return {"proposal": preview, "note": "A card with Confirm and Not now buttons is on the customer's screen. Nothing has changed yet."}


class Period(BaseModel):
    from_: date | None = Field(None, alias="from", description="First day, YYYY-MM-DD")
    to: date | None = Field(None, description="Last day, YYYY-MM-DD")


class MyBookings(Period):
    status: Literal["pending_payment", "confirmed", "picked_up", "returned", "cancelled", "expired", "no_show"] | None = None
    limit: int = Field(10, ge=1, le=20)


class RideHistory(Period):
    limit: int = Field(10, ge=1, le=30)


class Spend(Period):
    group_by: Literal["month", "bike_type", "station"] = "month"


class Code(BaseModel):
    booking_code: str = Field(description="Like BK-7F3K9Q")


class Slot(BaseModel):
    station: str = Field(description="Station name")
    bike_type: str = Field(description="Bike type name, e.g. Volt E-Bike")
    start: datetime = Field(description="ISO 8601, Ranchi time")
    end: datetime


class Price(BaseModel):
    bike_type: str
    start: datetime
    end: datetime


class Stations(BaseModel):
    name: str | None = None
    near_lat: float | None = None
    near_lng: float | None = None


class Search(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    doc_type: Literal["policy", "manual", "error_codes", "repair", "riding", "route", "station"] | None = None


class Extend(Code):
    new_end: datetime = Field(description="ISO 8601, Ranchi time")


def get_my_bookings(conn, ctx, a: MyBookings):
    rows = bookings.list_bookings(conn, ctx.user["id"], a.status, None, a.limit,
                                  datetime.combine(a.from_, datetime.min.time(), IST) if a.from_ else None,
                                  datetime.combine(a.to + timedelta(days=1), datetime.min.time(), IST) if a.to else None)
    return {"bookings": [_booking_view(b) for b in rows]}


def get_ride_history(conn, ctx, a: RideHistory):
    rides = analytics.ride_history(conn, ctx.user["id"], a.from_, a.to, a.limit, ctx.now)
    return {"rides": [{"code": r["code"], "station": r["station"], "bike_type": r["bike_type"], "date": fmt(r["picked_up_at"]),
                       "minutes": r["minutes"], "late_minutes": r["late_minutes"], "paid": rupees(r["paid_paise"])} for r in rides]}


def get_spend_summary(conn, ctx, a: Spend):
    s = analytics.summary(conn, ctx.user["id"], a.from_, a.to, ctx.now)
    groups = analytics.spend(conn, ctx.user["id"], a.group_by, a.from_, a.to, ctx.now)
    return {"from": s["from"], "to": s["to"], "net_spend": rupees(s["net_spend_paise"]), "refunds": rupees(s["refund_paise"]),
            "this_month": rupees(s["this_month_paise"]), "last_month": rupees(s["last_month_paise"]),
            "by_" + a.group_by: [{"label": g["label"], "spend": rupees(g["amount_paise"]), "rides": g["rides"]} for g in groups]}


def get_ride_stats(conn, ctx, a: Period):
    s = analytics.summary(conn, ctx.user["id"], a.from_, a.to, ctx.now)
    h = analytics.habits(conn, ctx.user["id"], a.from_, a.to, ctx.now)
    days = "Mon Tue Wed Thu Fri Sat Sun".split()
    return {"from": s["from"], "to": s["to"], "rides": s["rides"], "ride_hours": round(s["ride_minutes"] / 60, 1),
            "distance_km": round(s["distance_m"] / 1000, 1), "average_ride_minutes": s["avg_ride_minutes"],
            "longest_ride_minutes": s["longest_ride_minutes"], "favourite_station": s["favourite_station"],
            "favourite_bike_type": s["favourite_bike_type"],
            "rides_by_weekday": {days[r["weekday"] - 1]: r["rides"] for r in h["by_weekday"]},
            "rides_by_start_hour": {f"{r['hour']:02d}:00": r["rides"] for r in h["by_hour"]}}


def get_fees_and_refunds(conn, ctx, a: Period):
    f = analytics.fees_and_refunds(conn, ctx.user["id"], a.from_, a.to, ctx.now)
    return {"unpaid_dues": rupees(f["unpaid_dues_paise"]),
            "fees": [{"booking": x["booking_code"], "kind": x["kind"], "amount": rupees(x["amount_paise"]), "status": x["status"]}
                     for x in f["fees"]],
            "refunds": [{"booking": x["booking_code"], "amount": rupees(x["amount_paise"]), "status": x["status"],
                         "reason": x["reason"]} for x in f["refunds"]]}


def quote_cancellation(conn, ctx, a: Code):
    q = bookings.cancel_quote(conn, _my_booking(conn, ctx, a.booking_code), ctx.now)
    return {"booking": q["code"], "paid": rupees(q["paid_paise"]), "refund": rupees(q["refund_paise"]), "rule": q["rule"]}


def check_availability(conn, ctx, a: Slot):
    station, bike = _station(conn, a.station), _bike_type(conn, a.bike_type, ctx.now)
    r = catalog.availability(conn, station["id"], bike["id"], _local(a.start), _local(a.end), ctx.now)
    return {"station": station["name"], "bike_type": bike["name"], "free_bikes": r["free"], "price": rupees(r["quote"]["total_paise"])}


def get_price(conn, ctx, a: Price):
    bike = _bike_type(conn, a.bike_type, ctx.now)
    q = catalog.price(conn, bike["id"], _local(a.start), _local(a.end))
    return {"bike_type": bike["name"], "total": rupees(q["total_paise"]), "hourly_rate": rupees(q["hourly_paise"]),
            "daily_cap": rupees(q["daily_cap_paise"]),
            "lines": [f"{line['label']}: {rupees(line['amount_paise'])}{' (capped)' if line['capped'] else ''}" for line in q["lines"]]}


def find_stations(conn, ctx, a: Stations):
    near = (a.near_lat, a.near_lng) if a.near_lat is not None and a.near_lng is not None else None
    rows = catalog.list_stations(conn, ctx.now, near)
    if a.name:
        rows = [s for s in rows if a.name.lower() in s["name"].lower()]
    return {"stations": [{"name": s["name"], "address": s["address"], "hours": f"{s['opens_at'][:5]} to {s['closes_at'][:5]}",
                          "free_bikes_next_hour": s["free_bikes"],
                          **({"distance_km": round(s["distance_m"] / 1000, 1)} if s["distance_m"] is not None else {})}
                         for s in rows]}


def search_docs(conn, ctx, a: Search):
    found = search.search(conn, a.query, a.doc_type)
    search.log_query(conn, a.query, found, ctx.message_id)
    out = []
    for r in found["results"]:  # numbered across the whole turn, so two searches never reuse a number
        n = len(ctx.sources) + 1
        ctx.sources.append({"n": n, "chunk_id": r["id"], "slug": r["slug"], "title": r["title"],
                            "section_no": r["section_no"], "anchor": r["anchor"], "heading": r["heading"]})
        out.append({"n": n, "source": f"{r['title']} §{r['section_no']} {r['heading']}", "text": r["body"]})
    return {"results": out, "how_to_cite": "Put [n] after each sentence that uses result n."}


def propose_booking(conn, ctx, a: Slot):
    station, bike = _station(conn, a.station), _bike_type(conn, a.bike_type, ctx.now)
    start, end = _local(a.start), _local(a.end)
    r = catalog.availability(conn, station["id"], bike["id"], start, end, ctx.now)
    if r["free"] == 0:
        raise Problem(409, "No bike free", f"No {bike['name']} is free at {station['name']} then.")
    return _propose(conn, ctx, "book", {"station_id": station["id"], "bike_type_id": bike["id"], "start": start, "end": end},
                    {"station": station["name"], "bike_type": bike["name"], "start": fmt(start), "end": fmt(end),
                     "price": rupees(r["quote"]["total_paise"])})


def propose_cancel(conn, ctx, a: Code):
    booking = _my_booking(conn, ctx, a.booking_code)
    q = bookings.cancel_quote(conn, booking, ctx.now)
    return _propose(conn, ctx, "cancel", {"booking_id": booking["id"]},
                    {"booking": booking["code"], "when": fmt(booking["start_at"]), "refund": rupees(q["refund_paise"]),
                     "rule": q["rule"]})


def propose_extend(conn, ctx, a: Extend):
    booking = _my_booking(conn, ctx, a.booking_code)
    q = bookings.extend_quote(conn, booking, _local(a.new_end), ctx.now)
    return _propose(conn, ctx, "extend", {"booking_id": booking["id"], "end": _local(a.new_end)},
                    {"booking": booking["code"], "new_end": fmt(_local(a.new_end)), "extra_cost": rupees(q["extra_paise"])})


# name: (arguments, function, personal, what the status line says, description for the model)
TOOLS = {
    "get_my_bookings": (MyBookings, get_my_bookings, True, "Checking your bookings", "The customer's bookings, newest first."),
    "get_ride_history": (RideHistory, get_ride_history, True, "Looking at your rides", "Past completed rides with duration and cost."),
    "get_spend_summary": (Spend, get_spend_summary, True, "Adding up your spending", "Net spend, grouped by month, bike type or station."),
    "get_ride_stats": (Period, get_ride_stats, True, "Working out your ride stats", "Ride time, distance, longest ride, favourite stations and hours."),
    "get_fees_and_refunds": (Period, get_fees_and_refunds, True, "Checking fees and refunds", "Late fees, extensions, unpaid dues and refunds."),
    "quote_cancellation": (Code, quote_cancellation, True, "Working out the refund", "What the customer would get back if they cancelled now."),
    "check_availability": (Slot, check_availability, False, "Checking free bikes", "Free bikes and the price for a station, bike type and time."),
    "get_price": (Price, get_price, False, "Working out the price", "Price breakdown for a bike type and time."),
    "find_stations": (Stations, find_stations, False, "Finding stations", "Stations with hours and free bikes, nearest first if a point is given."),
    "search_docs": (Search, search_docs, False, "Searching the help pages", "Searches the company's rules, manuals, error codes, repair, riding, route and station guides. Returns the top 5 sections, numbered."),
    "propose_booking": (Slot, propose_booking, True, "Preparing a booking", "Shows the customer a booking to confirm. Books nothing by itself."),
    "propose_cancel": (Code, propose_cancel, True, "Preparing a cancellation", "Shows the customer a cancellation to confirm, with the refund. Cancels nothing by itself."),
    "propose_extend": (Extend, propose_extend, True, "Preparing an extension", "Shows the customer an extension to confirm, with the extra cost. Changes nothing by itself."),
}


def schemas() -> list[dict]:
    return [{"type": "function", "function": {"name": name, "description": desc,
                                              "parameters": model.model_json_schema(by_alias=True)}}
            for name, (model, _, _, _, desc) in TOOLS.items()]
