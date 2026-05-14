import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center gap-1.5 rounded-sm font-medium font-sans transition-colors disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent",
  {
    variants: {
      variant: {
        primary: "bg-accent text-bg-0 border border-accent hover:bg-accent/90",
        secondary: "bg-bg-2 text-fg-0 border border-line hover:bg-bg-3",
        ghost: "bg-transparent text-fg-1 border border-transparent hover:bg-bg-1",
        outline: "bg-transparent text-fg-0 border border-line hover:bg-bg-1",
        success: "bg-ok-soft text-ok border border-ok hover:bg-ok/20",
        danger: "bg-err-soft text-err border border-err hover:bg-err/20",
        warn: "bg-warn-soft text-warn border border-warn hover:bg-warn/20",
      },
      size: {
        sm: "px-2 py-1 text-[11px]",
        md: "px-2.5 py-1.5 text-xs",
        lg: "px-3.5 py-2 text-[13px]",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
  icon?: React.ReactNode;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild, icon, children, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props}>
        {icon}
        {children}
      </Comp>
    );
  },
);
Button.displayName = "Button";
