import { useEffect, useRef, useState } from "react";

interface Props {
  data: number[];
  height?: number;
  stroke?: string;
  area?: boolean;
}

/** Responsive sparkline — measures its container with ResizeObserver. */
export function Sparkline({ data, height = 28, stroke = "var(--accent)", area = true }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(160);

  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((entries) => setW(entries[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  if (!data?.length) return <div ref={ref} style={{ height }} />;

  const max = Math.max(...data);
  const min = Math.min(...data);
  const range = max - min || 1;
  const step = w / Math.max(1, data.length - 1);
  const pts = data.map((v, i) => [i * step, height - ((v - min) / range) * (height - 4) - 2] as const);
  const d = pts.map((p, i) => (i === 0 ? `M${p[0]},${p[1]}` : `L${p[0]},${p[1]}`)).join(" ");
  const areaD = `${d} L${w},${height} L0,${height} Z`;

  return (
    <div ref={ref} className="w-full">
      <svg width={w} height={height} className="block overflow-visible">
        {area && <path d={areaD} fill={stroke} opacity={0.1} />}
        <path d={d} fill="none" stroke={stroke} strokeWidth={1.25} strokeLinejoin="round" />
      </svg>
    </div>
  );
}
