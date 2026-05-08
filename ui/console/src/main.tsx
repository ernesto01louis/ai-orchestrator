import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { BrowserRouter } from "react-router-dom"

import { App } from "./App"
import { ThemeProvider } from "./theme"
import "./index.css"

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Stale-while-revalidate; per-query refetchInterval overrides this.
      staleTime: 2_000,
      // The orchestrator is on the same homelab; retry once for transient
      // blips, then bubble.
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

const rootElement = document.getElementById("root")
if (!rootElement) throw new Error("missing #root in index.html")

createRoot(rootElement).render(
  <StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
)
