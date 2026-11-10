import { useState, type ReactNode } from "react";
import { Link, NavLink, Outlet } from "react-router";
import { ApiError } from "./api";
import { useAuth } from "./auth";
import { ChatPanel } from "./chat";

const NAV: Record<string, [string, string][]> = {
  customer: [
    ["/", "Find a bike"],
    ["/bookings", "My bookings"],
    ["/me/analytics", "My analytics"],
    ["/me/dues", "Dues"],
    ["/help", "Help"],
    ["/chat", "Chat"],
  ],
  staff: [
    ["/staff", "Desk"],
    ["/staff/bikes", "Station bikes"],
    ["/admin/map", "Live map"],
  ],
  admin: [
    ["/admin", "Dashboard"],
    ["/admin/map", "Live map"],
    ["/admin/bookings", "Bookings"],
    ["/admin/users", "Users"],
    ["/admin/documents", "Documents"],
    ["/admin/chat-review", "Chat review"],
    ["/admin/system", "System"],
  ],
};

export function Layout() {
  const { user, logout } = useAuth();
  const [chatOpen, setChatOpen] = useState(false);
  return (
    <div className="min-h-screen">
      <header className="bg-ink text-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-5 gap-y-2 px-4 py-3">
          <Link to="/" className="text-lg font-bold tracking-tight">
            Saal <span className="text-mist">Bikes</span>
          </Link>
          <nav className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
            {(NAV[user?.role ?? ""] ?? [["/help", "Help"]]).map(([to, label]) => (
              <NavLink
                key={to}
                to={to}
                end
                className={({ isActive }) => (isActive ? "text-mist" : "text-white/80 hover:text-white")}
              >
                {label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-sm">
            {user ? (
              <>
                <Link to="/me" className="text-white/80 hover:text-white">
                  {user.full_name}
                </Link>
                <button onClick={logout} className="text-white/60 hover:text-white">
                  Log out
                </button>
              </>
            ) : (
              <Link to="/login" className="text-mist">
                Log in
              </Link>
            )}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">
        <Outlet />
      </main>
      {user?.role === "customer" && (
        <>
          <button
            onClick={() => setChatOpen(!chatOpen)}
            className="fixed right-5 bottom-5 rounded-full bg-navy px-5 py-3 font-medium text-white shadow-lg hover:bg-sky"
          >
            {chatOpen ? "Close chat" : "Ask Saal"}
          </button>
          {chatOpen && (
            <div className="fixed right-5 bottom-20 z-[1000] flex h-[70vh] w-[min(420px,calc(100vw-2.5rem))] flex-col rounded-xl border border-slate-200 bg-white shadow-2xl">
              <ChatPanel />
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function Page({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section>
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold text-ink">{title}</h1>
        <div className="ml-auto flex gap-2">{actions}</div>
      </div>
      {children}
    </section>
  );
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof ApiError ? error.message : "Something went wrong.";
  return <p className="my-2 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{message}</p>;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
    </label>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className="text-2xl font-bold text-navy">{value}</div>
      {hint && <div className="text-xs text-slate-500">{hint}</div>}
    </div>
  );
}

export function StatusChip({ status }: { status: string }) {
  const colour: Record<string, string> = {
    pending_payment: "bg-amber-100 text-amber-800",
    confirmed: "bg-mist text-navy",
    picked_up: "bg-sky text-white",
    returned: "bg-emerald-100 text-emerald-800",
    succeeded: "bg-emerald-100 text-emerald-800",
    due: "bg-amber-100 text-amber-800",
  };
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${colour[status] ?? "bg-slate-100 text-slate-600"}`}>
      {status.replace("_", " ")}
    </span>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-8 text-center text-sm text-slate-500">{children}</p>;
}
