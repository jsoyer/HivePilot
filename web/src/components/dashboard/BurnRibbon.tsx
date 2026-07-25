import { cn } from '@/lib/utils'

const WIDTH = 100
const HEIGHT = 40
const PAD_X = 2
const PAD_Y = 3

/** Literal Tailwind arbitrary-value classes (see `Sparkline`'s comment on
 * why this can't be a computed template string) — cycles the same
 * `--color-chart-*` palette `DistributionBar` uses, so stacked categories
 * stay on the shared theme instead of introducing new hues. */
const BAND_FILL = [
  'fill-[var(--color-chart-1)]',
  'fill-[var(--color-chart-2)]',
  'fill-[var(--color-chart-3)]',
  'fill-[var(--color-chart-4)]',
  'fill-[var(--color-chart-5)]',
]

export interface BurnRibbonProps {
  /** One sub-array per category, values across the SAME time axis —
   * `series[categoryIndex][timeIndex]`. Bands are stacked (summed) per
   * time step, oldest to newest. */
  series: number[][]
  /** Optional ceiling (budget cap), same units as the stacked totals —
   * drawn as a dashed reference line. */
  ceiling?: number
  className?: string
  ariaLabel?: string
}

/**
 * A stacked-area "burn ribbon" for spend-over-time: one filled band per
 * category, an optional dashed ceiling line, and an emphasized dot on the
 * latest cumulative total. Ragged sub-arrays and missing values are
 * treated as 0 rather than crashing.
 */
export function BurnRibbon({ series, ceiling, className, ariaLabel }: BurnRibbonProps) {
  const steps = series.length === 0 ? 0 : Math.max(0, ...series.map((s) => s.length))

  if (series.length === 0 || steps === 0) {
    return (
      <div
        data-slot="burn-ribbon"
        role="img"
        aria-label={ariaLabel ?? 'No data to display'}
        className={cn('text-sm text-muted-foreground', className)}
      >
        No data yet.
      </div>
    )
  }

  // cumulative[categoryIndex][timeIndex] = running stacked total up to and
  // including that category, at that time step.
  const cumulative: number[][] = []
  let running = new Array(steps).fill(0) as number[]
  for (const categorySeries of series) {
    running = running.map((value, t) => value + (categorySeries[t] ?? 0))
    cumulative.push(running)
  }
  const totals = running
  const max = Math.max(1, ceiling ?? 0, ...totals)
  const stepX = steps > 1 ? (WIDTH - PAD_X * 2) / (steps - 1) : 0

  const xFor = (index: number) => PAD_X + index * stepX
  const yFor = (value: number) => HEIGHT - PAD_Y - (value / max) * (HEIGHT - PAD_Y * 2)

  const zeros = new Array(steps).fill(0) as number[]
  const bands = series.map((_, categoryIndex) => {
    const lower = categoryIndex === 0 ? zeros : cumulative[categoryIndex - 1]
    const upper = cumulative[categoryIndex]
    const topPoints = upper.map((value, t) => `${xFor(t).toFixed(2)},${yFor(value).toFixed(2)}`)
    const bottomPoints = lower
      .map((value, t) => `${xFor(t).toFixed(2)},${yFor(value).toFixed(2)}`)
      .reverse()
    return { key: categoryIndex, d: `M${topPoints.join(' L')} L${bottomPoints.join(' L')} Z` }
  })

  const endpointX = xFor(steps - 1)
  const endpointY = yFor(totals[totals.length - 1])
  const ceilingY = ceiling != null ? yFor(ceiling) : null

  const summary =
    ariaLabel ??
    `Burn ribbon: ${series.length} ${series.length === 1 ? 'category' : 'categories'} over ${steps} steps, latest total ${totals[
      totals.length - 1
    ].toLocaleString('en-US')}`

  return (
    <svg
      data-slot="burn-ribbon"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={summary}
      className={cn('h-16 w-full', className)}
    >
      {bands.map((band) => (
        <path
          key={band.key}
          data-slot="burn-ribbon-band"
          d={band.d}
          stroke="none"
          opacity={0.55}
          className={BAND_FILL[band.key % BAND_FILL.length]}
        />
      ))}
      {ceilingY != null && (
        <line
          data-slot="burn-ribbon-ceiling"
          x1={PAD_X}
          y1={ceilingY}
          x2={WIDTH - PAD_X}
          y2={ceilingY}
          strokeWidth={1}
          strokeDasharray="3 2"
          className="stroke-[var(--color-warn)]"
        />
      )}
      <circle
        data-slot="burn-ribbon-endpoint"
        cx={endpointX}
        cy={endpointY}
        r={2.25}
        className="fill-[var(--color-primary)]"
      />
    </svg>
  )
}
