import type { ReactNode } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'

export type StatCardTone = 'default' | 'positive' | 'warning' | 'danger'

const TONE_CLASSES: Record<StatCardTone, { chip: string; value: string }> = {
  default: { chip: 'bg-muted text-muted-foreground', value: 'text-foreground' },
  positive: { chip: 'bg-emerald-500/10 text-emerald-500', value: 'text-emerald-500' },
  warning: { chip: 'bg-amber-500/10 text-amber-500', value: 'text-amber-500' },
  danger: { chip: 'bg-destructive/10 text-destructive', value: 'text-destructive' },
}

export interface StatCardProps {
  /** Optional icon rendered inside a rounded-square chip, tinted by `tone`. */
  icon?: ReactNode
  /** Rendered uppercase, muted — e.g. "TOTAL RUNS". */
  label: string
  /** The headline metric, rendered large and mono. */
  value: ReactNode
  /** Optional muted sub-metric line under the value. */
  sub?: ReactNode
  /** Tints the icon chip and value text. Defaults to 'default' (neutral). */
  tone?: StatCardTone
  className?: string
}

/**
 * A single dashboard stat: icon chip + uppercase label + big value + an
 * optional sub-metric line. Reusable building block for Pollen's
 * "Vigie"-style dashboard (Analytics, Cost, Health, Runs).
 */
export function StatCard({ icon, label, value, sub, tone = 'default', className }: StatCardProps) {
  const toneClasses = TONE_CLASSES[tone]

  return (
    <Card data-slot="stat-card" data-tone={tone} className={cn('w-full', className)}>
      <CardContent className="flex items-start gap-3">
        {icon && (
          <span
            data-slot="stat-card-icon"
            className={cn(
              'flex size-9 shrink-0 items-center justify-center rounded-lg',
              toneClasses.chip
            )}
          >
            {icon}
          </span>
        )}
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span
            data-slot="stat-card-label"
            className="text-xs font-medium tracking-wide text-muted-foreground uppercase"
          >
            {label}
          </span>
          {/* visual identity: the shared `metric-mono` utility (mono +
           * tabular-nums, see `src/index.css`) — same instrument-readout
           * numeral treatment `MetricReadout`/`Gauge` already use, so every
           * metric value across the dashboard reads consistently and never
           * jitters width as digits change. */}
          {/* Bug fix (permanent clipping): see MetricReadout's identical
           * comment — `break-words` stops the ancestor Card's
           * `overflow-hidden` from silently slicing an oversized value. */}
          <span
            data-slot="stat-card-value"
            className={cn('metric-mono text-3xl leading-none break-words', toneClasses.value)}
          >
            {value}
          </span>
          {sub != null && (
            <span data-slot="stat-card-sub" className="text-xs text-muted-foreground">
              {sub}
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
