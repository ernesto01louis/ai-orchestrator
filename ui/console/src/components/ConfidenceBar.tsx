import { TONE, type Tone } from "@/components/ui/badge";

// SmartPause (Phase 3.2) confidence visualizer.
// Threshold scheme: >=0.7 ok | 0.4-0.7 warn | <0.4 err — matches
// the gate config in core/hitl.py and the planner schema clamp.
export function ConfidenceBar({ value, w = 80 }: { value: number; w?: number }) {
  const tone: Tone = value >= 0.7 ? "ok" : value >= 0.4 ? "warn" : "err";
  const c = TONE[tone];
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="bg-bg-3 rounded-[2px] overflow-hidden" style={{ width: w, height: 4 }}>
        <span className={`block h-full ${c.dot}`} style={{ width: `${value * 100}%` }} />
      </span>
      <span className={`num text-[10.5px] ${c.fg}`}>{value.toFixed(2)}</span>
    </span>
  );
}
