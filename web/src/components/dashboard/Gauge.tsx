import { cn } from '@/lib/utils'
import type { VizTone } from './Sparkline'

const SIZE = 120
const CX = SIZE / 2
const CY = SIZE / 2 + 4
const RADIUS = 46
const TICKS = [0, 0.25, 0.5, 0.75, 1]

const TONE_STROKE: Record<VizTone, string> = {
  default: 'stroke-[var(--color-primary)]',
  good: 'stroke-[var(--color-good)]',
  warn: 'stroke-[var(--color-warn)]',
  crit: 'stroke-[var(--color-crit)]',
}

function clamp01(value: number): number {
  if (Number.isNaN(value)) return 0
  return Math.min(1, Math.max(0, value))
}

/** Point on the 180 degree arc (left baseline -> top -> right baseline) for
 * a given `fraction` in [0,1], at `radius` from the gauge center. */
function arcPoint(fraction: number, radius: number): { x: number; y: number } {
  const angle = Math.PI - fraction * Math.PI
  return { x: CX + radius * Math.cos(angle), y: CY - radius * Math.sin(angle) }
}

function arcPath(fromFraction: number, toFraction: number, radius: number): string {
  const start = arcPoint(fromFraction, radius)
  const end = arcPoint(toFraction, radius)
  const largeArc = toFraction - fromFraction > 0.5 ? 1 : 0
  return `M${start.x.toFixed(2)},${start.y.toFixed(2)} A${radius},${radius} 0 ${largeArc} 1 ${end.x.toFixed(2)},${end.y.toFixed(2)}`
}

export interface GaugeProps {
  /** Current value, 0..1 (clamped). */
  value: number
  /** Optional target marker, 0..1 (clamped). */
  target?: number
  label?: string
  tone?: VizTone
  className?: string
}

/**
 * A 180-degree arc dial (not a donut) with tick marks and an optional
 * target tick — for rate-style metrics (cache-hit rate, success rate,
 * budget burn). A mono tabular-figure readout sits under the arc.
 */
export function Gauge({ value, target, label, tone = 'default', className }: GaugeProps) {
  const clampedValue = clamp01(value)
  const trackPath = arcPath(0, 1, RADIUS)
  const valuePath = clampedValue > 0 ? arcPath(0, clampedValue, RADIUS) : null
  const pct = Math.round(clampedValue * 100)

  const ariaLabel = label ? `${label}: ${pct}%` : `Gauge reading ${pct}%`

  return (
    <div data-slot="gauge-wrapper" className={cn('flex flex-col items-center gap-1', className)}>
      <svg
        data-slot="gauge"
        viewBox={`0 0 ${SIZE} ${SIZE / 2 + 10}`}
        role="img"
        aria-label={ariaLabel}
        className="w-full max-w-[180px]"
      >
        <path
          data-slot="gauge-track"
          d={trackPath}
          fill="none"
          strokeWidth={8}
          strokeLinecap="round"
          className="stroke-muted"
        />
        {valuePath && (
          <path
            data-slot="gauge-value-arc"
            d={valuePath}
            fill="none"
            strokeWidth={8}
            strokeLinecap="round"
            className={TONE_STROKE[tone]}
          />
        )}
        {TICKS.map((tick) => {
          const inner = arcPoint(tick, RADIUS - 7)
          const outer = arcPoint(tick, RADIUS + 2)
          return (
            <line
              key={tick}
              data-slot="gauge-tick"
              x1={inner.x}
              y1={inner.y}
              x2={outer.x}
              y2={outer.y}
              strokeWidth={1.5}
              className="stroke-border"
            />
          )
        })}
        {target != null && (
          <line
            data-slot="gauge-target-tick"
            {...(() => {
              const t = clamp01(target)
              const inner = arcPoint(t, RADIUS - 10)
              const outer = arcPoint(t, RADIUS + 6)
              return { x1: inner.x, y1: inner.y, x2: outer.x, y2: outer.y }
            })()}
            strokeWidth={2}
            className="stroke-foreground"
          />
        )}
      </svg>
      <span data-slot="gauge-readout" className="metric-mono text-lg leading-none">
        {pct}%
      </span>
      {label && <span className="text-xs text-muted-foreground">{label}</span>}
    </div>
  )
}
