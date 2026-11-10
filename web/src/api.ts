import type { components } from "./schema";

export type S = components["schemas"];
export type User = S["UserOut"];
export type Booking = S["BookingOut"];
type Session = S["SessionOut"];

// The access token lives in memory only: a script injected into the page cannot read it from storage.
let token: string | null = null;
let refreshing: Promise<Session | null> | null = null;
export const hooks = { loggedOut: () => {} };

export class ApiError extends Error {
  constructor(
    public status: number,
    public problem: { title?: string; detail?: string; code?: string; [k: string]: unknown },
  ) {
    super(problem.detail || problem.title || `Request failed (${status})`);
  }
}

export function setToken(value: string | null) {
  token = value;
}

export function getToken() {
  return token;
}

/** One refresh at a time: two tabs or two requests reusing the same refresh token would look like theft. */
export function refresh(): Promise<Session | null> {
  refreshing ??= fetch("/api/v1/auth/refresh", { method: "POST" })
    .then(async (r) => {
      const body: Session | null = r.ok ? await r.json() : null;
      token = body?.access_token ?? null;
      return body;
    })
    .finally(() => {
      refreshing = null;
    });
  return refreshing;
}

export async function api<T = any>(path: string, init: RequestInit & { json?: unknown } = {}, retry = true): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  const r = await fetch(`/api/v1${path}`, { ...init, headers, body });
  if (r.status === 401 && retry && !path.startsWith("/auth/")) {
    if (await refresh()) return api(path, init, false);
    hooks.loggedOut();
  }
  if (!r.ok) throw new ApiError(r.status, await r.json().catch(() => ({ title: r.statusText })));
  return r.status === 204 ? (undefined as T) : r.json();
}

const IST = "Asia/Kolkata";

export function money(paise: number | null | undefined) {
  return "₹" + ((paise ?? 0) / 100).toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

export function when(iso: string | null | undefined, withDate = true) {
  if (!iso) return "–";
  return new Date(iso).toLocaleString("en-IN", {
    timeZone: IST,
    ...(withDate ? { weekday: "short", day: "numeric", month: "short" } : {}),
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** The value of a datetime-local input, read as Ranchi time whatever the browser's timezone. */
export function fromLocalInput(value: string) {
  return `${value}:00+05:30`;
}

export function toLocalInput(date: Date) {
  const ist = new Date(date.getTime() + (330 + date.getTimezoneOffset()) * 60000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${ist.getFullYear()}-${pad(ist.getMonth() + 1)}-${pad(ist.getDate())}T${pad(ist.getHours())}:${pad(ist.getMinutes())}`;
}

export function newKey() {
  return crypto.randomUUID();
}
