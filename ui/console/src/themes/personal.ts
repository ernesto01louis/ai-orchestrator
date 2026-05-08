/**
 * Personal theme — anime-inspired override layer.
 *
 * Empty palette stub; the operator will fill this in next iteration.
 * Read by useTheme() when localStorage.theme === "personal".
 *
 * The shape mirrors the `default` theme tokens defined in src/index.css.
 * Any key omitted here falls back to the default value, so partial
 * overrides are valid.
 */

export type ThemeTokens = {
  // Surfaces
  "bg-0"?: string
  "bg-1"?: string
  "bg-2"?: string
  "bg-3"?: string
  line?: string
  "line-soft"?: string

  // Text
  "fg-0"?: string
  "fg-1"?: string
  "fg-2"?: string
  "fg-3"?: string

  // Accents
  accent?: string
  "accent-soft"?: string
  "accent-line"?: string

  // Semantic
  ok?: string
  "ok-soft"?: string
  warn?: string
  "warn-soft"?: string
  err?: string
  "err-soft"?: string
  info?: string
  "info-soft"?: string

  // Type
  "font-sans"?: string
  "font-mono"?: string
}

export const personal: ThemeTokens = {
  // Intentionally empty — fill in next pass.
}

export default personal
