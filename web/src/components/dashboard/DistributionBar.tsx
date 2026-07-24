import { cn } from '@/lib/utils'

export interface DistributionSegment {
  key: string
  label: string
  value: number
  /** Tailwind background class for the segment + legend dot. Falls back to
   * the default theme-chart palette (cycled) when omitted. */
  colorClass?: string
}

export interface DistributionBarProps {
  segments: DistributionSegment[]
  /** Denominator for proportions. Defaults to the sum of segment values —
   * pass explicitly when the segments don't sum to the true total. */
  total?: number
  className?: string
}

/** Theme-consistent fallback palette, reusing the existing `--color-chart-*`
 * tokens so segments stay on-theme in both light and dark mode without
 * introducing new color values. */
const DEFAULT_PALETTE = [
  'bg-[var(--color-chart-1)]',
  'bg-[var(--color-chart-2)]',
  'bg-[var(--color-chart-3)]',
  'bg-[var(--color-chart-4)]',
  'bg-[var(--color-chart-5)]',
]

function formatPercent(value: number, total: number): string {
  return `${Math.round((value / total) * 100)}%`
}

/**
 * A single segmented horizontal bar (proportional by value) with a
 * wrap-friendly legend row below it — colored dot + label + count per
 * segment. Zero/empty input renders a muted empty state instead of a
 * degenerate bar.
 */
export function DistributionBar({ segments, total, className }: DistributionBarProps) {
  const computedTotal = total ?? segments.reduce((sum, segment) => sum + segment.value, 0)

  if (segments.length === 0 || computedTotal <= 0) {
    return (
      <div
        data-slot="distribution-bar"
        role="img"
        aria-label="No data to display"
        className={cn('text-sm text-muted-foreground', className)}
      >
        No data yet.
      </div>
    )
  }

  const ariaLabel = segments
    .map((segment) => `${segment.label} ${formatPercent(segment.value, computedTotal)}`)
    .join(', ')

  return (
    <div data-slot="distribution-bar" className={cn('flex flex-col gap-2', className)}>
      <div
        role="img"
        aria-label={`Distribution: ${ariaLabel}`}
        className="flex h-2.5 w-full overflow-hidden rounded-full bg-muted"
      >
        {segments.map((segment, index) => {
          const percent = (segment.value / computedTotal) * 100
          if (percent <= 0) return null
          const colorClass = segment.colorClass ?? DEFAULT_PALETTE[index % DEFAULT_PALETTE.length]
          return (
            <div
              key={segment.key}
              data-slot="distribution-bar-segment"
              data-key={segment.key}
              data-percent={percent.toFixed(2)}
              className={cn('h-full', colorClass)}
              style={{ width: `${percent}%` }}
            />
          )
        })}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1.5">
        {segments.map((segment, index) => {
          const colorClass = segment.colorClass ?? DEFAULT_PALETTE[index % DEFAULT_PALETTE.length]
          return (
            <div key={segment.key} className="flex items-center gap-1.5 text-xs">
              <span
                aria-hidden="true"
                data-slot="distribution-bar-dot"
                className={cn('size-2 rounded-full', colorClass)}
              />
              <span className="text-muted-foreground">{segment.label}</span>
              <span className="font-mono tabular-nums">{segment.value.toLocaleString('en-US')}</span>
              <span className="text-muted-foreground">
                ({formatPercent(segment.value, computedTotal)})
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
