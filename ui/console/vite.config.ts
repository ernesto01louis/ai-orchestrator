import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Vite config for the operator console.
//
// VITE_API_BASE — base URL for FastAPI (default http://localhost:8000).
// VITE_WS_URL   — full WebSocket URL (default ws://localhost:8000/ws).
// VITE_USE_MOCKS — "1" to bypass the backend and use src/lib/mocks.ts.
//
// During dev we proxy /api → backend so cookies / SameSite work cleanly.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_BASE ?? "http://localhost:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
      "/ws": {
        target: (process.env.VITE_API_BASE ?? "http://localhost:8000").replace(/^http/, "ws"),
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
