import { useEffect, useRef, useState } from "react"

interface SparklineProps {
  data: number[]
  /** Fixed width — falls back to a ResizeObserver-driven width when omitted. */
  width?: number
  height?: number
  /** CSS colour for the stroke (and 10%-opacity area fill). */
  stroke?: string
  /** Disable the area fill underneath the line. */
  area?: boolean
}

/**
 * Sparkline — pure SVG, 1.25px stroke, soft 10% area fill.
 *
 * When `width` is omitted the component measures its container via
 * ResizeObserver and redraws responsively. Useful for metric cards
 * where the available width depends on the grid breakpoint.
 */
export function Sparkline({
  data,
  width,
  height = 28,
  stroke = "var(--accent)",
  area = true,
}: SparklineProps) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [autoW, setAutoW] = useState(160)

  useEffect(() => {
    if (width != null || !ref.current) return
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width
      if (w && w > 4) setAutoW(w)
    })
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [width])

  const w = width ?? autoW
  if (data.length < 2) {
    return (
      <div ref={ref} style={{ width: width ?? "100%", height }} aria-hidden />
    )
  }

  const max = Math.max(...data)
  const min = Math.min(...data)
  const range = max - min || 1
  const step = w / (data.length - 1)
  const pts = data.map((v, i) => [
    i * step,
    height - ((v - min) / range) * (height - 4) - 2,
  ])
  const d = pts
    .map((p, i) => (i === 0 ? `M${p[0]},${p[1]}` : `L${p[0]},${p[1]}`))
    .join(" ")
  const areaD = `${d} L${w},${height} L0,${height} Z`

  const svg = (
    <svg width={w} height={height} className="block overflow-visible">
      {area && <path d={areaD} fill={stroke} opacity="0.10" />}
      <path
        d={d}
        fill="none"
        stroke={stroke}
        strokeWidth="1.25"
        strokeLinejoin="round"
      />
    </svg>
  )

  return width != null ? svg : <div ref={ref} className="w-full">{svg}</div>
}
