import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, money } from "../api";
import { Page, Stat } from "../ui";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const rupees = (paise: number) => Math.round(paise / 100);

function Chart({ title, children }: { title: string; children: React.ReactElement }) {
  return (
    <div className="card">
      <h2 className="mb-2 font-semibold">{title}</h2>
      <div className="h-64">
        <ResponsiveContainer>{children}</ResponsiveContainer>
      </div>
    </div>
  );
}

export function MyAnalytics() {
  const [groupBy, setGroupBy] = useState("bike_type");
  const summary = useQuery({ queryKey: ["summary"], queryFn: () => api("/me/analytics/summary") });
  const months = useQuery({ queryKey: ["spend", "month"], queryFn: () => api<any[]>("/me/analytics/spend?group_by=month") });
  const groups = useQuery({ queryKey: ["spend", groupBy], queryFn: () => api<any[]>(`/me/analytics/spend?group_by=${groupBy}`) });
  const habits = useQuery({ queryKey: ["habits"], queryFn: () => api("/me/analytics/habits") });
  const fees = useQuery({ queryKey: ["fees"], queryFn: () => api("/me/analytics/fees") });
  const s = summary.data;
  return (
    <Page title="My analytics">
      {s && (
        <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Stat label="Rides" value={s.rides} hint={`${Math.round(s.ride_minutes / 60)} hours ridden`} />
          <Stat label="Spent" value={money(s.net_spend_paise)} hint={`${money(s.refund_paise)} refunded`} />
          <Stat label="This month" value={money(s.this_month_paise)} hint={`Last month ${money(s.last_month_paise)}`} />
          <Stat label="Distance" value={`${(s.distance_m / 1000).toFixed(1)} km`} hint={`Longest ride ${s.longest_ride_minutes ?? 0} min`} />
          <Stat label="Average ride" value={`${s.avg_ride_minutes} min`} />
          <Stat label="Favourite station" value={<span className="text-base">{s.favourite_station ?? "–"}</span>} />
          <Stat label="Favourite bike" value={<span className="text-base">{s.favourite_bike_type ?? "–"}</span>} />
          <Stat label="Late fees" value={money(s.late_fee_paise)} hint={fees.data?.unpaid_dues_paise ? `${money(fees.data.unpaid_dues_paise)} unpaid` : "All paid"} />
        </div>
      )}
      <div className="grid gap-4 lg:grid-cols-2">
        <Chart title="Spend by month (₹)">
          <BarChart data={months.data?.map((m) => ({ ...m, rupees: rupees(m.amount_paise) }))}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="label" fontSize={12} />
            <YAxis fontSize={12} />
            <Tooltip />
            <Bar dataKey="rupees" fill="#0f4870" radius={[4, 4, 0, 0]} />
          </BarChart>
        </Chart>
        <div className="card">
          <div className="mb-2 flex items-center gap-2">
            <h2 className="font-semibold">Spend by</h2>
            <select className="input w-auto" value={groupBy} onChange={(e) => setGroupBy(e.target.value)}>
              <option value="bike_type">bike type</option>
              <option value="station">station</option>
            </select>
          </div>
          <div className="h-64">
            <ResponsiveContainer>
              <BarChart layout="vertical" data={groups.data?.map((g) => ({ ...g, rupees: rupees(g.amount_paise) }))}>
                <XAxis type="number" fontSize={12} />
                <YAxis type="category" dataKey="label" width={110} fontSize={12} />
                <Tooltip />
                <Bar dataKey="rupees" fill="#3581bc" radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
        <Chart title="When you start rides">
          <BarChart data={habits.data?.by_hour}>
            <XAxis dataKey="hour" tickFormatter={(h) => `${h}:00`} fontSize={12} />
            <YAxis allowDecimals={false} fontSize={12} />
            <Tooltip labelFormatter={(h) => `${h}:00`} />
            <Bar dataKey="rides" fill="#0f4870" radius={[4, 4, 0, 0]} />
          </BarChart>
        </Chart>
        <Chart title="Rides by weekday">
          <BarChart data={habits.data?.by_weekday?.map((d: any) => ({ ...d, day: DAYS[d.weekday - 1] }))}>
            <XAxis dataKey="day" fontSize={12} />
            <YAxis allowDecimals={false} fontSize={12} />
            <Tooltip />
            <Bar dataKey="rides" fill="#3581bc" radius={[4, 4, 0, 0]} />
          </BarChart>
        </Chart>
      </div>
      {fees.data && (fees.data.fees.length > 0 || fees.data.refunds.length > 0) && (
        <div className="card mt-4 text-sm">
          <h2 className="mb-2 font-semibold">Fees and refunds</h2>
          {fees.data.fees.map((f: any) => (
            <div key={f.id} className="flex justify-between border-b border-slate-100 py-1">
              <span>{f.description}</span>
              <span>{money(f.amount_paise)} · {f.status}</span>
            </div>
          ))}
          {fees.data.refunds.map((r: any, i: number) => (
            <div key={i} className="flex justify-between border-b border-slate-100 py-1">
              <span>Refund {r.booking_code} ({r.reason})</span>
              <span className="text-emerald-700">{money(r.amount_paise)}</span>
            </div>
          ))}
        </div>
      )}
    </Page>
  );
}

export function AdminDashboard() {
  const [days, setDays] = useState(30);
  const overview = useQuery({ queryKey: ["overview"], queryFn: () => api("/admin/analytics/overview") });
  const revenue = useQuery({ queryKey: ["revenue", days], queryFn: () => api<any[]>(`/admin/analytics/revenue?days=${days}`) });
  const stations = useQuery({ queryKey: ["station-report", days], queryFn: () => api<any[]>(`/admin/analytics/stations?days=${days}`) });
  const peak = useQuery({ queryKey: ["peak"], queryFn: () => api<any[]>("/admin/analytics/peak-hours") });
  const o = overview.data;
  const max = Math.max(1, ...(peak.data ?? []).map((p) => p.pickups));
  const cell = (d: number, h: number) => peak.data?.find((p) => p.day_of_week === d && p.hour === h)?.pickups ?? 0;
  return (
    <Page
      title="Dashboard"
      actions={
        <select className="input w-auto" value={days} onChange={(e) => setDays(+e.target.value)}>
          {[7, 30, 90].map((d) => <option key={d} value={d}>Last {d} days</option>)}
        </select>
      }
    >
      {o && (
        <div className="mb-4 grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="Rides now" value={o.rides_now} hint={`${o.overdue_now} overdue`} />
          <Stat label="Pickups today" value={o.pickups_today} />
          <Stat label="Revenue, 30 days" value={money(o.revenue_30d_paise)} />
          <Stat label="Bikes" value={o.bikes} />
          <Stat label="In maintenance" value={o.bikes_in_maintenance} />
          <Stat label="Failed jobs" value={o.dead_jobs} />
        </div>
      )}
      <Chart title="Net revenue per day (₹)">
        <LineChart data={revenue.data?.map((d) => ({ ...d, rupees: rupees(d.revenue_paise) }))}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="day" fontSize={11} tickFormatter={(d) => d.slice(5)} />
          <YAxis fontSize={12} />
          <Tooltip />
          <Line dataKey="rupees" stroke="#0f4870" strokeWidth={2} dot={false} />
        </LineChart>
      </Chart>
      <div className="card mt-4 overflow-x-auto">
        <h2 className="mb-2 font-semibold">Stations</h2>
        <table className="grid-table">
          <thead>
            <tr><th>Station</th><th>Bikes</th><th>Pickups</th><th>Revenue</th><th>Utilisation</th><th>No-shows</th><th>Late returns</th><th>Cancellations</th></tr>
          </thead>
          <tbody>
            {stations.data?.map((s) => (
              <tr key={s.station_id}>
                <td>{s.name}</td><td>{s.bikes}</td><td>{s.pickups}</td><td>{money(s.revenue_paise)}</td>
                <td>{s.utilisation_pct ?? 0}%</td><td>{s.no_show_pct ?? 0}%</td><td>{s.late_return_pct ?? 0}%</td><td>{s.cancellations}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="card mt-4 overflow-x-auto">
        <h2 className="mb-2 font-semibold">Peak hours (pickups, all time)</h2>
        <table className="text-xs">
          <tbody>
            {DAYS.map((name, d) => (
              <tr key={name}>
                <td className="pr-2 text-slate-500">{name}</td>
                {Array.from({ length: 18 }, (_, i) => i + 5).map((h) => (
                  <td key={h} title={`${name} ${h}:00 · ${cell(d + 1, h)} pickups`} className="h-6 w-7" style={{ background: `rgba(15,72,112,${cell(d + 1, h) / max})` }} />
                ))}
              </tr>
            ))}
            <tr>
              <td />
              {Array.from({ length: 18 }, (_, i) => i + 5).map((h) => <td key={h} className="text-center text-slate-400">{h}</td>)}
            </tr>
          </tbody>
        </table>
      </div>
    </Page>
  );
}
