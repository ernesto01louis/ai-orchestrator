import * as React from "react";
import { cn } from "@/lib/utils";

interface CardProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  title?: React.ReactNode;
  action?: React.ReactNode;
  padded?: boolean;
}

export function Card({ title, action, padded = true, className, children, ...rest }: CardProps) {
  return (
    <div
      className={cn("bg-bg-1 border border-line-soft rounded-md", className)}
      {...rest}
    >
      {title && (
        <div className="flex items-center justify-between px-3.5 py-2.5 border-b border-line-soft">
          <div className="text-[11px] uppercase tracking-wider text-fg-2 font-medium">{title}</div>
          {action}
        </div>
      )}
      <div className={padded ? "p-3.5" : ""}>{children}</div>
    </div>
  );
}
