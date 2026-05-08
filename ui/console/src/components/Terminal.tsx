import { useEffect, useRef } from "react"
import type { RunPhase } from "@/lib/types"
import { shortId } from "@/lib/fmt"

export interface TerminalLine {
  ts: string | undefined
  phase: RunPhase | string
  line: string
  run_id?: string
}

interface TerminalProps {
  lines: TerminalLine[]
  /** Render the run-id column. Default true. */
  showRunId?: boolean
  /** Override the default 460/380 max/min heights. */
  maxHeight?: number | string
  minHeight?: number | string
  /** Drop the rounded border + bg — useful inside Card title strips. */
  flush?: boolean
}

/**
 * Terminal — fixed-width log block with ts + phase-coloured tag + line.
 *
 * Auto-scrolls to the bottom on every new line. The phase classes
 * (`ph-planner`, `ph-judge`, …) live in src/index.css under the
 * `.term` block so the colours follow the theme.
 */
export function Terminal({
  lines,
  showRunId = true,
  maxHeight = 460,
  minHeight = 380,
  flush = false,
}: TerminalProps) {
  const ref = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight
  }, [lines.length])

  return (
    <div
      ref={ref}
      className={`term overflow-auto py-2.5 ${flush ? "rounded-none border-0" : ""}`}
      style={{ maxHeight, minHeight }}
    >
      {lines.length === 0 ? (
        <div className="ln text-fg-3">waiting for /ws traffic…</div>
      ) : (
        lines.map((m, i) => (
          <div key={i} className="ln">
            <span className="ts">
              {(m.ts ?? new Date().toISOString()).slice(11, 19)}
            </span>{" "}
            <span className={`ph-${m.phase}`}>{(m.phase || "").padEnd(10)}</span>{" "}
            {showRunId && m.run_id && (
              <>
                <span className="text-fg-3">{shortId(m.run_id)}</span>{" "}
              </>
            )}
            <span className="text-fg-1">{m.line}</span>
          </div>
        ))
      )}
    </div>
  )
}
