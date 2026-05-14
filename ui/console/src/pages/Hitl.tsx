import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, Edit, Hand, RefreshCw, X } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge, TONE, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfidenceBar } from "@/components/ConfidenceBar";
import { PhaseBadge } from "@/components/PhaseBadge";
import { useRuns } from "@/lib/queries";
import * as api from "@/lib/api";
import { fmtDuration, shortId } from "@/lib/utils";
import type { InterveneAction, Run } from "@/lib/types";
import { MetaCell } from "./RunDetail";

const TABS: Array<{ id: "approve" | "edit" | "reject"; label: string; tone: Tone; Icon: typeof Check; hot: string }> = [
  { id: "approve", label: "Approve", tone: "ok",   Icon: Check, hot: "A" },
  { id: "edit",    label: "Edit",    tone: "warn", Icon: Edit,  hot: "E" },
  { id: "reject",  label: "Reject",  tone: "err",  Icon: X,     hot: "R" },
];

export function Hitl() {
  const { data: runs = [], refetch } = useRuns();
  const paused = runs.filter((r) => r.paused);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = paused.find((r) => r.id === selectedId) ?? paused[0];

  useEffect(() => {
    if (selected && !selectedId) setSelectedId(selected.id);
  }, [selected, selectedId]);

  return (
    <div className="p-5 flex flex-col gap-3.5">
      <div className="flex items-center gap-3">
        <div className="w-[30px] h-[30px] rounded-md bg-warn-soft text-warn grid place-items-center">
          <Hand size={15} />
        </div>
        <div className="flex flex-col leading-tight">
          <span className="text-sm font-semibold text-fg-0">HITL Console</span>
          <span className="text-[11px] text-fg-2">
            {paused.length} run{paused.length === 1 ? "" : "s"} awaiting intervention ·
            <span className="mono ml-1">POST /runs/{`{id}`}/intervene</span>
          </span>
        </div>
        <span className="flex-1" />
        <Button variant="ghost" size="sm" onClick={() => refetch()} icon={<RefreshCw size={12} />}>refresh</Button>
      </div>

      {paused.length === 0 ? (
        <Card>
          <div className="p-8 text-center text-fg-2">
            <Check size={20} className="text-ok mx-auto mb-2" />
            <div className="text-[13px] text-fg-0">No paused runs.</div>
            <div className="text-[11.5px] text-fg-3 mt-1">
              SmartPause and HITL checkpoints will surface here when they fire.
            </div>
          </div>
        </Card>
      ) : (
        <div className="grid grid-cols-[320px_1fr] gap-3 min-h-[520px]">
          <Card padded={false} title={`Queue · ${paused.length}`}>
            <div className="flex flex-col">
              {paused.map((r) => (
                <QueueItem
                  key={r.id} run={r}
                  active={selected?.id === r.id}
                  onSelect={() => setSelectedId(r.id)}
                />
              ))}
            </div>
          </Card>
          {selected && <HitlDetail key={selected.id} run={selected} onResolved={refetch} />}
        </div>
      )}

      <Card title="Intervene modes">
        <div className="grid grid-cols-5 gap-3 text-[11px]">
          <ModeChip mode="approve" tone="ok"    desc="Accept current planner output and unblock generator." />
          <ModeChip mode="reject"  tone="err"   desc="Mark step as failed; orchestrator may retry per policy." />
          <ModeChip mode="edit"    tone="warn"  desc="Modify the planner output, then resume." />
          <ModeChip mode="skip"    tone="info"  desc="Skip the gated phase and continue downstream." />
          <ModeChip mode="abort"   tone="muted" desc="Terminate the entire run. Manifest closes early." />
        </div>
      </Card>
    </div>
  );
}

function ModeChip({ mode, tone, desc }: { mode: string; tone: Tone; desc: string }) {
  return (
    <div className="px-2.5 py-2 bg-bg-2 border border-line-soft rounded-sm flex flex-col gap-1">
      <span className={`mono text-[11px] font-medium ${TONE[tone].fg}`}>{mode}</span>
      <span className="text-fg-2 text-[11px] leading-snug">{desc}</span>
    </div>
  );
}

function QueueItem({ run, active, onSelect }: { run: Run; active: boolean; onSelect: () => void }) {
  const isSmart = run.paused === "smartpause";
  const tone: Tone = isSmart ? "info" : "warn";
  return (
    <div
      onClick={onSelect}
      className={`px-3.5 py-2.5 border-b border-line-soft cursor-pointer ${active ? "bg-bg-2" : ""}`}
      style={{ borderLeft: `2px solid ${active ? `var(--${tone})` : "transparent"}` }}
    >
      <div className="flex items-center gap-1.5">
        <span className="mono text-[11.5px] text-fg-0">{shortId(run.id)}</span>
        <span className="flex-1" />
        <span className="num text-[10.5px] text-fg-2">{fmtDuration(run.started_at)}</span>
      </div>
      <div className="flex items-center gap-1.5 mt-1 flex-wrap">
        <Badge tone={tone} mono>{isSmart ? "smartpause" : `hitl:${run.hitl_mode}`}</Badge>
        <PhaseBadge phase={run.phase} />
      </div>
      <div className="text-[11px] text-fg-2 mt-1">{run.project}</div>
      {isSmart && run.confidence != null && (
        <div className="flex items-center gap-1.5 mt-1">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">conf</span>
          <ConfidenceBar value={run.confidence} />
        </div>
      )}
    </div>
  );
}

interface PlannerOutput {
  sub_goals: string[];
  tool_plan: string[];
  notes: string;
}

function HitlDetail({ run, onResolved }: { run: Run; onResolved: () => void }) {
  const [tab, setTab] = useState<"approve" | "edit" | "reject">("approve");
  const [editText, setEditText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [resolved, setResolved] = useState<string | null>(null);

  const isSmart = run.paused === "smartpause";

  const plannerOutput = useMemo<PlannerOutput>(() => ({
    sub_goals: [
      "Locate cited evidence within source corpus",
      "Verify quote alignment via fuzzy match (>= 0.85)",
      "Compose answer with inline citations",
      "Run citation_grade.v3 judge",
    ],
    tool_plan: [
      "search.web(query=...) → 5 results",
      "fs.read(paths=[corpus/*.md])",
      "code.run(lang=python, snippet=...)",
    ],
    notes: "Planner uncertain on goal-3 phrasing; recommends checkpoint before generator commits tokens.",
  }), []);

  useEffect(() => {
    setEditText(JSON.stringify(plannerOutput, null, 2));
    setTab("approve");
    setResolved(null);
  }, [run.id, plannerOutput]);

  const submit = async (action: InterveneAction) => {
    setSubmitting(true);
    await api.intervene(run.id, action === "edit" ? { action, payload: editText } : { action });
    setSubmitting(false);
    setResolved(action);
    onResolved();
  };

  return (
    <Card padded={false} className="flex flex-col">
      <div className="flex items-center gap-2.5 px-3.5 py-3 border-b border-line-soft">
        <span className="mono text-[13px] text-fg-0">{run.id}</span>
        <Badge tone={isSmart ? "info" : "warn"} mono>{isSmart ? "smartpause" : `hitl:${run.hitl_mode}`}</Badge>
        <PhaseBadge phase={run.phase} />
        <span className="flex-1" />
        {isSmart && run.confidence != null && (
          <div className="flex items-center gap-2">
            <span className="text-[10.5px] text-fg-3 uppercase tracking-wider">planner conf</span>
            <ConfidenceBar value={run.confidence} w={120} />
          </div>
        )}
      </div>

      <div className="grid grid-cols-4 gap-2 p-3.5 border-b border-line-soft">
        <MetaCell label="project"   value={<span className="text-[11.5px] text-fg-0">{run.project}</span>} />
        <MetaCell label="campaign"  value={<span className="mono text-[11.5px] text-fg-0">{run.campaign_id}</span>} />
        <MetaCell label="model"     value={<span className="mono text-[11.5px] text-fg-0">{run.model}</span>} />
        <MetaCell label="waiting"   value={<span className="num text-warn">{fmtDuration(run.started_at)}</span>} />
      </div>

      <div className="flex px-3.5 border-b border-line-soft">
        {TABS.map((t) => {
          const active = tab === t.id;
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`flex items-center gap-1.5 px-3.5 py-2.5 bg-transparent border-none cursor-pointer text-xs ${active ? `${TONE[t.tone].fg} font-medium` : "text-fg-2"}`}
              style={{ borderBottom: `2px solid ${active ? `var(--${t.tone})` : "transparent"}` }}
            >
              <t.Icon size={12} />
              {t.label}
              <span className="kbd ml-1">{t.hot}</span>
            </button>
          );
        })}
      </div>

      <div className="p-3.5 flex flex-col gap-3 flex-1">
        <div>
          <div className="text-[10.5px] uppercase tracking-wider text-fg-2 mb-1.5">Planner output · awaiting review</div>
          {tab === "edit" ? (
            <textarea
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              spellCheck={false}
              className="w-full min-h-[220px] p-3 mono text-xs leading-relaxed bg-[oklch(0.13_0.005_250)] border border-accent-line rounded-sm text-fg-0 outline-none resize-y"
            />
          ) : (
            <pre className="m-0 p-3 bg-[oklch(0.13_0.005_250)] border border-line-soft rounded-sm mono text-xs leading-relaxed text-fg-0 max-h-[240px] overflow-auto">
              {JSON.stringify(plannerOutput, null, 2)}
            </pre>
          )}
        </div>

        <Card className="bg-bg-2">
          <div className="flex items-start gap-2.5">
            <AlertTriangle size={14} className={`mt-0.5 ${TONE[isSmart ? "info" : "warn"].fg}`} />
            <div className="min-w-0">
              <div className="text-[11.5px] text-fg-0 font-medium">
                {isSmart
                  ? "SmartPause triggered — planner confidence below threshold."
                  : `Checkpoint reached — campaign hitl_mode=${run.hitl_mode} requires explicit go-ahead.`}
              </div>
              <div className="text-[11px] text-fg-2 mt-1 leading-relaxed">
                {isSmart
                  ? `Confidence ${run.confidence?.toFixed(2)} < 0.50 threshold. Generator will not be invoked until an operator approves the plan or applies an edit.`
                  : "post_planner gate is a hard checkpoint. Generator will not run, no tokens will be billed, and the manifest stays open until you act."}
              </div>
            </div>
          </div>
        </Card>

        <div className="flex items-center gap-2 pt-2 border-t border-line-soft">
          <span className="text-[11px] text-fg-3">
            POST <span className="mono text-fg-1">/runs/{shortId(run.id)}/intervene</span>{" "}
            <span className="text-fg-2">{`{action: "${tab}"${tab === "edit" ? ", payload: <edited json>" : ""}}`}</span>
          </span>
          <span className="flex-1" />
          {resolved ? (
            <Badge tone="ok" dot>resolved · {resolved}</Badge>
          ) : (
            <>
              <Button variant="outline" size="md" onClick={() => submit("skip")}>Skip</Button>
              <Button variant="ghost"   size="md" onClick={() => submit("abort")}>Abort run</Button>
              <Button
                variant={tab === "approve" ? "success" : tab === "edit" ? "warn" : "danger"}
                size="md"
                disabled={submitting}
                onClick={() => submit(tab)}
                icon={tab === "approve" ? <Check size={12} /> : tab === "edit" ? <Edit size={12} /> : <X size={12} />}
              >
                {submitting ? "submitting…" : `Confirm ${tab}`}
              </Button>
            </>
          )}
        </div>
      </div>
    </Card>
  );
}
