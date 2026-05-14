// useTheme — switches the visual layer between `default` and `personal`.
//
// The default skin is defined as CSS custom properties in src/index.css
// under [data-theme="default"]. Switching to `personal` does two things:
//   1. Sets data-theme="personal" on <html> so future personal.css rules
//      can scope under [data-theme="personal"].
//   2. Applies any token overrides exported from src/theme/personal.ts
//      as inline CSS custom properties on the root element.
//
// Toggle is intentionally hidden from the default UI — flipped only via
//   localStorage.setItem("theme", "personal" | "default")
// or window.__setTheme(name).

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { personalTokens } from "./personal";

export type ThemeName = "default" | "personal";

interface ThemeContextValue {
  theme: ThemeName;
  setTheme: (name: ThemeName) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function readStoredTheme(): ThemeName {
  try {
    const v = localStorage.getItem("theme");
    return v === "personal" ? "personal" : "default";
  } catch {
    return "default";
  }
}

function applyTheme(name: ThemeName): void {
  const root = document.documentElement;
  root.setAttribute("data-theme", name);

  // Clear any previously-injected personal overrides
  const existing = root.getAttribute("data-theme-overrides");
  if (existing) {
    existing.split(",").forEach((k) => k && root.style.removeProperty(`--${k}`));
    root.removeAttribute("data-theme-overrides");
  }

  if (name === "personal") {
    const keys = Object.keys(personalTokens) as (keyof typeof personalTokens)[];
    keys.forEach((k) => {
      const v = personalTokens[k];
      if (v) root.style.setProperty(`--${k}`, v);
    });
    if (keys.length) root.setAttribute("data-theme-overrides", keys.join(","));
  }
}

declare global {
  interface Window {
    __setTheme?: (name: ThemeName) => void;
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeName>(readStoredTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  // Cross-tab sync
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === "theme") setThemeState(readStoredTheme());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const setTheme = useCallback((name: ThemeName) => {
    try { localStorage.setItem("theme", name); } catch { /* ignore */ }
    setThemeState(name);
  }, []);

  // Console hook — `__setTheme("personal")` flips without UI.
  useEffect(() => {
    window.__setTheme = (name) => {
      try { localStorage.setItem("theme", name); } catch { /* ignore */ }
      location.reload();
    };
    return () => { delete window.__setTheme; };
  }, []);

  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside <ThemeProvider>");
  return ctx;
}
