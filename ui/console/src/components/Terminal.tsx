import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";

export interface TerminalLine {
  ts: string;
  phase: string;
  line: string;
}

interface Props {
  lines: TerminalLine[];
  className?: string;
  emptyText?: string;
  /** Auto-scroll to bottom when new lines arrive (default true). */
  follow?: boolean;
  minHeight?: number;
  maxHeight?: number;
}

export function Terminal({
  lines,
  className,
  emptyText = "waiting for /ws traffic…",
  follow = true,
  minHeight = 380,
  maxHeight = 460,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (follow && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [lines.length, follow]);

  return (
    <div
      ref={ref}
      className={cn("term overflow-auto py-2.5", className)}
      style={{ minHeight, maxHeight, border: "none", borderRadius: 0 }}
    >
      {lines.length === 0 && <div className="ln text-fg-3">{emptyText}</div>}
      {lines.map((m, i) => (
        <div key={i} className="ln">
          <span className="ts">{new Date(m.ts).toISOString().slice(11, 19)}</span>{" "}
          <span className={`ph-${m.phase}`}>{(m.phase || "").padEnd(10)}</span>{" "}
          <span className="text-fg-1">{m.line}</span>
        </div>
      ))}
    </div>
  );
}
