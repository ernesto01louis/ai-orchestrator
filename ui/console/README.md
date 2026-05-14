# orchestrator-console

Operator console for the AI orchestrator platform.
React 18 + Vite + Tailwind + react-query + react-router.

## Quickstart

```bash
pnpm install        # or npm / yarn / bun
cp .env.example .env
pnpm dev
```

By default `VITE_USE_MOCKS=1` is set, so the UI runs without the FastAPI
backend — fixtures live in `src/lib/mocks.ts`. Comment that out to fetch
real data via the Vite proxy (`/api/*` → `VITE_API_BASE`).

## Project layout

```
src/
├── main.tsx              ← React + react-query + router root
├── App.tsx               ← Route table
├── index.css             ← Theme tokens (CSS custom properties)
│
├── lib/
│   ├── types.ts          ← Backend contract types
│   ├── api.ts            ← REST client (mocks-aware)
│   ├── ws.ts             ← /ws hook with auto-reconnect
│   ├── queries.ts        ← useQuery wrappers + query keys
│   ├── mocks.ts          ← Fixtures (gated by VITE_USE_MOCKS)
│   └── utils.ts          ← cn(), formatters
│
├── theme/
│   ├── ThemeProvider.tsx ← useTheme(), default | personal
│   └── personal.ts       ← Empty token-override stub (anime skin)
│
├── components/
│   ├── ui/               ← Button, Card, Badge (shadcn-style)
│   ├── PhaseBadge.tsx
│   ├── LiveDot.tsx
│   ├── ScoreBar.tsx
│   ├── BudgetBar.tsx
│   ├── Sparkline.tsx     ← ResizeObserver-driven
│   ├── Terminal.tsx
│   └── KV.tsx
│
├── shell/
│   ├── AppShell.tsx
│   ├── Sidebar.tsx
│   └── Topbar.tsx
│
└── pages/
    ├── Dashboard.tsx
    ├── RunsList.tsx
    ├── RunDetail.tsx
    ├── Hitl.tsx
    └── Stub.tsx          ← Placeholder for Campaigns / Logs / Memory / Config
```

## Theming

Two themes:

- **`default`** — operator-console aesthetic (this iteration)
- **`personal`** — anime-inspired override layer; empty token stub at
  `src/theme/personal.ts`

There is intentionally **no UI toggle** in the default skin. To flip:

```js
// in DevTools console
window.__setTheme("personal")  // reloads
window.__setTheme("default")
```

`useTheme()` reads `localStorage.theme`, sets `data-theme="..."` on
`<html>`, and applies any tokens from `personal.ts` as inline custom
properties on `:root`. Tailwind classes resolve through these CSS vars
so partial overrides work without component changes.

## Backend contract seams

| Surface       | Wire here                                                |
| ------------- | -------------------------------------------------------- |
| REST          | `src/lib/api.ts` (`/api` proxied to `VITE_API_BASE`)     |
| WebSocket     | `src/lib/ws.ts` (`VITE_WS_URL` or derived from origin)   |
| React Query keys | `src/lib/queries.ts` (`qk.health() / qk.runs()` etc.) |
| Types         | `src/lib/types.ts`                                       |

## Pages built

- **Dashboard** (`/dashboard`)
- **Runs list** (`/runs`) + **Run detail** (`/runs/:runId`)
- **HITL Console** (`/hitl`)

Stubs (next round): Campaigns, Live Logs, Memory & Gates, Config.

## Scripts

```bash
pnpm dev         # vite dev server
pnpm build       # tsc -b && vite build
pnpm preview     # serve dist
pnpm typecheck   # tsc -b --noEmit
pnpm lint        # eslint
```
