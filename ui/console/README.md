# orchestrator · operator console

The neutral, project-facing UI for the AI orchestrator platform. Phase 2.6
of the roadmap. Ships in three PRs:

| PR | What lands |
|---|---|
| **2.6 (a) Foundation** *(this PR)* | Vite + React 19 + TS + Tailwind v4; design tokens (oklch); `useTheme` (default + personal stub); shared atoms; sidebar + topbar shell; React Router routing; `@tanstack/react-query` setup; `useWebSocket` hook; **all seven routes wired (3 page placeholders + 4 stub pages)**. Builds + boots end-to-end. |
| **2.6 (b) Pages** | Pixel-fidelity port of Dashboard, Runs (list + detail), HITL Console from the Claude-Design prototype. Real REST + `/ws` data wired. |
| **2.6 (c) Serving** | FastAPI route at `/console` serves `dist/`; CLAUDE / ROADMAP / RUNBOOK updates; CI step to build the UI. |

The legacy Thanatos / Kazuki UI at `ui/graph.html` (still served by the
FastAPI `/ui` route) remains untouched and will eventually become the
basis for the secondary "personal" theme — see `src/themes/personal.ts`.

## Tech stack

- **React 19** (Strict Mode) + **TypeScript** (strict)
- **Vite 8**
- **Tailwind v4** with `@theme inline` mapping our oklch tokens onto utility names
- **shadcn-style primitives** (Card, Badge, Button) hand-rolled in `src/components/ui/`. Not the full shadcn-cli generator — we only need three primitives and don't want the dep on `@shadcn/cli`.
- **lucide-react** for icons
- **@tanstack/react-query** for REST polling
- **react-router-dom v7** for routing (back-button + URL-shareable routes)
- A custom singleton **`useWebSocket`** for `/ws` (one shared socket, fan-out subscribers, exponential-backoff reconnect)

## Design tokens

The full token system lives in [src/index.css](src/index.css) under the
`[data-theme="default"]` block — every colour is `oklch()` so the
`personal` theme can swap palettes without breaking contrast. Tailwind
classes like `bg-bg-1`, `text-fg-2`, `border-line-soft`, `text-warn` etc.
all resolve through the `@theme inline` block at the top of the same file.

Typography: IBM Plex Sans + Plex Mono via Google Fonts, loaded in
[index.html](index.html). `.mono` and `.num` (mono + tabular-nums)
utilities are defined globally for code-style values across the UI.

## Theme switching

Default skin is the operator console aesthetic. To flip into the empty
`personal` stub:

```js
// browser console
__setTheme("personal")    // or __setTheme("default")
```

The choice persists in `localStorage["theme"]` and is read synchronously
in [index.html](index.html) before React mounts so there's no
default-theme flash. See [src/theme.tsx](src/theme.tsx) for the hook +
provider.

## Develop

```sh
cd ui/console
npm install
npm run dev          # http://localhost:5173/
```

The Vite dev server proxies `/api/*` and `/ws` to the live FastAPI
backend at `http://192.168.2.218:8000` (override with
`VITE_ORCHESTRATOR_URL`). See [vite.config.ts](vite.config.ts).

```sh
npm run build        # → dist/
npm run lint         # ESLint, just touched files
```

## Layout

```
src/
├── main.tsx             — entry; ThemeProvider · QueryClientProvider · BrowserRouter · App
├── App.tsx              — routes + AppShell (sidebar + topbar + outlet)
├── index.css            — Tailwind import, design tokens, atoms (.mono / .num / .kbd / .pulse / .term)
├── theme.tsx            — useTheme hook (default | personal) + window.__setTheme
├── themes/personal.ts   — empty token override stub
├── lib/
│   ├── cn.ts            — clsx + tailwind-merge
│   ├── types.ts         — Run / Campaign / Health / Metrics / WsMessage shapes
│   ├── fmt.ts           — fmtAgo / fmtDuration / fmtNum / shortId
│   ├── api.ts           — REST seam (apiGetHealth / apiGetRuns / apiIntervene / …)
│   └── ws.ts            — singleton WebSocket bus + useWebSocket hook
├── components/
│   ├── ui/Card.tsx
│   ├── ui/Badge.tsx
│   ├── ui/Button.tsx
│   ├── LiveDot.tsx      — pulsing status dot
│   ├── Sparkline.tsx    — responsive SVG sparkline with ResizeObserver
│   ├── PhaseBadge.tsx   — RunPhase → tone-coloured pill
│   ├── ScoreBar.tsx     — judge-score progress bar + tabular value
│   ├── BudgetBar.tsx    — Phase 2.4 budget visualisation
│   ├── ConfidenceBar.tsx — Phase 3.2 SmartPause confidence bar
│   ├── KV.tsx           — uppercase label : mono value row
│   └── Terminal.tsx     — log-line block with ts + phase-coloured tag
├── shell/
│   ├── Sidebar.tsx      — 220px fixed; brand + env switcher + nav + bottom strip
│   └── Topbar.tsx       — title + subtitle + ⌘K trigger + health pill
└── pages/
    ├── Dashboard.tsx    — placeholder (PR (b) lands the real page)
    ├── Runs.tsx         — placeholder
    ├── Hitl.tsx         — placeholder
    ├── StubPage.tsx     — generic placeholder for unbuilt routes
    └── Stubs.tsx        — Campaigns / Logs / Memory / Config stubs
```

## Backend contract used

REST (FastAPI):

```
GET  /health                    → orchestrator + ollama + hindsight
GET  /metrics_console           → console-shaped metrics (PR (c) adds this route)
GET  /runs                      → list runs
GET  /runs/{id}                 → single run
POST /runs/{id}/resume          → SmartPause unblock (Phase 3.2)
POST /runs/{id}/intervene       → HITL approve / reject / edit (Phase 3.1)
GET  /campaigns
GET  /campaigns/{id}/budget     → Phase 2.4
GET  /campaigns/{id}/evidence   → Phase 1.2
GET  /campaigns/{id}/evidence.crate.zip
POST /campaigns/{id}/{pause,resume,abort}
```

WebSocket `/ws` (global broadcast):

```
{"type":"log",    "run_id":"...", "phase":"...", "line":"..."}
{"type":"status", "run_id":"...", "phase":"...", "score":N, "paused":"smartpause|hitl:...|null"}
```
