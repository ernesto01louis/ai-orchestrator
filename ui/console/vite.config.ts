import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    // Proxy REST + WebSocket to the live FastAPI backend during dev.
    // Override with VITE_ORCHESTRATOR_URL env var (sets target only, the
    // /api and /ws prefixes stay the same).
    proxy: {
      "/api": {
        target: process.env.VITE_ORCHESTRATOR_URL ?? "http://192.168.2.218:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
      "/ws": {
        target: (process.env.VITE_ORCHESTRATOR_URL ?? "http://192.168.2.218:8000").replace(/^http/, "ws"),
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
