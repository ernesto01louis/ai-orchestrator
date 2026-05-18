import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronRight, Download, Filter as FilterIcon, RefreshCw, Search } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PhaseBadge } from "@/components/PhaseBadge";
import { ScoreBar } from "@/components/ScoreBar";
import { useRuns } from "@/lib/queries";
import { fmtAgo, shortId } from "@/lib/utils";
import { Td, Th } from "./Dashboard";

const PHASES = ["all", "planner", "post_planner", "generator", "judge", "optimizer", "complete", "failed"];
const STATES = ["all", "running", "paused", "complete", "failed"];

export function RunsList() {
  const { data: runs = [], refetch } = useRuns();
  const navigate = useNavigate();
  const [phaseF, setPhaseF] = useState("all");
  const [stateF, setStateF] = useState("all");
  const [q, setQ] = useState("");

  const filtered = runs.filter((r) => {
    if (phaseF !== "all" && r.phase !== phaseF) return false;
    if (stateF === "running" && (r.paused || ["complete", "failed"].includes(r.phase))) return false;
    if (stateF === "paused" && !r.paused) return false;
    if (stateF === "complete" && r.phase !== "complete") return false;
    if (stateF === "failed" && r.phase !== "failed") return false;
    if (q && !(r.id.includes(q) || r.project.includes(q) || r.model.includes(q))) return false;
    return true;
  });

  return (
    <div className="p-5 flex flex-col gap-3.5">
      <div className="flex items-center gap-2 flex-wrap">
        <div className="flex items-center gap-1.5 px-2.5 py-1 bg-bg-1 border border-line-soft rounded-sm min-w-[280px]">
          <Search size={12} className="text-fg-3" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="filter by id, project, model…"
            className="flex-1 bg-transparent border-none outline-none text-fg-0 mono text-[11.5px]"
          />
        </div>
        <FilterPill label="phase" value={phaseF} options={PHASES} onChange={setPhaseF} />
        <FilterPill label="state" value={stateF} options={STATES} onChange={setStateF} />
        <span className="flex-1" />
        <span className="mono text-[11px] text-fg-3">
          {filtered.length} of {runs.length} runs
        </span>
        <Button variant="ghost" size="sm" onClick={() => refetch()} icon={<RefreshCw size={12} />}>refresh</Button>
        <Button variant="outline" size="sm" icon={<Download size={12} />}>export</Button>
      </div>

      <Card padded={false}>
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="bg-bg-1 uppercase text-[10.5px] tracking-wider text-fg-2">
              <Th>Run</Th><Th>Project</Th><Th>Phase</Th><Th>Score</Th>
              <Th>Model</Th><Th>Target</Th><Th>Started</Th><Th>State</Th><Th></Th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => (
              <tr key={r.id} className="row-hover cursor-pointer border-b border-line-soft" onClick={() => navigate(`/runs/${r.id}`)}>
                <Td><span className="mono text-fg-0">{shortId(r.id)}</span></Td>
                <Td><span className="text-fg-1">{r.project}</span></Td>
                <Td><PhaseBadge phase={r.phase} /></Td>
                <Td><ScoreBar score={r.score} /></Td>
                <Td><span className="mono text-fg-1 text-[11px]">{r.model}</span></Td>
                <Td><span className="mono text-fg-2 text-[11px]">{r.target}</span></Td>
                <Td><span className="text-fg-2 text-[11px]">{fmtAgo(r.started_at)}</span></Td>
                <Td>
                  {r.paused ? (
                    <Badge tone={r.paused === "smartpause" ? "info" : "warn"} mono dot>
                      {r.paused === "smartpause" ? "smartpause" : `hitl:${r.hitl_mode}`}
                    </Badge>
                  ) : r.phase === "complete" ? (
                    <Badge tone="muted" mono dot>complete</Badge>
                  ) : r.phase === "failed" ? (
                    <Badge tone="err" mono dot>failed</Badge>
                  ) : (
                    <Badge tone="ok" mono dot>running</Badge>
                  )}
                </Td>
                <Td><ChevronRight size={13} className="text-fg-3" /></Td>
              </tr>
            ))}
          </tbody>
        </table>
        {!filtered.length && (
          <div className="p-8 text-center text-fg-3 text-xs flex flex-col items-center gap-1.5">
            <FilterIcon size={16} />
            No runs match the current filters.
          </div>
        )}
      </Card>
    </div>
  );
}

function FilterPill({
  label, value, options, onChange,
}: { label: string; value: string; options: string[]; onChange: (v: string) => void }) {
  return (
    <div className="flex items-center gap-1.5 pl-2.5 pr-1 py-0.5 bg-bg-1 border border-line-soft rounded-sm text-[11px]">
      <span className="text-fg-2 uppercase tracking-wider text-[10px]">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-transparent text-fg-0 border-none outline-none mono text-[11px] py-0.5 px-1 cursor-pointer"
      >
        {options.map((o) => <option key={o} value={o} className="bg-bg-1">{o}</option>)}
      </select>
    </div>
  );
}
