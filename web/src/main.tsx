import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router";
import { AuthProvider, RequireRole } from "./auth";
import { ChatPage } from "./chat";
import "./index.css";
import * as A from "./pages/admin";
import { AdminDashboard, MyAnalytics } from "./pages/analytics";
import * as Auth from "./pages/auth";
import * as C from "./pages/customer";
import { ActiveRide } from "./pages/ride";
import { Desk, StationBikes } from "./pages/staff";
import { Layout } from "./ui";

const customer = (el: React.ReactNode) => <RequireRole roles={["customer"]}>{el}</RequireRole>;
const admin = (el: React.ReactNode) => <RequireRole roles={["admin"]}>{el}</RequireRole>;
const staff = (el: React.ReactNode) => <RequireRole roles={["staff"]}>{el}</RequireRole>;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } })}>
      <AuthProvider>
        <BrowserRouter>
          <Routes>
            <Route element={<Layout />}>
              <Route path="/login" element={<Auth.Login />} />
              <Route path="/signup" element={<Auth.Signup />} />
              <Route path="/verify-email" element={<Auth.VerifyEmail />} />
              <Route path="/forgot-password" element={<Auth.ForgotPassword />} />
              <Route path="/reset-password" element={<Auth.ResetPassword />} />
              <Route path="/help" element={<A.HelpIndex />} />
              <Route path="/help/:slug" element={<A.HelpPage />} />
              <Route path="/" element={customer(<C.FindBike />)} />
              <Route path="/bikes/:slug" element={<C.BikeTypePage />} />
              <Route path="/bookings" element={customer(<C.MyBookings />)} />
              <Route path="/bookings/:id" element={customer(<C.BookingDetail />)} />
              <Route path="/bookings/:id/pay" element={customer(<C.PayPage />)} />
              <Route path="/ride/:id" element={customer(<ActiveRide />)} />
              <Route path="/me/analytics" element={customer(<MyAnalytics />)} />
              <Route path="/me/dues" element={customer(<C.Dues />)} />
              <Route path="/chat" element={customer(<ChatPage />)} />
              <Route path="/me" element={<RequireRole roles={["customer", "staff", "admin"]}><Auth.Account /></RequireRole>} />
              <Route path="/staff" element={staff(<Desk />)} />
              <Route path="/staff/bikes" element={staff(<StationBikes />)} />
              <Route path="/admin" element={admin(<AdminDashboard />)} />
              <Route path="/admin/map" element={<RequireRole roles={["admin", "staff"]}><A.LiveMap /></RequireRole>} />
              <Route path="/admin/bookings" element={admin(<A.AdminBookings />)} />
              <Route path="/admin/users" element={admin(<A.Users />)} />
              <Route path="/admin/documents" element={admin(<A.Documents />)} />
              <Route path="/admin/chat-review" element={admin(<A.ChatReview />)} />
              <Route path="/admin/system" element={admin(<A.System />)} />
            </Route>
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
);
