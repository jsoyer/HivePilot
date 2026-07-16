import type { DurationStats, TrendPoint } from '@/lib/mirador-api'

/**
 * Compact inline-SVG bar chart for a run-volume trend over time. No charting
 * library — this is the "otherwise render a compact inline SVG/bars" path
 * from the sprint spec, kept deliberately small to avoid bloating the
 * bundle.
 */
export function TrendBarChart({ series }: { series: TrendPoint[] }) {
  const barWidth = 18
  const gap = 6
  const chartHeight = 90
  const max = Math.max(1, ...series.map((point) => point.total))

  return (
    <figure className="w-full">
      <svg
        viewBox={`0 0 ${series.length * (barWidth + gap)} ${chartHeight}`}
        preserveAspectRatio="none"
        className="h-24 w-full"
        role="img"
        aria-label="Run volume trend over time"
      >
        {series.map((point, index) => {
          const height = Math.max(1, (point.total / max) * (chartHeight - 4))
          return (
            <g key={point.bucket}>
              <title>
                {`${point.bucket}: ${point.total} runs (${point.outcomes.succeeded} succeeded, ${point.outcomes.failed} failed)`}
              </title>
              <rect
                x={index * (barWidth + gap)}
                y={chartHeight - height}
                width={barWidth}
                height={height}
                rx={2}
                className="fill-[var(--color-chart-2)]"
              />
            </g>
          )
        })}
      </svg>
      <figcaption className="mt-1 flex justify-between text-xs text-muted-foreground">
        <span>{series[0]?.bucket}</span>
        {series.length > 1 && <span>{series[series.length - 1]?.bucket}</span>}
      </figcaption>
    </figure>
  )
}

/** Compact horizontal-bar rendering of p50/p95/p99 duration percentiles. */
export function PercentileBars({ stats }: { stats: DurationStats }) {
  const rows: { label: string; value: number }[] = [
    { label: 'p50', value: stats.p50 },
    { label: 'p95', value: stats.p95 },
    { label: 'p99', value: stats.p99 },
  ]
  const max = Math.max(1, stats.p99)

  return (
    <div className="flex flex-col gap-1.5" role="img" aria-label="Duration percentiles">
      {rows.map((row) => (
        <div key={row.label} className="flex items-center gap-2 text-xs">
          <span className="w-8 text-muted-foreground">{row.label}</span>
          <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-[var(--color-chart-3)]"
              style={{ width: `${Math.min(100, (row.value / max) * 100)}%` }}
            />
          </div>
          <span className="w-14 text-right tabular-nums">{row.value.toFixed(2)}s</span>
        </div>
      ))}
    </div>
  )
}
