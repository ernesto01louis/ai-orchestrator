export function ScoreBar({ score, w = 60 }: { score: number | null | undefined; w?: number }) {
  if (score == null) return <span className="mono text-fg-3">—</span>;
  const pct = Math.max(0, Math.min(1, score)) * 100;
  const tone = score >= 0.8 ? "bg-ok" : score >= 0.6 ? "bg-warn" : "bg-err";
  return (
    <span className="inline-flex items-center gap-2">
      <span className="bg-bg-3 rounded-[2px] overflow-hidden" style={{ width: w, height: 4 }}>
        <span className={`block h-full ${tone}`} style={{ width: `${pct}%` }} />
      </span>
      <span className="num text-[11px] text-fg-1">{score.toFixed(2)}</span>
    </span>
  );
}
