import type { Budget } from "@/lib/types";
import { cn } from "@/lib/utils";

interface Props extends Budget {
  className?: string;
}

export function BudgetBar({ used, total, percentage, state, className }: Props) {
  const tone = state === "warn" ? "warn" : state === "err" ? "err" : "ok";
  const fillCls = tone === "warn" ? "bg-warn" : tone === "err" ? "bg-err" : "bg-ok";
  const textCls = tone === "warn" ? "text-warn" : tone === "err" ? "text-err" : "text-ok";
  return (
    <div className={cn("flex flex-col gap-1 w-full", className)}>
      <div className="flex justify-between text-[11px]">
        <span className="num text-fg-1">${used.toFixed(2)} / ${total.toFixed(2)}</span>
        <span className={cn("num", textCls)}>{percentage.toFixed(1)}%</span>
      </div>
      <div className="h-1 bg-bg-3 rounded-[2px] overflow-hidden">
        <div className={cn("h-full", fillCls)} style={{ width: `${percentage}%` }} />
      </div>
    </div>
  );
}
