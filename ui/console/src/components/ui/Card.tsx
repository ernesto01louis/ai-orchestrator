import type { HTMLAttributes, ReactNode } from "react"
import { cn } from "@/lib/cn"

// HTMLAttributes.title is `string` and would clash with our richer
// ReactNode title slot. Omit it from the base props.
interface CardProps extends Omit<HTMLAttributes<HTMLDivElement>, "title"> {
  /** Optional uppercase title strip. Adds a 10px/14px header + divider. */
  title?: ReactNode
  /** Right-aligned action element rendered into the title strip. */
  action?: ReactNode
  /** Disable the default 14px body padding (e.g. for tables). */
  padded?: boolean
}

/**
 * Card — bg-1 surface with line-soft border, optional title strip.
 *
 * Matches the prototype's `Card` but typed and Tailwind-driven. Pass
 * `padded={false}` for full-bleed children (tables, terminals).
 */
export function Card({
  title,
  action,
  padded = true,
  className,
  children,
  ...props
}: CardProps) {
  return (
    <div
      className={cn(
        "rounded-md border border-line-soft bg-bg-1",
        className,
      )}
      {...props}
    >
      {title != null && (
        <div className="flex items-center justify-between border-b border-line-soft px-3.5 py-2.5">
          <div className="text-[11px] font-medium uppercase tracking-[0.6px] text-fg-2">
            {title}
          </div>
          {action}
        </div>
      )}
      <div className={padded ? "p-3.5" : undefined}>{children}</div>
    </div>
  )
}
