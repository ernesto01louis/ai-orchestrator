import type { LucideIcon } from "lucide-react"
import { Card } from "@/components/ui/Card"

interface StubPageProps {
  title: string
  Icon: LucideIcon
  /** Bullet list of planned content for this page. */
  planned: string[]
  /** REST/WS endpoints the real page will consume. */
  endpoints: string[]
}

/**
 * StubPage — placeholder for routes that are designed but not yet built.
 *
 * Phase 2.6 (a) ships all four secondary pages (Campaigns, Logs, Memory,
 * Config) as stubs so the nav works end-to-end while we focus on
 * Dashboard / Runs / HITL in PR (b).
 */
export function StubPage({ title, Icon, planned, endpoints }: StubPageProps) {
  return (
    <div className="max-w-[720px] p-8">
      <div className="mb-3.5 flex items-center gap-3">
        <div className="grid size-[34px] place-items-center rounded-md border border-line-soft bg-bg-2 text-accent">
          <Icon width={16} height={16} />
        </div>
        <div>
          <h2 className="m-0 text-base font-semibold text-fg-0">{title}</h2>
          <div className="mt-0.5 text-[11px] text-fg-3">
            Stub for follow-up — design landed, real wiring next round.
          </div>
        </div>
      </div>

      <Card title="Planned content">
        <ul className="m-0 list-disc pl-[18px] text-[12.5px] leading-[1.7] text-fg-1">
          {planned.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </Card>

      <div className="h-3" />

      <Card title="Endpoints">
        <div className="flex flex-col gap-1">
          {endpoints.map((e) => (
            <div
              key={e}
              className="rounded-sm border border-line-soft bg-bg-2 px-2 py-1.5 font-mono text-xs text-fg-1"
            >
              {e}
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}
