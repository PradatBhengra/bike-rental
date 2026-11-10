import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router";
import { CircleMarker } from "react-leaflet";
import { api, getToken, money, when, type S } from "../api";
import { useAuth } from "../auth";
import { BaseMap, flip, Follow, openLive, PathLine, StationDot } from "../map";
import { ErrorText, Page } from "../ui";

type Fix = { lat: number; lng: number; recorded_at: string; accuracy_m: number; speed_mps: number | null };
const SEND_EVERY_MS = 10000;

/** Watches the phone's position and posts it in batches; a failed batch stays in the buffer for the next try. */
function useTracking(bookingId: number, on: boolean) {
  const buffer = useRef<Fix[]>([]);
  const [sent, setSent] = useState(0);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!on || !navigator.geolocation) return;
    const watch = navigator.geolocation.watchPosition(
      (p) => buffer.current.push({ lat: p.coords.latitude, lng: p.coords.longitude, recorded_at: new Date(p.timestamp).toISOString(), accuracy_m: p.coords.accuracy, speed_mps: p.coords.speed }),
      (e) => setError(e.message),
      { enableHighAccuracy: true, maximumAge: 5000 },
    );
    const timer = setInterval(async () => {
      if (!buffer.current.length) return;
      const batch = buffer.current.splice(0);
      const r = await fetch(`/track/${bookingId}/points`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken()}` },
        body: JSON.stringify({ points: batch }),
      }).catch(() => null);
      if (r?.ok) { const { accepted } = await r.json(); setSent((n) => n + accepted); }
      else if (r && r.status < 500) setError((await r.json().catch(() => ({}))).detail ?? "Tracking refused.");
      else buffer.current.unshift(...batch);
    }, SEND_EVERY_MS);
    return () => {
      navigator.geolocation.clearWatch(watch);
      clearInterval(timer);
    };
  }, [bookingId, on]);
  return { sent, error };
}

export function ActiveRide() {
  const { id } = useParams();
  const bookingId = Number(id);
  const { user } = useAuth();
  const booking = useQuery({ queryKey: ["booking", id], queryFn: () => api<S["BookingDetailOut"]>(`/bookings/${id}`) });
  const path = useQuery({ queryKey: ["path", id], queryFn: () => api(`/bookings/${id}/path`) });
  const stations = useQuery({ queryKey: ["stations"], queryFn: () => api<S["StationOut"][]>("/stations") });
  const [line, setLine] = useState<[number, number][]>([]);
  const [now, setNow] = useState(Date.now());
  const [tracking, setTracking] = useState(false);
  const { sent, error } = useTracking(bookingId, tracking);

  useEffect(() => setLine((path.data?.coordinates ?? []).map(flip)), [path.data]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    let close = () => {};
    openLive(`ride:${bookingId}`, (m) => m.type === "point" && setLine((l) => [...l, [m.lat, m.lng]])).then((c) => (close = c));
    return () => {
      clearInterval(t);
      close();
    };
  }, [bookingId]);

  const b = booking.data;
  if (!b) return <ErrorText error={booking.error} />;
  const minutes = b.picked_up_at ? Math.floor((now - new Date(b.picked_up_at).getTime()) / 60000) : 0;
  const due = new Date(b.end_at).getTime() - now;
  const here = line.length ? line[line.length - 1] : null;
  const home = stations.data?.find((s) => s.id === b.station_id);
  return (
    <Page title={`Ride ${b.code}`} actions={<Link className="btn-ghost" to={`/bookings/${b.id}`}>Booking details</Link>}>
      {b.status !== "picked_up" && <p className="mb-3 text-sm">This ride is {b.status.replace("_", " ")}.</p>}
      <div className="grid gap-4 lg:grid-cols-[1fr_300px]">
        <BaseMap center={home ? [home.lat, home.lng] : undefined} zoom={14} height="h-[60vh]">
          {stations.data?.map((s) => <StationDot key={s.id} s={s} />)}
          <PathLine coords={line} />
          {here && <CircleMarker center={here} radius={9} pathOptions={{ color: "#fff", weight: 3, fillColor: "#0f4870", fillOpacity: 1 }} />}
          <Follow at={here} />
        </BaseMap>
        <div className="card h-fit space-y-2 text-sm">
          <div className="text-3xl font-bold text-navy">{Math.floor(minutes / 60)}h {minutes % 60}m</div>
          <div>Return by {when(b.end_at)} to {b.station_name}</div>
          <div className={due < 0 ? "font-semibold text-red-700" : "text-slate-600"}>
            {due < 0 ? `${Math.ceil(-due / 60000)} minutes overdue` : `${Math.floor(due / 60000)} minutes left`}
          </div>
          <div>Distance so far: {line.length > 1 ? `${(km(line)).toFixed(2)} km` : "–"}</div>
          <div>Booked price: {money(b.quoted_paise)}</div>
          <hr className="border-slate-200" />
          {user?.tracking_consent ? (
            <button className="btn w-full" onClick={() => setTracking(!tracking)} disabled={b.status !== "picked_up"}>
              {tracking ? `Tracking on · ${sent} points sent` : "Share my location for this ride"}
            </button>
          ) : (
            <p className="text-slate-600">Turn on tracking in your <Link to="/me" className="text-sky">account</Link> to see your route.</p>
          )}
          {error && <p className="text-red-700">{error}</p>}
          <p className="text-xs text-slate-500">Location is only sent while this page is open. Phones need HTTPS for GPS.</p>
        </div>
      </div>
    </Page>
  );
}

function km(line: [number, number][]) {
  let total = 0;
  for (let i = 1; i < line.length; i++) {
    const [a, b] = [line[i - 1], line[i]];
    const rad = Math.PI / 180;
    const h = Math.sin(((b[0] - a[0]) * rad) / 2) ** 2 + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(((b[1] - a[1]) * rad) / 2) ** 2;
    total += 2 * 6371 * Math.asin(Math.sqrt(h));
  }
  return total;
}
