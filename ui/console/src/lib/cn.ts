import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

/**
 * Shadcn-style class merger. Accepts the same shapes as `clsx` and runs
 * the result through `tailwind-merge` so conflicting Tailwind classes
 * (e.g. `px-2 px-4`) collapse to the last one.
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
