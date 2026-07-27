import type { ReactNode } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import type { VizTone } from './Sparkline'

const TONE_CLASSES: Record<VizTone, { chip: string; value: string; stripe: string }> = {
  default: { chip: 'bg-muted text-muted-foreground', value: 'text-foreground', stripe: '' },
  good: {
    chip: 'bg-[var(--color-good)]/10 text-[var(--color-good)]',
    value: 'text-[var(--color-good)]',
    stripe: 'border-l-2 border-l-[var(--color-good)]',
  },
  warn: {
    chip: 'bg-[var(--color-warn)]/10 text-[var(--color-warn)]',
    value: 'text-[var(--color-warn)]',
    stripe: 'border-l-2 border-l-[var(--color-warn)]',
  },
  crit: {
    chip: 'bg-[var(--color-crit)]/10 text-[var(--color-crit)]',
    value: 'text-[var(--color-crit)]',
    stripe: 'border-l-2 border-l-[var(--color-crit)]',
  },
}

const TREND_CLASSES: Record<'good' | 'crit', string> = {
  good: 'text-[var(--color-good)]',
  crit: 'text-[var(--color-crit)]',
}

export interface MetricReadoutTrend {
  direction: 'up' | 'down'
  label: ReactNode
  /** Tints the arrow + trend text. Defaults to 'good' for 'up', 'crit' for
   * 'down' — pass explicitly when an "up" trend is actually bad news (e.g.
   * rising error count). */
  tone?: 'good' | 'crit'
}

export interface MetricReadoutProps {
  /** Optional icon rendered inside a rounded-square chip, tinted by `tone`. */
  icon?: ReactNode
  /** Rendered uppercase, muted — e.g. "TOTAL RUNS". */
  label: string
  /** The headline metric, rendered large in the mono tabular-numeral
   * "instrument readout" treatment. */
  value: ReactNode
  /** Optional muted sub-metric line under the value. */
  sub?: ReactNode
  /** Optional trend indicator (▲/▼ + label), rendered alongside `sub`. */
  trend?: MetricReadoutTrend
  /** Tints the icon chip and value text. Defaults to 'default' (neutral). */
  tone?: VizTone
  className?: string
}

/**
 * The "digital readout" primitive — Pollen's instrument-identity evolution
 * of `StatCard`: eyebrow label, a big monospace tabular-figure value, a 1px
 * baseline rule, and an optional trend sub-line (▲/▼, tinted good/crit).
 * Kept ALONGSIDE `StatCard` (not a replacement) so existing call sites can
 * adopt it incrementally; same data shape (icon/label/value/sub/tone) plus
 * an additive `trend` prop.
 */
export function MetricReadout({
  icon,
  label,
  value,
  sub,
  trend,
  tone = 'default',
  className,
}: MetricReadoutProps) {
  const toneClasses = TONE_CLASSES[tone]
  const trendTone = trend?.tone ?? (trend?.direction === 'up' ? 'good' : 'crit')

  return (
    <Card
      data-slot="metric-readout"
      data-tone={tone}
      className={cn('w-full', toneClasses.stripe, className)}
    >
      <CardContent className="flex items-start gap-3">
        {icon && (
          <span
            data-slot="metric-readout-icon"
            className={cn(
              'flex size-9 shrink-0 items-center justify-center rounded-lg',
              toneClasses.chip,
            )}
          >
            {icon}
          </span>
        )}
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <span
            data-slot="metric-readout-label"
            className="text-[10px] font-semibold tracking-wider text-muted-foreground uppercase"
          >
            {label}
          </span>
          {/* Bug fix (permanent clipping): the ancestor `Card` clips
           * overflow (`overflow-hidden`, for its rounded-corner gradient) —
           * without `break-words` a long unbroken value (e.g. a big
           * comma-grouped number) that doesn't fit its column just gets
           * silently sliced mid-character instead of wrapping. */}
          <span
            data-slot="metric-readout-value"
            className={cn('metric-mono text-2xl leading-none font-semibold break-words', toneClasses.value)}
          >
            {value}
          </span>
          <div data-slot="metric-readout-rule" aria-hidden="true" className="h-px w-full bg-border" />
          {(sub != null || trend) && (
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
              {trend && (
                <span
                  data-slot="metric-readout-trend"
                  className={cn('metric-mono inline-flex items-center gap-0.5', TREND_CLASSES[trendTone])}
                >
                  <span aria-hidden="true">{trend.direction === 'up' ? '▲' : '▼'}</span>
                  {trend.label}
                </span>
              )}
              {sub != null && <span data-slot="metric-readout-sub">{sub}</span>}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
