import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { api } from "../api";
import { home, useAuth } from "../auth";
import { ErrorText, Field, Page } from "../ui";

function Box({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mx-auto mt-6 max-w-sm">
      <div className="card space-y-4 p-6">
        <h1 className="text-xl font-bold">{title}</h1>
        {children}
      </div>
    </div>
  );
}

function useSubmit<T>(run: () => Promise<T>) {
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await run();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return { submit, error, busy };
}

export function Login() {
  const { start } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const { submit, error, busy } = useSubmit(async () => {
    const session = await api("/auth/login", { method: "POST", json: { email, password } });
    start(session);
    navigate(params.get("next") || home(session.user.role));
  });
  return (
    <Box title="Log in">
      <form onSubmit={submit} className="space-y-3">
        <Field label="Email">
          <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </Field>
        <Field label="Password">
          <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </Field>
        <ErrorText error={error} />
        <button className="btn w-full" disabled={busy}>
          Log in
        </button>
      </form>
      <div className="flex justify-between text-sm">
        <Link to="/signup" className="text-sky">
          Create an account
        </Link>
        <Link to="/forgot-password" className="text-sky">
          Forgot password?
        </Link>
      </div>
      <p className="text-xs text-slate-500">Demo: asha@example.com / rider12345 · admin@saalbikes.test / admin12345</p>
    </Box>
  );
}

export function Signup() {
  const { start } = useAuth();
  const navigate = useNavigate();
  const [form, setForm] = useState({ full_name: "", email: "", password: "" });
  const { submit, error, busy } = useSubmit(async () => {
    start(await api("/auth/signup", { method: "POST", json: form }));
    navigate("/");
  });
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });
  return (
    <Box title="Create an account">
      <form onSubmit={submit} className="space-y-3">
        <Field label="Full name">
          <input className="input" value={form.full_name} onChange={set("full_name")} required />
        </Field>
        <Field label="Email">
          <input className="input" type="email" value={form.email} onChange={set("email")} required />
        </Field>
        <Field label="Password (8 or more characters)">
          <input className="input" type="password" value={form.password} onChange={set("password")} minLength={8} required />
        </Field>
        <ErrorText error={error} />
        <button className="btn w-full" disabled={busy}>
          Sign up
        </button>
      </form>
      <p className="text-xs text-slate-500">We email a confirmation link. In development it lands in Mailpit at /mail.</p>
    </Box>
  );
}

export function VerifyEmail() {
  const [params] = useSearchParams();
  const [state, setState] = useState<"working" | "done" | unknown>("working");
  useEffect(() => {
    api("/auth/verify-email", { method: "POST", json: { token: params.get("token") } }).then(
      () => setState("done"),
      (e) => setState(e),
    );
  }, [params]);
  return (
    <Box title="Confirm your email">
      {state === "working" ? <p>Checking the link…</p> : state === "done" ? <p>Your email is confirmed.</p> : <ErrorText error={state} />}
      <Link to="/" className="text-sky">
        Go to Saal Bikes
      </Link>
    </Box>
  );
}

export function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const { submit, error, busy } = useSubmit(async () => {
    await api("/auth/password-reset", { method: "POST", json: { email } });
    setSent(true);
  });
  return (
    <Box title="Reset your password">
      {sent ? (
        <p className="text-sm">If that email has an account, a reset link is on its way. It works for 1 hour.</p>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <Field label="Email">
            <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </Field>
          <ErrorText error={error} />
          <button className="btn w-full" disabled={busy}>
            Send reset link
          </button>
        </form>
      )}
    </Box>
  );
}

export function ResetPassword() {
  const [params] = useSearchParams();
  const [password, setPassword] = useState("");
  const [done, setDone] = useState(false);
  const { submit, error, busy } = useSubmit(async () => {
    await api("/auth/password-reset/confirm", { method: "POST", json: { token: params.get("token"), password } });
    setDone(true);
  });
  return (
    <Box title="Choose a new password">
      {done ? (
        <Link to="/login" className="text-sky">
          Password changed. Log in.
        </Link>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <Field label="New password">
            <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} minLength={8} required />
          </Field>
          <ErrorText error={error} />
          <button className="btn w-full" disabled={busy}>
            Save
          </button>
        </form>
      )}
    </Box>
  );
}

export function Account() {
  const { user, setUser } = useAuth();
  const [profile, setProfile] = useState({ full_name: user?.full_name ?? "", phone: user?.phone ?? "" });
  const [pw, setPw] = useState({ current_password: "", new_password: "" });
  const [note, setNote] = useState("");
  const save = useSubmit(async () => {
    setUser(await api("/me", { method: "PATCH", json: { full_name: profile.full_name, phone: profile.phone || null } }));
    setNote("Saved.");
  });
  const change = useSubmit(async () => {
    await api("/me/password", { method: "POST", json: pw });
    setPw({ current_password: "", new_password: "" });
    setNote("Password changed.");
  });
  if (!user) return null;
  const toggleConsent = async () => setUser(await api("/me", { method: "PATCH", json: { tracking_consent: !user.tracking_consent } }));
  return (
    <Page title="Account">
      {note && <p className="mb-3 text-sm text-emerald-700">{note}</p>}
      <div className="grid gap-4 md:grid-cols-2">
        <form onSubmit={save.submit} className="card space-y-3">
          <h2 className="font-semibold">Profile</h2>
          <p className="text-sm text-slate-500">
            {user.email} · {user.role}
            {!user.email_verified_at && (
              <button type="button" className="ml-2 text-sky" onClick={() => api("/auth/resend-verification", { method: "POST" }).then(() => setNote("Link sent."))}>
                Resend confirmation link
              </button>
            )}
          </p>
          <Field label="Full name">
            <input className="input" value={profile.full_name} onChange={(e) => setProfile({ ...profile, full_name: e.target.value })} />
          </Field>
          <Field label="Phone">
            <input className="input" value={profile.phone} onChange={(e) => setProfile({ ...profile, phone: e.target.value })} />
          </Field>
          <ErrorText error={save.error} />
          <button className="btn">Save</button>
        </form>
        <form onSubmit={change.submit} className="card space-y-3">
          <h2 className="font-semibold">Password</h2>
          <Field label="Current password">
            <input className="input" type="password" value={pw.current_password} onChange={(e) => setPw({ ...pw, current_password: e.target.value })} />
          </Field>
          <Field label="New password">
            <input className="input" type="password" value={pw.new_password} onChange={(e) => setPw({ ...pw, new_password: e.target.value })} minLength={8} />
          </Field>
          <ErrorText error={change.error} />
          <button className="btn">Change password</button>
        </form>
        {user.role === "customer" && (
          <div className="card space-y-3">
            <h2 className="font-semibold">Ride tracking</h2>
            <p className="text-sm text-slate-600">
              When on, the ride page records your phone's location during an active ride only. See{" "}
              <Link to="/help/privacy-tracking" className="text-sky">
                Privacy and ride tracking
              </Link>
              .
            </p>
            <div className="flex gap-2">
              <button className="btn" onClick={toggleConsent}>
                {user.tracking_consent ? "Turn tracking off" : "Turn tracking on"}
              </button>
              <button className="btn-ghost" onClick={() => api("/me/ride-paths", { method: "DELETE" }).then(() => setNote("Your stored routes are deleted."))}>
                Delete my stored routes
              </button>
            </div>
          </div>
        )}
      </div>
    </Page>
  );
}
