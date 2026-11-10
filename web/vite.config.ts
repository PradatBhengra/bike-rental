import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// In development the API and the tracking service run on the host; Caddy does this routing in Docker.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/track": "http://localhost:8001",
      "/ws": { target: "ws://localhost:8001", ws: true },
    },
  },
  test: { environment: "jsdom" },
});
