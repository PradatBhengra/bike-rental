import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { api, fromLocalInput, money, newKey, toLocalInput, when, type Booking, type S } from "../api";
import { BaseMap, FitLine, flip, PathLine, StationDot } from "../map";
import { Empty, ErrorText, Field, Page, StatusChip } from "../ui";

type Station = S["StationOut"];
type BikeType = S["BikeTypeOut"];

export function FindBike() {
  const navigate = useNavigate();
  const [near, setNear] = useState<string | null>(null);
  const stations = useQuery({ queryKey: ["stations", near], queryFn: () => api<Station[]>(`/stations${near ? `?near=${near}` : ""}`) });
  const types = useQuery({ queryKey: ["bike-types"], queryFn: () => api<BikeType[]>("/bike-types") });
  const tomorrow9 = new Date(Date.now() + 86400000);
  tomorrow9.setHours(9, 0, 0, 0);
  const [form, setForm] = useState({ station_id: 0, bike_type_id: 0, start: toLocalInput(tomorrow9), end: toLocalInput(new Date(tomorrow9.getTime() + 2 * 3600000)) });
  const [error, setError] = useState<unknown>(null);
  const ready = form.station_id && form.bike_type_id && form.start && form.end;
  const quote = useQuery({
    queryKey: ["availability", form],
    enabled: !!ready,
    retry: false,
    queryFn: () =>
      api<S["AvailabilityOut"]>(
        `/availability?station_id=${form.station_id}&bike_type_id=${form.bike_type_id}&start=${encodeURIComponent(fromLocalInput(form.start))}&end=${encodeURIComponent(fromLocalInput(form.end))}`,
      ),
  });
  const book = async () => {
    setError(null);
    try {
      const booking = await api<Booking>("/bookings", {
        method: "POST",
        headers: { "Idempotency-Key": newKey() },
        json: { ...form, start: fromLocalInput(form.start), end: fromLocalInput(form.end) },
      });
      navigate(`/bookings/${booking.id}/pay`);
    } catch (e) {
      setError(e);
    }
  };
  const locate = () => navigator.geolocation?.getCurrentPosition((p) => setNear(`${p.coords.latitude},${p.coords.longitude}`));
  return (
    <Page title="Find a bike" actions={<button className="btn-ghost" onClick={locate}>Nearest to me</button>}>
      <div className="grid gap-4 lg:grid-cols-[1fr_360px]">
        <div className="space-y-3">
          <BaseMap>
            {stations.data?.map((s) => (
              <StationDot key={s.id} s={s} active={s.id === form.station_id} onClick={() => setForm({ ...form, station_id: s.id })} />
            ))}
          </BaseMap>
          <div className="grid gap-2 sm:grid-cols-2">
            {stations.data?.map((s) => (
              <button key={s.id} onClick={() => setForm({ ...form, station_id: s.id })} className={`card text-left ${s.id === form.station_id ? "border-sky ring-2 ring-mist" : ""}`}>
                <div className="font-semibold">{s.name}</div>
                <div className="text-xs text-slate-500">
                  {s.address} · {s.opens_at.slice(0, 5)}–{s.closes_at.slice(0, 5)}
                  {s.distance_m != null && ` · ${(s.distance_m / 1000).toFixed(1)} km`}
                </div>
                <div className="mt-1 text-sm text-navy">{s.free_bikes} free now</div>
              </button>
            ))}
          </div>
        </div>
        <div className="card h-fit space-y-3 lg:sticky lg:top-4">
          <Field label="Station">
            <select className="input" value={form.station_id} onChange={(e) => setForm({ ...form, station_id: +e.target.value })}>
              <option value={0}>Choose a station</option>
              {stations.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </Field>
          <Field label="Bike type">
            <select className="input" value={form.bike_type_id} onChange={(e) => setForm({ ...form, bike_type_id: +e.target.value })}>
              <option value={0}>Choose a bike</option>
              {types.data?.map((t) => <option key={t.id} value={t.id}>{t.name} · {money(t.hourly_paise)}/h</option>)}
            </select>
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Start (Ranchi time)">
              <input className="input" type="datetime-local" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} />
            </Field>
            <Field label="End">
              <input className="input" type="datetime-local" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} />
            </Field>
          </div>
          {quote.data && (
            <div className="rounded-lg bg-mist/40 p-3 text-sm">
              {quote.data.quote.lines.map((l) => (
                <div key={l.label} className="flex justify-between">
                  <span>{l.label}{l.capped && " (daily cap)"}</span>
                  <span>{money(l.amount_paise)}</span>
                </div>
              ))}
              <div className="mt-1 flex justify-between border-t border-navy/20 pt-1 font-semibold">
                <span>Total</span>
                <span>{money(quote.data.quote.total_paise)}</span>
              </div>
              <div className="mt-1 text-xs text-slate-600">{quote.data.free} bikes free for these times</div>
            </div>
          )}
          <ErrorText error={quote.error || error} />
          <button className="btn w-full" disabled={!quote.data?.free} onClick={book}>
            Book and pay
          </button>
          <p className="text-xs text-slate-500">The bike is held for 15 minutes while you pay.</p>
        </div>
      </div>
      <h2 className="mt-8 mb-3 text-lg font-semibold">Our bikes</h2>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {types.data?.map((t) => (
          <Link key={t.id} to={`/bikes/${t.slug}`} className="card hover:border-sky">
            <div className="font-semibold">{t.name}</div>
            <div className="text-sm text-slate-600">{t.description}</div>
            <div className="mt-2 text-sm text-navy">{money(t.hourly_paise)}/hour · {money(t.daily_cap_paise)}/day max</div>
          </Link>
        ))}
      </div>
    </Page>
  );
}

export function BikeTypePage() {
  const { slug } = useParams();
  const t = useQuery({ queryKey: ["bike-type", slug], queryFn: () => api<BikeType>(`/bike-types/${slug}`) });
  if (!t.data) return <ErrorText error={t.error} />;
  return (
    <Page title={t.data.name}>
      <div className="card max-w-xl space-y-2">
        <p>{t.data.description}</p>
        <dl className="grid grid-cols-2 gap-1 text-sm">
          {Object.entries(t.data.specs).map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-slate-500 capitalize">{k.replaceAll("_", " ")}</dt>
              <dd>{String(v)}</dd>
            </div>
          ))}
          <dt className="text-slate-500">Price</dt>
          <dd>{money(t.data.hourly_paise)} an hour, at most {money(t.data.daily_cap_paise)} a day</dd>
          <dt className="text-slate-500">Late fee</dt>
          <dd>{money(t.data.late_fee_per_hour_paise)} per started hour</dd>
        </dl>
        {t.data.manual_slug && <Link to={`/help/${t.data.manual_slug}`} className="text-sky">Read the manual</Link>}
      </div>
    </Page>
  );
}

export function MyBookings() {
  const q = useInfiniteQuery({
    queryKey: ["bookings"],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api<S["BookingPage"]>(`/bookings?limit=15${pageParam ? `&cursor=${pageParam}` : ""}`),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = q.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <Page title="My bookings">
      {items.length === 0 && !q.isLoading && <Empty>No bookings yet. <Link to="/" className="text-sky">Find a bike</Link>.</Empty>}
      <div className="space-y-2">
        {items.map((b) => (
          <Link key={b.id} to={`/bookings/${b.id}`} className="card flex flex-wrap items-center gap-3 hover:border-sky">
            <span className="font-mono text-sm">{b.code}</span>
            <StatusChip status={b.status} />
            {b.overdue && <span className="chip bg-red-100 text-red-700">overdue</span>}
            <span className="text-sm">{b.bike_type_name} · {b.station_name}</span>
            <span className="ml-auto text-sm text-slate-600">{when(b.start_at)} → {when(b.end_at, false)}</span>
            <span className="font-semibold">{money(b.quoted_paise)}</span>
          </Link>
        ))}
      </div>
      {q.hasNextPage && <button className="btn-ghost mt-3" onClick={() => q.fetchNextPage()}>Load more</button>}
    </Page>
  );
}

export function BookingDetail() {
  const { id } = useParams();
  const qc = useQueryClient();
  const b = useQuery({ queryKey: ["booking", id], queryFn: () => api<S["BookingDetailOut"]>(`/bookings/${id}`) });
  const path = useQuery({
    queryKey: ["path", id],
    enabled: b.data?.status === "returned" || b.data?.status === "picked_up",
    queryFn: () => api(`/bookings/${id}/path`),
  });
  const [quote, setQuote] = useState<S["CancelQuoteOut"] | null>(null);
  const [newEnd, setNewEnd] = useState("");
  const [error, setError] = useState<unknown>(null);
  const reload = () => qc.invalidateQueries({ queryKey: ["booking", id] });
  const run = (f: () => Promise<unknown>) => async () => {
    setError(null);
    try {
      await f();
      setQuote(null);
      reload();
    } catch (e) {
      setError(e);
    }
  };
  if (!b.data) return <ErrorText error={b.error} />;
  const d = b.data;
  const coords: [number, number][] = (path.data?.coordinates ?? []).map(flip);
  return (
    <Page title={`Booking ${d.code}`} actions={<StatusChip status={d.status} />}>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="card space-y-1 text-sm">
          <div className="text-lg font-semibold">{d.bike_type_name} · {d.station_name}</div>
          <div>Booked: {when(d.start_at)} → {when(d.end_at)}</div>
          {d.picked_up_at && <div>Collected: {when(d.picked_up_at)} (bike {d.bike_code})</div>}
          {d.returned_at && <div>Returned: {when(d.returned_at)}{d.late_minutes ? `, ${d.late_minutes} min late` : ""}</div>}
          <div>Price: {money(d.quoted_paise)}</div>
          <div className="flex flex-wrap gap-2 pt-2">
            {d.status === "pending_payment" && <Link className="btn" to={`/bookings/${d.id}/pay`}>Pay now</Link>}
            {d.status === "picked_up" && <Link className="btn" to={`/ride/${d.id}`}>Open the ride</Link>}
            {["pending_payment", "confirmed"].includes(d.status) && (
              <button className="btn-ghost" onClick={run(async () => setQuote(await api(`/bookings/${d.id}/cancel-quote`)))}>Cancel…</button>
            )}
            {d.has_receipt && (
              <button className="btn-ghost" onClick={() => fetchPdf(d.id)}>Receipt PDF</button>
            )}
          </div>
          {quote && (
            <div className="mt-2 rounded-lg bg-amber-50 p-3">
              <p>{quote.rule}</p>
              <p className="font-semibold">You get back {money(quote.refund_paise)} of {money(quote.paid_paise)}.</p>
              <button className="btn mt-2" onClick={run(() => api(`/bookings/${d.id}/cancel`, { method: "POST" }))}>Confirm cancellation</button>
            </div>
          )}
          {["confirmed", "picked_up"].includes(d.status) && (
            <div className="flex items-end gap-2 pt-2">
              <Field label="Extend to">
                <input className="input" type="datetime-local" value={newEnd} onChange={(e) => setNewEnd(e.target.value)} />
              </Field>
              <button className="btn-ghost" disabled={!newEnd} onClick={run(() => api(`/bookings/${d.id}/extend`, { method: "POST", json: { end: fromLocalInput(newEnd) } }))}>
                Extend
              </button>
            </div>
          )}
          <ErrorText error={error} />
        </div>
        <div className="card text-sm">
          <h2 className="mb-2 font-semibold">Payments</h2>
          {d.payments.map((p) => (
            <div key={p.id} className="flex justify-between border-b border-slate-100 py-1">
              <span>{p.description || p.kind} <StatusChip status={p.status} /></span>
              <span>{money(p.amount_paise)}{p.refunded_paise > 0 && ` (−${money(p.refunded_paise)} refunded)`}</span>
            </div>
          ))}
          <h2 className="mt-4 mb-2 font-semibold">History</h2>
          {d.history.map((h, i) => (
            <div key={i} className="text-slate-600">
              {when(h.at)}: {h.from_status ? `${h.from_status} → ` : ""}{h.to_status}{h.reason ? ` (${h.reason})` : ""}
            </div>
          ))}
        </div>
      </div>
      {coords.length > 1 && (
        <div className="card mt-4">
          <h2 className="mb-2 font-semibold">Your route · {((path.data?.distance_m ?? 0) / 1000).toFixed(1)} km</h2>
          <BaseMap height="h-80">
            <PathLine coords={coords} />
            <FitLine coords={coords} />
          </BaseMap>
        </div>
      )}
    </Page>
  );
}

async function fetchPdf(id: number) {
  const { getToken } = await import("../api");
  const r = await fetch(`/api/v1/bookings/${id}/receipt.pdf`, { headers: { Authorization: `Bearer ${getToken()}` } });
  if (r.ok) window.open(URL.createObjectURL(await r.blob()));
}

declare global {
  interface Window {
    Razorpay?: any;
  }
}

/** Opens Razorpay Checkout, or in fake mode (no key configured) sends the test payment straight through the webhook path. */
export async function payCheckout(checkout: S["CheckoutOut"]) {
  if (checkout.fake) {
    await api(`/payments/fake/${checkout.order_id}/pay`, { method: "POST" });
    return;
  }
  if (!window.Razorpay) {
    await new Promise((ok, fail) => {
      const s = document.createElement("script");
      s.src = "https://checkout.razorpay.com/v1/checkout.js";
      s.onload = ok;
      s.onerror = fail;
      document.body.appendChild(s);
    });
  }
  await new Promise<void>((resolve, reject) => {
    new window.Razorpay({
      key: checkout.key_id,
      order_id: checkout.order_id,
      amount: checkout.amount_paise,
      currency: checkout.currency,
      name: "Saal Bikes",
      description: checkout.description,
      theme: { color: "#0f4870" },
      handler: () => resolve(), // only means "paid"; the webhook is what confirms the booking
      modal: { ondismiss: () => reject(new Error("Payment window closed.")) },
    }).open();
  });
}

export function PayPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const b = useQuery({ queryKey: ["booking", id], queryFn: () => api<S["BookingDetailOut"]>(`/bookings/${id}`), refetchInterval: (q) => (q.state.data?.status === "pending_payment" ? 2000 : false) });
  const [now, setNow] = useState(Date.now());
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (b.data?.status === "confirmed") navigate(`/bookings/${id}`);
  }, [b.data?.status, id, navigate]);
  if (!b.data) return <ErrorText error={b.error} />;
  const left = Math.max(0, Math.floor((new Date(b.data.hold_expires_at ?? 0).getTime() - now) / 1000));
  const pay = async () => {
    setError(null);
    try {
      await payCheckout(await api(`/bookings/${id}/checkout`, { method: "POST" }));
      setWaiting(true);
    } catch (e) {
      setError(e);
    }
  };
  return (
    <Page title="Pay for your booking">
      <div className="card max-w-md space-y-3">
        <div className="font-semibold">{b.data.bike_type_name} at {b.data.station_name}</div>
        <div className="text-sm">{when(b.data.start_at)} → {when(b.data.end_at)}</div>
        {(b.data.price_snapshot.lines as any[]).map((l: any) => (
          <div key={l.label} className="flex justify-between text-sm">
            <span>{l.label}</span>
            <span>{money(l.amount_paise)}</span>
          </div>
        ))}
        <div className="flex justify-between font-semibold">
          <span>Total</span>
          <span>{money(b.data.quoted_paise)}</span>
        </div>
        {b.data.status === "pending_payment" ? (
          <>
            <p className="text-sm text-amber-700">Bike held for {Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")}</p>
            <button className="btn w-full" disabled={!left || waiting} onClick={pay}>
              {waiting ? "Confirming your payment…" : `Pay ${money(b.data.quoted_paise)}`}
            </button>
          </>
        ) : (
          <p>This booking is {b.data.status.replace("_", " ")}.</p>
        )}
        <ErrorText error={error} />
        <p className="text-xs text-slate-500">Test mode: use Razorpay's test cards or UPI success@razorpay. No real money moves.</p>
      </div>
    </Page>
  );
}

export function Dues() {
  const dues = useQuery({ queryKey: ["dues"], queryFn: () => api<S["PaymentOut"][]>("/me/dues"), refetchInterval: 3000 });
  const [error, setError] = useState<unknown>(null);
  const pay = async (pid: number) => {
    setError(null);
    try {
      await payCheckout(await api(`/payments/${pid}/checkout`, { method: "POST" }));
    } catch (e) {
      setError(e);
    }
  };
  return (
    <Page title="Dues">
      <p className="mb-3 text-sm text-slate-600">Late fees and extensions are never charged automatically. Pay them here; new bookings wait until they are paid.</p>
      <ErrorText error={error} />
      {dues.data?.length === 0 && <Empty>Nothing to pay.</Empty>}
      <div className="space-y-2">
        {dues.data?.map((d) => (
          <div key={d.id} className="card flex items-center gap-3">
            <span>{d.description}</span>
            <span className="ml-auto font-semibold">{money(d.amount_paise)}</span>
            <button className="btn" onClick={() => pay(d.id)}>Pay</button>
          </div>
        ))}
      </div>
    </Page>
  );
}
