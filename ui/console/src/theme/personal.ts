// Personal theme — anime-inspired override layer.
//
// Empty palette stub; the user will fill this in next iteration.
// Read by ThemeProvider when localStorage.theme === "personal".
//
// The shape mirrors the `default` theme tokens defined in src/index.css.
// Any key omitted falls back to the default value, so partial overrides
// are valid.

export type ThemeTokens = Partial<{
  // surfaces
  "bg-0": string;
  "bg-1": string;
  "bg-2": string;
  "bg-3": string;
  line: string;
  "line-soft": string;

  // text
  "fg-0": string;
  "fg-1": string;
  "fg-2": string;
  "fg-3": string;

  // accents
  accent: string;
  "accent-soft": string;
  "accent-line": string;

  // semantic
  ok: string;
  "ok-soft": string;
  warn: string;
  "warn-soft": string;
  err: string;
  "err-soft": string;
  info: string;
  "info-soft": string;

  // type
  "font-sans": string;
  "font-mono": string;
}>;

export const personalTokens: ThemeTokens = {
  // intentionally empty — fill in next pass
};
