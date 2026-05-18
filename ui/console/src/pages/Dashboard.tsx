import type * as React from "react";
import { useNavigate } from "react-router-dom";
import { AlertTriangle, ChevronRight, Hand, Pause } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge, TONE } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LiveDot } from "@/components/LiveDot";
import { Sparkline } from "@/components/Sparkline";
import { ScoreBar } from "@/components/ScoreBar";
import { BudgetBar } from "@/components/BudgetBar";
import { PhaseBadge } from "@/components/PhaseBadge";
import { useCampaigns, useMetrics, useRuns } from "@/lib/queries";
import { useWebSocket } from "@/lib/ws";
import { fmtDuration, fmtNum, shortId } from "@/lib/utils";
import type { Run, Campaign, Metrics } from "@/lib/types";

export function Dashboard() {
  const navigate = useNavigate();
  const { data: metrics } = useMetrics();
  const { data: runs = [] } = useRuns();
  const { data: campaigns = [] } = useCampaigns();

  const paused = runs.filter((r) => r.paused);
  const active = runs.filter((r) => !["complete", "failed"].includes(r.phase));
  const openRun = (id: string) => navigate(`/runs/${id}`);

  return (
    <div className="p-5 flex flex-col gap-4">
      <div className="grid grid-cols-4 gap-3">
        <MetricCard
          label="LLM call rate"
          value={metrics ? metrics.llm_calls_rate_5m.toFixed(1) : "—"}
          unit="/s · 5m"
          sub={metrics ? `${fmtNum(metrics.llm_calls_total)} total` : ""}
          spark={metrics?.sparks?.llm_rate}
          tone="accent"
        />
        <MetricCard
          label="p95 latency"
          value={metrics ? metrics.llm_p95_ms.toLocaleString() : "—"}
          unit="ms"
          sub={metrics ? `p50 ${metrics.llm_p50_ms}ms · p99 ${metrics.llm_p99_ms.toLocaleString()}ms` : ""}
          spark={metrics?.sparks?.p95}
          tone="warn"
        />
        <MetricCard
          label="Ollama queue"
          value={metrics ? metrics.ollama_queue_depth : "—"}
          unit="reqs"
          sub={metrics ? `gpu ${(metrics.ollama_gpu_util*100).toFixed(0)}% · vram ${metrics.ollama_vram_used_gb.toFixed(1)}/${metrics.ollama_vram_total_gb.toFixed(0)} GB` : ""}
          spark={metrics?.sparks?.queue}
          tone="info"
        />
        <MetricCard
          label="GPU util"
          value={metrics ? `${(metrics.ollama_gpu_util*100).toFixed(0)}` : "—"}
          unit="%"
          sub={metrics ? `${active.length} active · ${paused.length} paused` : ""}
          spark={metrics?.sparks?.gpu}
          tone="ok"
        />
      </div>

      {paused.length > 0 && (
        <Card padded={false} className="border-warn shadow-[inset_0_0_0_1px_var(--warn-soft)]">
          <div className="flex items-center justify-between px-3.5 py-2.5 border-b border-line-soft">
            <div className="flex items-center gap-2">
              <AlertTriangle size={13} className="text-warn" />
              <span className="text-xs text-fg-0 font-medium">
                {paused.length} run{paused.length > 1 ? "s" : ""} awaiting operator action
              </span>
            </div>
            <Button variant="ghost" size="sm" onClick={() => navigate("/hitl")}>
              Open HITL Console <ChevronRight size={12} />
            </Button>
          </div>
          <div className="grid grid-cols-2">
            {paused.map((r, i) => (
              <PausedStripItem
                key={r.id}
                run={r}
                onOpen={() => openRun(r.id)}
                divider={i % 2 === 0 && i < paused.length - 1}
              />
            ))}
          </div>
        </Card>
      )}

      <div className="grid grid-cols-[1.4fr_1fr] gap-3">
        <Card
          padded={false}
          title={`Active runs · ${active.length}`}
          action={<Button variant="ghost" size="sm" onClick={() => navigate("/runs")}>All runs <ChevronRight size={12} /></Button>}
        >
          <ActiveRunsTable runs={active} onOpen={openRun} />
        </Card>

        <div className="flex flex-col gap-3">
          <Card title="Budget · 24h window">
            <BudgetSummary metrics={metrics} campaigns={campaigns} />
          </Card>
          <Card padded={false} title="Recent campaigns"
                action={<Button variant="ghost" size="sm">All campaigns <ChevronRight size={12} /></Button>}>
            <CampaignList campaigns={campaigns.slice(0, 4)} />
          </Card>
        </div>
      </div>

      <Card padded={false} title="Live · /ws"
            action={
              <div className="flex items-center gap-2">
                <LiveDot tone="ok" />
                <span className="mono text-[11px] text-fg-2">connected</span>
                <Button variant="ghost" size="sm" onClick={() => navigate("/logs")}>Open <ChevronRight size={12} /></Button>
              </div>
            }>
        <LiveLogPreview lines={8} />
      </Card>
    </div>
  );
}

// ── sub-components ─────────────────────────────────────────────────

interface MetricCardProps {
  label: string;
  value: string | number;
  unit: string;
  sub: string;
  spark?: number[];
  tone?: keyof typeof TONE;
}

function MetricCard({ label, value, unit, sub, spark, tone = "accent" }: MetricCardProps) {
  const stroke = `var(--${tone})`;
  return (
    <Card padded={false}>
      <div className="px-3.5 pt-3 pb-2.5">
        <div className="text-[10.5px] uppercase tracking-wider text-fg-2 mb-1.5">{label}</div>
        <div className="flex items-baseline gap-1.5">
          <span className="num text-2xl font-medium text-fg-0">{value}</span>
          <span className="mono text-[11px] text-fg-2">{unit}</span>
        </div>
        <div className="text-[11px] text-fg-3 mt-0.5">{sub}</div>
      </div>
      {spark && <div className="px-2 pb-2"><Sparkline data={spark} height={28} stroke={stroke} /></div>}
    </Card>
  );
}

function PausedStripItem({ run, onOpen, divider }: { run: Run; onOpen: () => void; divider: boolean }) {
  const isSmart = run.paused === "smartpause";
  const tone = isSmart ? "info" : "warn";
  const Icon = isSmart ? Pause : Hand;
  return (
    <div
      onClick={onOpen}
      className={`row-hover grid grid-cols-[auto_1fr_auto] items-center gap-3 px-3.5 py-2.5 cursor-pointer border-b border-line-soft ${divider ? "border-r" : ""}`}
    >
      <div className={`w-7 h-7 rounded-md grid place-items-center bg-${tone}-soft text-${tone}`}>
        <Icon size={13} />
      </div>
      <div className="min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="mono text-xs text-fg-0">{shortId(run.id)}</span>
          <Badge tone={tone} mono>{isSmart ? "smartpause" : `hitl:${run.hitl_mode}`}</Badge>
          <PhaseBadge phase={run.phase} />
        </div>
        <div className="text-[11px] text-fg-2 mt-0.5">
          {run.project} · waiting <span className="num">{fmtDuration(run.started_at)}</span>
          {isSmart && run.confidence != null && (
            <> · planner conf <span className="num text-fg-1">{run.confidence.toFixed(2)}</span></>
          )}
        </div>
      </div>
      <ChevronRight size={14} className="text-fg-3" />
    </div>
  );
}

function ActiveRunsTable({ runs, onOpen }: { runs: Run[]; onOpen: (id: string) => void }) {
  if (!runs.length) return <div className="p-5 text-fg-3 text-xs">No active runs.</div>;
  return (
    <table className="w-full border-collapse text-xs">
      <thead>
        <tr className="bg-bg-1 uppercase text-[10.5px] tracking-wider text-fg-2">
          <Th>Run</Th><Th>Phase</Th><Th>Score</Th><Th>Model</Th><Th>Duration</Th><Th>State</Th>
        </tr>
      </thead>
      <tbody>
        {runs.map((r) => (
          <tr key={r.id} className="row-hover cursor-pointer border-b border-line-soft" onClick={() => onOpen(r.id)}>
            <Td>
              <div className="flex flex-col">
                <span className="mono text-fg-0 text-[11.5px]">{shortId(r.id)}</span>
                <span className="text-fg-3 text-[10.5px]">{r.project}</span>
              </div>
            </Td>
            <Td><PhaseBadge phase={r.phase} /></Td>
            <Td><ScoreBar score={r.score} /></Td>
            <Td><span className="mono text-fg-1 text-[11px]">{r.model.split(":")[0]}</span></Td>
            <Td><span className="num text-fg-1">{fmtDuration(r.started_at)}</span></Td>
            <Td>
              {r.paused ? (
                <Badge tone={r.paused === "smartpause" ? "info" : "warn"} mono dot>
                  {r.paused === "smartpause" ? "smartpause" : `hitl:${r.hitl_mode}`}
                </Badge>
              ) : (
                <Badge tone="ok" mono dot>running</Badge>
              )}
            </Td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export const Th = ({ children }: { children?: React.ReactNode }) => (
  <th className="text-left px-3 py-2 font-medium border-b border-line-soft">{children}</th>
);
export const Td = ({ children }: { children?: React.ReactNode }) => (
  <td className="px-3 py-2.5">{children}</td>
);

function BudgetSummary({ metrics, campaigns }: { metrics?: Metrics; campaigns: Campaign[] }) {
  if (!metrics) return null;
  const pct = (metrics.budget_used_usd / metrics.budget_total_usd) * 100;
  const state = pct > 80 ? "warn" : "healthy";
  return (
    <div className="flex flex-col gap-3">
      <BudgetBar
        used={metrics.budget_used_usd}
        total={metrics.budget_total_usd}
        percentage={pct}
        state={state}
      />
      <div className="grid grid-cols-2 gap-2">
        <Stat k="tokens in"  v={fmtNum(metrics.llm_tokens_in_total)} />
        <Stat k="tokens out" v={fmtNum(metrics.llm_tokens_out_total)} />
        <Stat k="campaigns"  v={metrics.campaigns_active} />
        <Stat k="runs"       v={`${metrics.runs_active} active`} />
      </div>
      <div className="text-[10.5px] text-fg-3 border-t border-dashed border-line-soft pt-2">
        Budget aggregated across {campaigns.filter(c => c.status === "running").length} running campaigns ·
        Sky controller: <span className="mono text-fg-2">enabled</span>
      </div>
    </div>
  );
}

const Stat = ({ k, v }: { k: string; v: React.ReactNode }) => (
  <div className="flex flex-col px-2 py-1.5 bg-bg-2 rounded-[4px]">
    <span className="text-[10px] uppercase text-fg-2 tracking-wider">{k}</span>
    <span className="num text-sm text-fg-0">{v}</span>
  </div>
);

function CampaignList({ campaigns }: { campaigns: Campaign[] }) {
  return (
    <div>
      {campaigns.map((c) => (
        <div key={c.id} className="row-hover grid grid-cols-[1fr_auto] gap-2.5 items-center px-3.5 py-2.5 border-b border-line-soft cursor-pointer last:border-0">
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-xs text-fg-0 font-medium truncate max-w-[220px]">{c.name}</span>
              <Badge tone={c.hitl_mode === "checkpoint" ? "warn" : "muted"} mono>{c.hitl_mode}</Badge>
              <Badge tone={c.status === "running" ? "ok" : c.status === "complete" ? "muted" : "err"} mono dot>{c.status}</Badge>
            </div>
            <div className="text-[10.5px] text-fg-3 mt-0.5">
              <span className="mono">{c.id}</span> · {c.children} child{c.children !== 1 ? "ren" : ""}
              {" "}({c.completed} done{c.failed ? `, ${c.failed} fail` : ""})
            </div>
          </div>
          <div className="w-[110px]"><BudgetBar {...c.budget} /></div>
        </div>
      ))}
    </div>
  );
}

function LiveLogPreview({ lines = 8 }: { lines?: number }) {
  const { messages } = useWebSocket();
  const tail = messages.slice(-lines);
  return (
    <div className="term overflow-hidden py-2" style={{ borderRadius: 0, border: "none", maxHeight: 180 }}>
      {tail.length === 0 ? (
        <div className="ln text-fg-3">waiting for messages on /ws…</div>
      ) : tail.map((m, i) => (
        <div key={i} className="ln">
          <span className="ts">{new Date("ts" in m ? m.ts : Date.now()).toISOString().slice(11, 19)}</span>{" "}
          <span className={`ph-${m.phase}`}>{m.phase.padEnd(10)}</span>{" "}
          <span className="text-fg-3">{shortId(m.run_id)}</span>{" "}
          <span className="text-fg-1">{"line" in m ? m.line : ""}</span>
        </div>
      ))}
    </div>
  );
}
