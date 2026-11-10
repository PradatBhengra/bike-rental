"""Ride paths and the live map, on the api side. The tracking service writes ride_point; the api only reads it."""

import json
import secrets

from fastapi import APIRouter, Depends

from app import cache, db
from app.analytics import rollups
from app.bookings.states import get_booking
from app.catalog import service as catalog
from app.deps import CurrentUser, require_role
from app.errors import Problem
from app.timeutil import utcnow

router = APIRouter(tags=["rides"])
admin = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("admin", "staff"))])
TICKET_TTL_S = 30

BUILD = """
WITH pts AS (
    SELECT location::geometry AS g, recorded_at, recorded_at - lag(recorded_at) OVER (ORDER BY recorded_at) AS gap
    FROM ride_point WHERE booking_id = %(id)s)
INSERT INTO ride_path (booking_id, path, replay_path, distance_m, point_count, gap_count, started_at, ended_at)
SELECT %(id)s, ST_MakeLine(g ORDER BY recorded_at)::geography,
       ST_Simplify(ST_MakeLine(g ORDER BY recorded_at), 0.00005),  -- about 5 m: enough detail for a replay
       ST_Length(ST_MakeLine(g ORDER BY recorded_at)::geography)::int,
       count(*), count(*) FILTER (WHERE gap > interval '60 seconds'), min(recorded_at), max(recorded_at)
FROM pts HAVING count(*) >= 2
ON CONFLICT (booking_id) DO UPDATE SET path = EXCLUDED.path, replay_path = EXCLUDED.replay_path,
    distance_m = EXCLUDED.distance_m, point_count = EXCLUDED.point_count, gap_count = EXCLUDED.gap_count,
    started_at = EXCLUDED.started_at, ended_at = EXCLUDED.ended_at
"""


def build_path_job(conn, payload: dict) -> None:
    """Outbox consumer for booking.returned: one summary line per ride, then its distance joins the rollups."""
    conn.execute(BUILD, {"id": payload["booking_id"]})
    rollups.rebuild(conn, rollups.affected_days(conn, payload["booking_id"], None))


def ensure_partitions_job(conn, payload: dict) -> None:
    conn.execute("SELECT ensure_ride_point_partition((now() + interval '1 month')::date)")


@router.post("/ws-ticket")
def ws_ticket(user: CurrentUser):
    """A one-time ticket for the WebSocket: browsers cannot send an Authorization header when opening one."""
    ticket = secrets.token_urlsafe(24)
    cache.r.set(f"ws:{ticket}", json.dumps({"user_id": user["id"], "role": user["role"],
                                            "station_id": user["staff_station_id"]}), ex=TICKET_TTL_S)
    return {"ticket": ticket, "expires_in": TICKET_TTL_S}


@router.get("/bookings/{booking_id}/path")
def path(booking_id: int, user: CurrentUser):
    """The finished ride's simplified line, or the points so far while the ride is on."""
    with db.tx() as conn:
        booking = get_booking(conn, booking_id, user["id"])
        done = conn.execute(
            "SELECT ST_AsGeoJSON(replay_path)::json AS line, distance_m FROM ride_path WHERE booking_id = %s", (booking_id,)
        ).fetchone()
        if done:
            return {"live": False, "coordinates": done["line"]["coordinates"], "distance_m": done["distance_m"]}
        live = conn.execute(
            """SELECT coalesce(json_agg(json_build_array(ST_X(location::geometry), ST_Y(location::geometry))
                                        ORDER BY recorded_at), '[]') AS coords,
                      coalesce(ST_Length(ST_MakeLine(location::geometry ORDER BY recorded_at)::geography), 0)::int AS m
               FROM ride_point WHERE booking_id = %s""",
            (booking_id,),
        ).fetchone()
    return {"live": booking["status"] == "picked_up", "coordinates": live["coords"], "distance_m": live["m"]}


@router.delete("/me/ride-paths", status_code=204)
def delete_paths(user: CurrentUser):
    with db.tx() as conn:
        mine = "SELECT id FROM booking WHERE user_id = %s"
        conn.execute(f"DELETE FROM ride_path WHERE booking_id IN ({mine})", (user["id"],))
        conn.execute(f"DELETE FROM ride_point WHERE booking_id IN ({mine})", (user["id"],))


@admin.get("/fleet/live")
def fleet_live(user: CurrentUser):
    """Snapshot for the live map; WebSocket messages on the 'fleet' channel keep it current. Staff see their station."""
    station = user["staff_station_id"] if user["role"] == "staff" else None
    with db.tx() as conn:
        rides = conn.execute(
            """SELECT b.id AS booking_id, b.code, k.code AS bike_code, t.name AS bike_type, u.full_name AS rider,
                      b.station_id, upper(b.during) AS due_back, b.overdue_notified_at IS NOT NULL AS overdue,
                      ST_Y(p.location::geometry) AS lat, ST_X(p.location::geometry) AS lng, p.recorded_at
               FROM booking b JOIN bike k ON k.id = b.bike_id JOIN bike_type t ON t.id = k.bike_type_id
               JOIN app_user u ON u.id = b.user_id
               LEFT JOIN LATERAL (SELECT location, recorded_at FROM ride_point WHERE booking_id = b.id
                                  ORDER BY recorded_at DESC LIMIT 1) p ON true
               WHERE b.status = 'picked_up' AND (%(station)s::bigint IS NULL OR b.station_id = %(station)s)
               ORDER BY upper(b.during)""",
            {"station": station},
        ).fetchall()
        stations = catalog.list_stations(conn, utcnow())
    if station:
        stations = [s for s in stations if s["id"] == station]
    return {"rides": rides, "stations": stations}


def can_watch(conn, who: dict, channel: str) -> bool:
    """Who may subscribe to a live channel: the fleet for admins and staff, one ride for its rider too."""
    if channel == "fleet":
        return who["role"] in ("admin", "staff")
    if not channel.startswith("ride:") or not channel[5:].isdigit():
        return False
    booking = conn.execute("SELECT user_id, station_id FROM booking WHERE id = %s", (int(channel[5:]),)).fetchone()
    if booking is None:
        raise Problem(404, "Booking not found")
    return (booking["user_id"] == who["user_id"] or who["role"] == "admin"
            or (who["role"] == "staff" and booking["station_id"] == who["station_id"]))
