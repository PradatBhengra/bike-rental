import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, money, when, type S } from "../api";
import { Empty, ErrorText, Field, Page, StatusChip } from "../ui";

type DeskBooking = S["DeskBookingOut"];

export function Desk() {
  const qc = useQueryClient();
  const [date, setDate] = useState("");
  const desk = useQuery({ queryKey: ["desk", date], queryFn: () => api<S["DeskOut"]>(`/staff/desk${date ? `?date=${date}` : ""}`), refetchInterval: 15000 });
  const [code, setCode] = useState("");
  const [found, setFound] = useState<DeskBooking | null>(null);
  const [returning, setReturning] = useState<DeskBooking | null>(null);
  const [form, setForm] = useState({ note: "", odometer_km: "" });
  const [message, setMessage] = useState("");
  const [error, setError] = useState<unknown>(null);
  const run = (f: () => Promise<string>) => async () => {
    setError(null);
    setMessage("");
    try {
      setMessage(await f());
      setFound(null);
      setReturning(null);
      qc.invalidateQueries({ queryKey: ["desk"] });
    } catch (e) {
      setError(e);
    }
  };
  const handOver = (b: DeskBooking) => run(async () => {
    await api(`/staff/bookings/${b.code}/pickup`, { method: "POST" });
    return `${b.code} handed over: bike ${b.bike_code}.`;
  });
  const checkIn = (b: DeskBooking) => run(async () => {
    const r = await api(`/staff/bookings/${b.code}/return`, { method: "POST", json: { note: form.note || null, odometer_km: form.odometer_km ? +form.odometer_km : null } });
    return r.late_fee_paise ? `${b.code} returned ${r.late_minutes} min late: late fee ${money(r.late_fee_paise)} added as a due.` : `${b.code} returned on time.`;
  });
  const row = (b: DeskBooking, action: React.ReactNode) => (
    <div key={b.id} className="flex flex-wrap items-center gap-3 border-b border-slate-100 py-2 text-sm">
      <span className="font-mono">{b.code}</span>
      <StatusChip status={b.status} />
      {b.overdue && <span className="chip bg-red-100 text-red-700">overdue</span>}
      <span>{b.customer_name}{b.customer_phone && ` · ${b.customer_phone}`}</span>
      <span className="text-slate-600">{b.bike_type_name} ({b.bike_code})</span>
      <span className="ml-auto text-slate-600">{when(b.start_at, false)} → {when(b.end_at, false)}</span>
      {action}
    </div>
  );
  return (
    <Page title={`Desk · ${desk.data?.station_name ?? ""}`} actions={<input className="input w-auto" type="date" value={date} onChange={(e) => setDate(e.target.value)} />}>
      <form className="card mb-4 flex items-end gap-2" onSubmit={(e) => { e.preventDefault(); setError(null); api(`/staff/bookings/${code}`).then(setFound, setError); }}>
        <Field label="Booking code">
          <input className="input font-mono uppercase" value={code} onChange={(e) => setCode(e.target.value)} placeholder="BK-XXXXXX" />
        </Field>
        <button className="btn">Look up</button>
      </form>
      {message && <p className="mb-3 rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-800">{message}</p>}
      <ErrorText error={error} />
      {found && <div className="card mb-4">{row(found, found.status === "confirmed" ? <button className="btn" onClick={handOver(found)}>Hand over</button> : found.status === "picked_up" ? <button className="btn" onClick={() => setReturning(found)}>Check in</button> : null)}</div>}
      {returning && (
        <div className="card mb-4 space-y-2">
          <h2 className="font-semibold">Check in {returning.code}</h2>
          <div className="grid gap-2 sm:grid-cols-[1fr_160px]">
            <Field label="Condition note"><input className="input" value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} placeholder="All fine" /></Field>
            <Field label="Odometer (km)"><input className="input" type="number" value={form.odometer_km} onChange={(e) => setForm({ ...form, odometer_km: e.target.value })} /></Field>
          </div>
          <button className="btn" onClick={checkIn(returning)}>Mark returned</button>
        </div>
      )}
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="card">
          <h2 className="font-semibold">To hand over ({desk.data?.pickups.length ?? 0})</h2>
          {desk.data?.pickups.length === 0 && <Empty>No pickups for this day.</Empty>}
          {desk.data?.pickups.map((b) => row(b, <button className="btn-ghost" onClick={handOver(b)}>Hand over</button>))}
        </div>
        <div className="card">
          <h2 className="font-semibold">Out now ({desk.data?.out.length ?? 0}) · returned today {desk.data?.returned_today ?? 0}</h2>
          {desk.data?.out.length === 0 && <Empty>No bikes out.</Empty>}
          {desk.data?.out.map((b) => row(b, <button className="btn-ghost" onClick={() => setReturning(b)}>Check in</button>))}
        </div>
      </div>
    </Page>
  );
}

export function StationBikes() {
  const qc = useQueryClient();
  const bikes = useQuery({ queryKey: ["staff-bikes"], queryFn: () => api<S["StationBikeOut"][]>("/staff/bikes") });
  const [error, setError] = useState<unknown>(null);
  const toggle = async (b: S["StationBikeOut"]) => {
    const status = b.status === "available" ? "maintenance" : "available";
    const reason = prompt(status === "maintenance" ? "Why is it going out of service?" : "Reason for putting it back", status === "available" ? "repaired" : "");
    if (!reason) return;
    try {
      await api(`/staff/bikes/${b.id}`, { method: "PATCH", json: { status, reason } });
      qc.invalidateQueries({ queryKey: ["staff-bikes"] });
    } catch (e) {
      setError(e);
    }
  };
  return (
    <Page title="Station bikes">
      <ErrorText error={error} />
      <div className="card overflow-x-auto">
        <table className="grid-table">
          <thead><tr><th>Bike</th><th>Type</th><th>Status</th><th>Odometer</th><th>Out with</th><th /></tr></thead>
          <tbody>
            {bikes.data?.map((b) => (
              <tr key={b.id}>
                <td className="font-mono">{b.code}</td><td>{b.bike_type_name}</td><td><StatusChip status={b.status} /></td>
                <td>{b.odometer_km} km</td><td>{b.out_with ? `${b.out_with}, due ${when(b.due_back, false)}` : "–"}</td>
                <td><button className="btn-ghost" onClick={() => toggle(b)}>{b.status === "available" ? "Take out of service" : "Put back in service"}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Page>
  );
}
