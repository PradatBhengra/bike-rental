import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { CircleMarker, Tooltip } from "react-leaflet";
import Markdown from "react-markdown";
import { Link, useParams } from "react-router";
import { api, money, when } from "../api";
import { BaseMap, flip, openLive, PathLine, StationDot } from "../map";
import { ErrorText, Page, StatusChip } from "../ui";

export function LiveMap() {
  const snap = useQuery({ queryKey: ["fleet"], queryFn: () => api("/admin/fleet/live"), refetchInterval: 30000 });
  const [pos, setPos] = useState<Record<number, [number, number]>>({});
  useEffect(() => {
    let close = () => {};
    openLive("fleet", (m) => {
      if (m.type === "point") setPos((p) => ({ ...p, [m.booking_id]: [m.lat, m.lng] }));
      else snap.refetch();
    }).then((c) => (close = c));
    return () => close();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const rides = snap.data?.rides ?? [];
  return (
    <Page title={`Live map · ${rides.length} rides`}>
      <BaseMap height="h-[70vh]">
        {snap.data?.stations.map((s: any) => <StationDot key={s.id} s={s} />)}
        {rides.map((r: any) => {
          const at = pos[r.booking_id] ?? (r.lat ? [r.lat, r.lng] : null);
          return at ? (
            <CircleMarker key={r.booking_id} center={at} radius={7} pathOptions={{ color: r.overdue ? "#dc2626" : "#1f2a30", fillColor: r.overdue ? "#dc2626" : "#3581bc", fillOpacity: 1 }}>
              <Tooltip>{r.code} · {r.bike_code} · {r.rider}<br />due {when(r.due_back, false)}</Tooltip>
            </CircleMarker>
          ) : null;
        })}
      </BaseMap>
    </Page>
  );
}

export function AdminBookings() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const list = useQuery({ queryKey: ["admin-bookings", q], queryFn: () => api(`/admin/bookings?limit=50${q ? `&q=${encodeURIComponent(q)}` : ""}`) });
  const [error, setError] = useState<unknown>(null);
  const change = async (id: number) => {
    const status = prompt("New status (confirmed, picked_up, returned, cancelled, expired, no_show)");
    const reason = status && prompt("Reason (kept in the audit log)");
    if (!status || !reason) return;
    try {
      await api(`/admin/bookings/${id}/status`, { method: "POST", json: { status, reason } });
      qc.invalidateQueries({ queryKey: ["admin-bookings"] });
    } catch (e) {
      setError(e);
    }
  };
  return (
    <Page title="Bookings" actions={<input className="input" placeholder="Code, email or name" value={q} onChange={(e) => setQ(e.target.value)} />}>
      <ErrorText error={error} />
      <div className="card overflow-x-auto">
        <table className="grid-table">
          <thead><tr><th>Code</th><th>Status</th><th>Customer</th><th>Bike</th><th>Station</th><th>Time</th><th>Price</th><th /></tr></thead>
          <tbody>
            {list.data?.items.map((b: any) => (
              <tr key={b.id}>
                <td className="font-mono">{b.code}</td><td><StatusChip status={b.status} /></td><td>{b.customer_name}</td>
                <td>{b.bike_type_name}</td><td>{b.station_name}</td><td>{when(b.start_at)}</td><td>{money(b.quoted_paise)}</td>
                <td><button className="btn-ghost" onClick={() => change(b.id)}>Change status</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Page>
  );
}

export function Users() {
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api("/admin/users") });
  const stations = useQuery({ queryKey: ["stations"], queryFn: () => api("/stations") });
  const [error, setError] = useState<unknown>(null);
  const patch = (id: number, json: object) => api(`/admin/users/${id}`, { method: "PATCH", json }).then(() => qc.invalidateQueries({ queryKey: ["users"] }), setError);
  return (
    <Page title="Users">
      <ErrorText error={error} />
      <div className="card overflow-x-auto">
        <table className="grid-table">
          <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Station</th><th /></tr></thead>
          <tbody>
            {users.data?.map((u: any) => (
              <tr key={u.id}>
                <td>{u.full_name}</td><td>{u.email}</td>
                <td>
                  <select className="input" value={u.role} onChange={(e) => patch(u.id, { role: e.target.value, staff_station_id: e.target.value === "staff" ? stations.data?.[0]?.id : null })}>
                    {["customer", "staff", "admin"].map((r) => <option key={r}>{r}</option>)}
                  </select>
                </td>
                <td>
                  {u.role === "staff" && (
                    <select className="input" value={u.staff_station_id ?? ""} onChange={(e) => patch(u.id, { staff_station_id: +e.target.value })}>
                      {stations.data?.map((s: any) => <option key={s.id} value={s.id}>{s.name}</option>)}
                    </select>
                  )}
                </td>
                <td><button className="btn-ghost" onClick={() => patch(u.id, { blocked: !u.blocked_at })}>{u.blocked_at ? "Unblock" : "Block"}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Page>
  );
}

export function Documents() {
  const qc = useQueryClient();
  const docs = useQuery({ queryKey: ["admin-docs"], queryFn: () => api("/admin/documents"), refetchInterval: 5000 });
  const [error, setError] = useState<unknown>(null);
  const upload = async (file: File) => {
    const body = new FormData();
    body.append("file", file);
    try {
      await api("/admin/documents", { method: "POST", body });
      qc.invalidateQueries({ queryKey: ["admin-docs"] });
    } catch (e) {
      setError(e);
    }
  };
  return (
    <Page title="Documents" actions={<input type="file" accept=".md" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />}>
      <ErrorText error={error} />
      <div className="card overflow-x-auto">
        <table className="grid-table">
          <thead><tr><th>Title</th><th>Type</th><th>Active</th><th>Latest</th><th>Chunks</th><th /></tr></thead>
          <tbody>
            {docs.data?.map((d: any) => (
              <tr key={d.id}>
                <td><Link className="text-sky" to={`/help/${d.slug}`}>{d.title}</Link></td><td>{d.doc_type}</td><td>v{d.active_version}</td>
                <td>v{d.latest_version} · {d.latest_status}</td><td>{d.chunks}</td>
                <td><button className="btn-ghost" onClick={() => api(`/admin/documents/${d.id}/reindex`, { method: "POST" })}>Re-index</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Page>
  );
}

export function ChatReview() {
  const qc = useQueryClient();
  const flagged = useQuery({ queryKey: ["flagged"], queryFn: () => api("/admin/flagged") });
  const runs = useQuery({ queryKey: ["eval-runs"], queryFn: () => api("/admin/eval-runs") });
  const review = (id: number, status: string) => api(`/admin/flagged/${id}`, { method: "PATCH", json: { status } }).then(() => qc.invalidateQueries({ queryKey: ["flagged"] }));
  return (
    <Page title="Chat review">
      <div className="space-y-3">
        {flagged.data?.length === 0 && <p className="text-sm text-slate-500">No open flags.</p>}
        {flagged.data?.map((f: any) => (
          <div key={f.id} className="card text-sm">
            <span className="chip">{f.reason}</span> <span className="text-slate-500">{when(f.created_at)} · {f.model}</span>
            <p className="mt-2 font-medium">Q: {f.question}</p>
            <p className="mt-1 whitespace-pre-wrap">A: {f.answer}</p>
            <p className="mt-1 text-xs text-slate-500">Tools: {f.tools.map((t: any) => `${t.tool} (${t.status})`).join(", ") || "none"}</p>
            <div className="mt-2 flex gap-2">
              <button className="btn-ghost" onClick={() => review(f.id, "reviewed")}>Reviewed</button>
              <button className="btn-ghost" onClick={() => review(f.id, "dismissed")}>Dismiss</button>
            </div>
          </div>
        ))}
      </div>
      <h2 className="mt-6 mb-2 font-semibold">Evaluation runs</h2>
      <div className="card text-sm">
        {runs.data?.map((r: any) => <div key={r.id}>{when(r.started_at)} · {r.kind} · {r.model} · {JSON.stringify(r.metrics)}</div>)}
        {runs.data?.length === 0 && <p className="text-slate-500">Run `python -m app.eval retrieval` to add one.</p>}
      </div>
    </Page>
  );
}

export function System() {
  const qc = useQueryClient();
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => api("/admin/jobs?status=dead") });
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api("/admin/settings") });
  const [error, setError] = useState<unknown>(null);
  const edit = async (key: string, value: unknown) => {
    const next = prompt(`New value for ${key} (JSON)`, JSON.stringify(value));
    if (next == null) return;
    try {
      await api(`/admin/settings/${key}`, { method: "PUT", json: { value: JSON.parse(next) } });
      qc.invalidateQueries({ queryKey: ["settings"] });
    } catch (e) {
      setError(e);
    }
  };
  return (
    <Page title="System">
      <ErrorText error={error} />
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="card text-sm">
          <h2 className="mb-2 font-semibold">Settings</h2>
          {settings.data?.map((s: any) => (
            <div key={s.key} className="flex items-center gap-2 border-b border-slate-100 py-1">
              <span className="font-mono">{s.key}</span><span className="ml-auto truncate">{JSON.stringify(s.value)}</span>
              <button className="text-sky" onClick={() => edit(s.key, s.value)}>Edit</button>
            </div>
          ))}
        </div>
        <div className="card text-sm">
          <h2 className="mb-2 font-semibold">Failed jobs · {JSON.stringify(jobs.data?.counts ?? {})}</h2>
          {jobs.data?.items.map((j: any) => (
            <div key={j.id} className="border-b border-slate-100 py-1">
              {j.kind} #{j.id} <button className="text-sky" onClick={() => api(`/admin/jobs/${j.id}/retry`, { method: "POST" }).then(() => jobs.refetch())}>Retry</button>
              <pre className="overflow-x-auto text-xs text-slate-500">{j.last_error?.slice(-300)}</pre>
            </div>
          ))}
        </div>
      </div>
    </Page>
  );
}

export function HelpIndex() {
  const docs = useQuery({ queryKey: ["docs"], queryFn: () => api("/docs") });
  return (
    <Page title="Help">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {docs.data?.map((d: any) => (
          <Link key={d.slug} to={`/help/${d.slug}`} className="card hover:border-sky">
            <div className="font-semibold">{d.title}</div>
            <div className="text-xs text-slate-500">{d.doc_type.replace("_", " ")}</div>
          </Link>
        ))}
      </div>
    </Page>
  );
}

export function HelpPage() {
  const { slug } = useParams();
  const doc = useQuery({ queryKey: ["doc", slug], queryFn: () => api(`/docs/${slug}`) });
  useEffect(() => {
    if (doc.data && location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
  }, [doc.data]);
  if (!doc.data) return <ErrorText error={doc.error} />;
  const route = doc.data.route_geojson?.coordinates.map(flip);
  return (
    <Page title={doc.data.title}>
      {route && <div className="mb-4"><BaseMap center={route[0]} zoom={14} height="h-72"><PathLine coords={route} /></BaseMap></div>}
      <div className="card max-w-3xl space-y-5">
        {doc.data.sections.map((s: any) => (
          <section key={s.anchor} id={s.anchor} className="scroll-mt-4 target:rounded target:bg-mist/40">
            {s.section_no !== "0" && <h2 className="font-semibold text-navy">§{s.section_no} {s.heading}</h2>}
            <div className="prose-sm space-y-2 text-sm leading-relaxed [&_ul]:list-disc [&_ul]:pl-5"><Markdown>{s.body}</Markdown></div>
          </section>
        ))}
      </div>
    </Page>
  );
}
