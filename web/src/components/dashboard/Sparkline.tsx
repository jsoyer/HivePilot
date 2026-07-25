import { cn } from '@/lib/utils'

/** Shared tone vocabulary for every viz primitive (Sparkline/Gauge/BurnRibbon/
 * MetricReadout) — maps onto the `--good`/`--warn`/`--crit`/`--primary`
 * instrument-identity tokens declared in `src/index.css`. `'default'` is the
 * neutral brand-accent (primary/beacon) tone. */
export type VizTone = 'default' | 'good' | 'warn' | 'crit'

const WIDTH = 100
const HEIGHT = 32
const PAD_X = 2
const PAD_Y = 3

/** Literal (not computed) Tailwind arbitrary-value classes — Tailwind's
 * scanner needs the class text to appear verbatim in source, so this must
 * be a lookup table rather than a template string (same pattern as
 * `DistributionBar`'s `DEFAULT_PALETTE`). */
const TONE_STROKE: Record<VizTone, string> = {
  default: 'stroke-[var(--color-primary)]',
  good: 'stroke-[var(--color-good)]',
  warn: 'stroke-[var(--color-warn)]',
  crit: 'stroke-[var(--color-crit)]',
}

const TONE_FILL: Record<VizTone, string> = {
  default: 'fill-[var(--color-primary)]',
  good: 'fill-[var(--color-good)]',
  warn: 'fill-[var(--color-warn)]',
  crit: 'fill-[var(--color-crit)]',
}

export interface SparklineProps {
  /** Values plotted left-to-right, oldest to newest. */
  points: number[]
  tone?: VizTone
  ariaLabel?: string
  className?: string
}

/**
 * A minimal inline-SVG trend line: a thin stroked line, a faint area fill
 * beneath it, and an emphasized dot on the latest value. No chart library,
 * no fixed pixel size (viewBox-scaled to its container). Theme-aware via
 * the same kind of arbitrary-value `stroke`/`fill` Tailwind classes (each
 * pinned to a literal CSS custom property token, see `TONE_STROKE`/
 * `TONE_FILL` below) the rest of the dashboard already uses (see
 * `DistributionBar`/`charts.tsx`) — no JS color reading needed, so it
 * repaints for free across the light/dark toggle.
 */
export function Sparkline({ points, tone = 'default', ariaLabel, className }: SparklineProps) {
  if (points.length === 0) {
    return (
      <div
        data-slot="sparkline"
        role="img"
        aria-label={ariaLabel ?? 'No data to display'}
        className={cn('flex h-8 w-full items-center text-xs text-muted-foreground', className)}
      >
        —
      </div>
    )
  }

  const min = Math.min(...points)
  const max = Math.max(...points)
  const range = max - min || 1
  const stepX = points.length > 1 ? (WIDTH - PAD_X * 2) / (points.length - 1) : 0

  const coords = points.map((value, index) => {
    const x = PAD_X + index * stepX
    const y = HEIGHT - PAD_Y - ((value - min) / range) * (HEIGHT - PAD_Y * 2)
    return [x, y] as const
  })

  const linePath = coords
    .map(([x, y], index) => `${index === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`)
    .join(' ')
  const [firstX] = coords[0]
  const [lastX, lastY] = coords[coords.length - 1]
  const areaPath = `${linePath} L${lastX.toFixed(2)},${HEIGHT} L${firstX.toFixed(2)},${HEIGHT} Z`

  const summary =
    ariaLabel ??
    `Trend: ${points.length} points, from ${min.toLocaleString('en-US')} to ${max.toLocaleString(
      'en-US',
    )}, latest ${points[points.length - 1].toLocaleString('en-US')}`

  return (
    <svg
      data-slot="sparkline"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={summary}
      className={cn('h-8 w-full', className)}
    >
      <path d={areaPath} className={TONE_FILL[tone]} stroke="none" opacity={0.12} />
      <path
        d={linePath}
        className={TONE_STROKE[tone]}
        fill="none"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
      <circle cx={lastX} cy={lastY} r={2.25} className={TONE_FILL[tone]} />
    </svg>
  )
}
