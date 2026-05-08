import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"
import type { ButtonHTMLAttributes, ReactNode } from "react"
import { cn } from "@/lib/cn"

const buttonVariants = cva(
  "inline-flex items-center gap-1.5 rounded-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        primary: "bg-accent text-bg-0 border border-accent hover:opacity-90",
        secondary: "bg-bg-2 text-fg-0 border border-line hover:bg-bg-3",
        ghost: "text-fg-1 border border-transparent hover:bg-bg-2",
        outline:
          "text-fg-0 border border-line hover:bg-bg-2 bg-transparent",
        success: "text-ok bg-ok-soft border border-ok hover:opacity-90",
        warn: "text-warn bg-warn-soft border border-warn hover:opacity-90",
        danger: "text-err bg-err-soft border border-err hover:opacity-90",
      },
      size: {
        sm: "px-2 py-1 text-[11px]",
        md: "px-2.5 py-1.5 text-xs",
        lg: "px-3.5 py-2 text-[13px]",
      },
    },
    defaultVariants: {
      variant: "secondary",
      size: "md",
    },
  },
)

interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
  /** Optional leading icon (any ReactNode — typically a lucide icon). */
  icon?: ReactNode
}

/**
 * Button — shadcn-style with success / warn / danger variants beyond
 * the stock palette. Pass `asChild` to render into a child element
 * (Radix Slot pattern) — useful for `<Link asChild>` etc.
 */
export function Button({
  className,
  variant,
  size,
  asChild,
  icon,
  children,
  ...props
}: ButtonProps) {
  const Comp = asChild ? Slot : "button"
  return (
    <Comp
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    >
      {icon}
      {children}
    </Comp>
  )
}

export { buttonVariants }
