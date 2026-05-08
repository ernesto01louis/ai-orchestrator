/**
 * useTheme — switches the visual layer between `default` and `personal`.
 *
 * The default skin is the operator-console aesthetic baked into
 * src/index.css as CSS variables under [data-theme="default"]. Switching
 * to `personal` does TWO things:
 *
 *   1. Sets data-theme="personal" on <html> (lets a future personal.css
 *      block scope itself under [data-theme="personal"]).
 *   2. Applies any token overrides exported from src/themes/personal.ts
 *      as inline CSS custom properties on the root element.
 *
 * Toggle is intentionally hidden from the default skin's UI — flipped
 * only via:
 *   - localStorage.setItem("theme", "personal" | "default")
 *   - window.__setTheme("personal" | "default")    (console hook)
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react"
import type { ReactNode } from "react"

import { personal as PERSONAL_TOKENS } from "./themes/personal"

type ThemeName = "default" | "personal"

interface ThemeContextValue {
  theme: ThemeName
  setTheme: (name: ThemeName) => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

function readStoredTheme(): ThemeName {
  try {
    return localStorage.getItem("theme") === "personal" ? "personal" : "default"
  } catch {
    return "default"
  }
}

function applyTheme(name: ThemeName): void {
  const root = document.documentElement
  root.setAttribute("data-theme", name)

  // Clear any previously-injected personal overrides.
  const existing = root.getAttribute("data-theme-overrides")
  if (existing) {
    existing.split(",").forEach((k) => k && root.style.removeProperty(`--${k}`))
    root.removeAttribute("data-theme-overrides")
  }

  if (name === "personal") {
    const keys = Object.keys(PERSONAL_TOKENS) as Array<keyof typeof PERSONAL_TOKENS>
    keys.forEach((k) => {
      const v = PERSONAL_TOKENS[k]
      if (v) root.style.setProperty(`--${k}`, v)
    })
    if (keys.length) root.setAttribute("data-theme-overrides", keys.join(","))
  }
}

declare global {
  interface Window {
    __setTheme?: (name: ThemeName) => void
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeName>(readStoredTheme)

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  // Cross-tab sync.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === "theme") setThemeState(readStoredTheme())
    }
    window.addEventListener("storage", onStorage)
    return () => window.removeEventListener("storage", onStorage)
  }, [])

  const setTheme = useCallback((name: ThemeName) => {
    try {
      localStorage.setItem("theme", name)
    } catch {
      /* private mode / quota — ignore */
    }
    setThemeState(name)
  }, [])

  // Console hook — operators flip themes without a UI toggle.
  useEffect(() => {
    window.__setTheme = (name: ThemeName) => setTheme(name)
    return () => {
      delete window.__setTheme
    }
  }, [setTheme])

  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error("useTheme must be used inside <ThemeProvider>")
  return ctx
}
