import { useEffect, type ReactNode } from "react";
import { CircleMarker, MapContainer, Polyline, TileLayer, Tooltip, useMap } from "react-leaflet";
import { api, getToken } from "./api";

export const RANCHI: [number, number] = [23.37, 85.325];

/** OpenStreetMap tiles: the attribution is required, and the tile servers are for light use only. */
export function BaseMap({ children, center = RANCHI, zoom = 13, height = "h-96" }: {
  children?: ReactNode;
  center?: [number, number];
  zoom?: number;
  height?: string;
}) {
  return (
    <MapContainer center={center} zoom={zoom} className={`${height} w-full rounded-xl`} scrollWheelZoom>
      <TileLayer
        url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
      />
      {children}
    </MapContainer>
  );
}

/** Keeps the map on a moving point. */
export function Follow({ at }: { at: [number, number] | null }) {
  const map = useMap();
  useEffect(() => {
    if (at) map.panTo(at);
  }, [at, map]);
  return null;
}

export function FitLine({ coords }: { coords: [number, number][] }) {
  const map = useMap();
  useEffect(() => {
    if (coords.length > 1) map.fitBounds(coords, { padding: [30, 30] });
  }, [coords, map]);
  return null;
}

/** GeoJSON is [lng, lat]; Leaflet wants [lat, lng]. Mixing them up puts Ranchi in the sea. */
export const flip = (c: number[]): [number, number] => [c[1], c[0]];

export function StationDot({ s, onClick, active }: { s: any; onClick?: () => void; active?: boolean }) {
  return (
    <CircleMarker
      center={[s.lat, s.lng]}
      radius={active ? 11 : 8}
      pathOptions={{ color: "#0f4870", fillColor: s.free_bikes ? "#3581bc" : "#94a3b8", fillOpacity: 0.9 }}
      eventHandlers={onClick ? { click: onClick } : undefined}
    >
      <Tooltip>
        <b>{s.name}</b>
        {s.free_bikes != null && <div>{s.free_bikes} bikes free now</div>}
      </Tooltip>
    </CircleMarker>
  );
}

export function PathLine({ coords }: { coords: [number, number][] }) {
  return coords.length > 1 ? <Polyline positions={coords} pathOptions={{ color: "#0f4870", weight: 4 }} /> : null;
}

/** Opens the tracking WebSocket with a one-time ticket and calls onMessage for each push. */
export async function openLive(channel: string, onMessage: (m: any) => void): Promise<() => void> {
  if (!getToken()) return () => {};
  const { ticket } = await api("/ws-ticket", { method: "POST" });
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws?ticket=${ticket}`);
  ws.onopen = () => ws.send(JSON.stringify({ subscribe: channel }));
  ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  return () => ws.close();
}
