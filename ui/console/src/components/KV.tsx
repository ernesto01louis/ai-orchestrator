import type { ReactNode } from "react";

interface Props {
  k: ReactNode;
  v: ReactNode;
  mono?: boolean;
}

export function KV({ k, v, mono = true }: Props) {
  return (
    <div className="flex justify-between py-1.5 border-b border-dashed border-line-soft last:border-0">
      <span className="text-[11px] text-fg-2 uppercase tracking-wider">{k}</span>
      <span className={`text-xs text-fg-0 ${mono ? "mono" : ""}`}>{v}</span>
    </div>
  );
}
