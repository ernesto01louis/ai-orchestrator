import { useCallback, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Check, ChevronRight, Edit, ShieldCheck, X } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge, TONE } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PhaseBadge, PHASE_TONE } from "@/components/PhaseBadge";
import { ScoreBar } from "@/components/ScoreBar";
import { Terminal } from "@/components/Terminal";
import { KV } from "@/components/KV";
import { LiveDot } from "@/components/LiveDot";
import { useRun } from "@/lib/queries";
import { useWebSocket } from "@/lib/ws";
import * as api from "@/lib/api";
import { fmtDuration, shortId } from "@/lib/utils";
import type { InterveneAction, Phase, Run, WsMessage } from "@/lib/types";
import { MOCK_CAMPAIGNS } from "@/lib/mocks";

export function RunDetail() {
  const navigate = useNavigate();
  const { runId = "" } = useParams<{ runId: string }>();
  const { data: run } = useRun(runId);

  const wsFilter = useCallback((m: WsMessage) => m.run_id === runId, [runId]);
  const { messages } = useWebSocket({ filter: wsFilter });

  const [verifyState, setVerifyState] = useState<"idle" | "checking" | "ok">("idle");

  const seed = useMemo<{ ts: string; phase: string; line: string }[]>(() => {
    if (!run) return [];
    const camp = MOCK_CAMPAIGNS.find((c) => c.id === run.campaign_id);
    return [
      { ts: new Date(Date.now() - 12000).toISOString(), phase: "planner",   line: `bootstrap context · run_id=${run.id}` },
      { ts: new Date(Date.now() - 10500).toISOString(), phase: "planner",   line: `loaded ${camp?.name ?? "campaign"} grid params` },
      { ts: new Date(Date.now() -  9300).toISOString(), phase: "planner",   line: "decomposed task into 4 sub-goals" },
      { ts: new Date(Date.now() -  7400).toISOString(), phase: "generator", line: `model=${run.model.split(":")[0]} ctx=8192` },
      { ts: new Date(Date.now() -  5200).toISOString(), phase: "generator", line: "draft 1/3 token=128 latency=412ms" },
      { ts: new Date(Date.now() -  3100).toISOString(), phase: "generator", line: "draft 2/3 token=256 latency=518ms" },
      { ts: new Date(Date.now() -  1800).toISOString(), phase: "judge",     line: "rubric=citation_grade.v3 score=0.71 pass=true" },
    ];
  }, [run]);

  const allLines = useMemo(
    () => [
      ...seed,
      ...messages
        .filter((m): m is Extract<WsMessage, { type: "log" }> => m.type === "log")
        .map((m) => ({ ts: m.ts, phase: m.phase, line: m.line })),
    ],
    [seed, messages],
  );

  const onVerify = async () => {
    setVerifyState("checking");
    await api.verifyManifest(runId);
    setVerifyState("ok");
  };

  const onIntervene = async (action: InterveneAction) => {
    await api.intervene(runId, { action });
    // In real use: queryClient.invalidateQueries({ queryKey: qk.run(runId) })
  };

  if (!run) return <div className="p-8 text-fg-3">Loading run…</div>;

  return (
    <div className="p-5 flex flex-col gap-3.5">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate("/runs")} icon={<ChevronRight size={12} className="rotate-180" />}>
          Runs
        </Button>
        <span className="text-fg-3">/</span>
        <span className="mono text-[13px] text-fg-0">{run.id}</span>
        <span className="flex-1" />

        <Button
          variant={verifyState === "ok" ? "success" : "outline"}
          size="md"
          onClick={onVerify}
          icon={verifyState === "ok" ? <Check size={12} /> : <ShieldCheck size={12} />}
          disabled={verifyState === "checking"}
        >
          {verifyState === "checking" ? "verifying manifest…" :
           verifyState === "ok"       ? "manifest verified" :
                                        "verify manifest"}
        </Button>

        {run.paused && (
          <>
            <Button variant="success" size="md" icon={<Check size={12} />} onClick={() => onIntervene("approve")}>Approve</Button>
            <Button variant="warn"    size="md" icon={<Edit size={12} />}  onClick={() => onIntervene("edit")}>Edit</Button>
            <Button variant="danger"  size="md" icon={<X size={12} />}     onClick={() => onIntervene("reject")}>Reject</Button>
          </>
        )}
      </div>

      <div className="grid grid-cols-6 gap-2">
        <MetaCell label="phase"    value={<PhaseBadge phase={run.phase} />} />
        <MetaCell label="score"    value={<ScoreBar score={run.score} />} />
        <MetaCell label="model"    value={<span className="mono text-[11.5px] text-fg-0">{run.model}</span>} />
        <MetaCell label="target"   value={<span className="mono text-[11.5px] text-fg-0">{run.target}</span>} />
        <MetaCell label="duration" value={<span className="num text-fg-0">{fmtDuration(run.started_at)}</span>} />
        <MetaCell label="state"    value={
          run.paused
            ? <Badge tone={run.paused === "smartpause" ? "info" : "warn"} mono dot>
                {run.paused === "smartpause" ? "smartpause" : `hitl:${run.hitl_mode}`}
              </Badge>
            : <Badge tone="ok" mono dot>running</Badge>
        } />
      </div>

      <div className="grid grid-cols-[1.6fr_1fr] gap-3 min-h-[420px]">
        <Card
          padded={false}
          title="Live log tail · /ws"
          action={
            <div className="flex items-center gap-1.5">
              <LiveDot tone="ok" />
              <span className="mono text-[11px] text-fg-2">filter run_id={shortId(run.id)}</span>
            </div>
          }
        >
          <Terminal lines={allLines} />
        </Card>

        <div className="flex flex-col gap-3">
          <Card title="Provenance">
            <KV k="run id"    v={run.id} />
            <KV k="campaign"  v={run.campaign_id} />
            <KV k="trace"     v={`tempo://${run.id.slice(-12).toLowerCase()}`} />
            <KV k="artifacts" v={`dvc://truenas/runs/${run.id}/`} />
            <KV k="manifest"  v="sha256:8f2c…b3a1" />
            <KV k="dsse"      v={<span className="text-ok">signed · ed25519</span>} mono={false} />
          </Card>

          <Card title="Phases">
            <PhaseTimeline run={run} />
          </Card>
        </div>
      </div>
    </div>
  );
}

export function MetaCell({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="px-2.5 py-2 bg-bg-1 border border-line-soft rounded-sm flex flex-col gap-1 min-w-0">
      <span className="text-[10px] uppercase tracking-wider text-fg-2">{label}</span>
      <span className="min-w-0 truncate">{value}</span>
    </div>
  );
}

const PHASE_ORDER: Phase[] = ["planner", "post_planner", "generator", "judge", "optimizer", "complete"];

function PhaseTimeline({ run }: { run: Run }) {
  const idx = PHASE_ORDER.indexOf(run.phase);
  return (
    <div className="flex flex-col gap-1.5">
      {PHASE_ORDER.map((p, i) => {
        const isPast = i < idx;
        const isCurrent = i === idx;
        const tone = isCurrent ? PHASE_TONE[p] : isPast ? "ok" : "muted";
        const fg = TONE[tone].fg;
        return (
          <div key={p} className="grid grid-cols-[auto_1fr_auto] items-center gap-2.5 py-1">
            <span
              className="grid place-items-center"
              style={{
                width: 14, height: 14, borderRadius: 999,
                background: isCurrent ? `var(--${tone}-soft)` : "transparent",
                border: `1.5px solid ${isPast ? "var(--ok)" : isCurrent ? `var(--${tone})` : "var(--line)"}`,
              }}
            >
              {isPast && <Check size={9} className="text-ok" strokeWidth={2.5} />}
              {isCurrent && (
                <span
                  className="animate-pulse-ring"
                  style={{ width: 5, height: 5, borderRadius: 999, background: `var(--${tone})`, color: `var(--${tone})` }}
                />
              )}
            </span>
            <span className={`mono text-[11.5px] ${isCurrent ? "text-fg-0" : isPast ? "text-fg-1" : "text-fg-3"} ${fg}`}>{p}</span>
            {isCurrent && run.paused && (
              <Badge tone={run.paused === "smartpause" ? "info" : "warn"} mono>
                {run.paused === "smartpause" ? "paused" : `hitl:${run.hitl_mode}`}
              </Badge>
            )}
          </div>
        );
      })}
    </div>
  );
}
