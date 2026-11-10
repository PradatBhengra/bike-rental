import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router";
import { api, hooks, refresh, setToken, type S, type User } from "./api";

type Auth = {
  user: User | null;
  ready: boolean;
  start: (session: S["SessionOut"]) => void;
  setUser: (user: User) => void;
  logout: () => Promise<void>;
};

const AuthContext = createContext<Auth>(null!);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    hooks.loggedOut = () => setUser(null);
    refresh() // the refresh cookie survives a reload; the access token does not
      .then((session) => setUser(session?.user ?? null))
      .finally(() => setReady(true));
  }, []);

  const start = useCallback((session: S["SessionOut"]) => {
    setToken(session.access_token);
    setUser(session.user);
  }, []);

  const logout = useCallback(async () => {
    await api("/auth/logout", { method: "POST" }).catch(() => {});
    setToken(null);
    setUser(null);
  }, []);

  return <AuthContext.Provider value={{ user, ready, start, setUser, logout }}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  return useContext(AuthContext);
}

export function home(role: string | undefined) {
  return role === "admin" ? "/admin" : role === "staff" ? "/staff" : "/";
}

/** The server enforces every permission; this only keeps people out of pages they cannot use. */
export function RequireRole({ roles, children }: { roles: string[]; children: ReactNode }) {
  const { user, ready } = useAuth();
  const location = useLocation();
  if (!ready) return null;
  if (!user) return <Navigate to={`/login?next=${encodeURIComponent(location.pathname)}`} replace />;
  if (!roles.includes(user.role)) return <Navigate to={home(user.role)} replace />;
  return <>{children}</>;
}
