import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Box, GitCommit, History, Layers, RefreshCw } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useActivity } from "@/lib/queries";
import { fmtAgo } from "@/lib/utils";
import type { TimelineEvent, TimelineEventType } from "@/lib/types";

const TYPES: Array<"all" | TimelineEventType> = ["all", "run", "campaign", "git"];

const TYPE_ICON: Record<TimelineEventType, typeof Layers> = {
  run: Layers,
  campaign: Box,
  git: GitCommit,
};

const TYPE_TONE: Record<TimelineEventType, "ok" | "info" | "muted"> = {
  run: "ok",
  campaign: "info",
  git: "muted",
};

/**
 * Activity timeline — a chronological feed interleaving run completions,
 * campaign lifecycle, and git commits (GET /activity). One scrollable
 * view of "what changed, when, why", each entry linking to its detail.
 */
export function Timeline() {
  const { data: events = [], refetch, isLoading } = useActivity();
  const navigate = useNavigate();
  const [typeF, setTypeF] = useState<"all" | TimelineEventType>("all");

  const filtered = events.filter((e) => typeF === "all" || e.type === typeF);

  return (
    <div className="p-5 flex flex-col gap-3.5">
      <div className="flex items-center gap-2 flex-wrap">
        <div className="flex items-center gap-1.5 pl-2.5 pr-1 py-0.5 bg-bg-1 border border-line-soft rounded-sm text-[11px]">
          <span className="text-fg-2 uppercase tracking-wider text-[10px]">type</span>
          <select
            value={typeF}
            onChange={(e) => setTypeF(e.target.value as "all" | TimelineEventType)}
            className="bg-transparent text-fg-0 border-none outline-none mono text-[11px] py-0.5 px-1 cursor-pointer"
          >
            {TYPES.map((t) => (
              <option key={t} value={t} className="bg-bg-1">{t}</option>
            ))}
          </select>
        </div>
        <span className="flex-1" />
        <span className="mono text-[11px] text-fg-3">
          {filtered.length} of {events.length} events
        </span>
        <Button variant="ghost" size="sm" onClick={() => refetch()} icon={<RefreshCw size={12} />}>
          refresh
        </Button>
      </div>

      <Card padded={false}>
        <ul className="flex flex-col">
          {filtered.map((e) => (
            <TimelineRow key={e.id} event={e} onOpen={() => {
              if (e.link) navigate(e.link.id ? `${e.link.path}/${e.link.id}` : e.link.path);
            }} />
          ))}
        </ul>
        {!filtered.length && (
          <div className="p-8 text-center text-fg-3 text-xs flex flex-col items-center gap-1.5">
            <History size={16} />
            {isLoading ? "Loading activity…" : "No events in the current window."}
          </div>
        )}
      </Card>
    </div>
  );
}

function TimelineRow({ event, onOpen }: { event: TimelineEvent; onOpen: () => void }) {
  const Icon = TYPE_ICON[event.type];
  const clickable = event.link !== null;
  return (
    <li
      onClick={clickable ? onOpen : undefined}
      className={
        "flex items-start gap-3 px-4 py-2.5 border-b border-line-soft" +
        (clickable ? " row-hover cursor-pointer" : "")
      }
    >
      <span className="mt-0.5 text-fg-2">
        <Icon size={14} />
      </span>
      <div className="flex-1 min-w-0">
        <div className="text-[12.5px] text-fg-0 truncate">{event.title}</div>
        <div className="mono text-[11px] text-fg-3 truncate">{event.details}</div>
        <div className="flex items-center gap-1 mt-1 flex-wrap">
          <Badge tone={TYPE_TONE[event.type]} mono dot>{event.type}</Badge>
          {event.tags.slice(0, 4).map((t) => (
            <Badge key={t} tone="muted" mono>{t}</Badge>
          ))}
        </div>
      </div>
      <span className="text-fg-3 text-[11px] whitespace-nowrap mt-0.5">
        {fmtAgo(event.timestamp)}
      </span>
    </li>
  );
}
