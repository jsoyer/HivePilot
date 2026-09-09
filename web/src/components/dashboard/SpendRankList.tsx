import { ProviderMark } from '@/components/dashboard/ProviderMark'
import { formatCompactCount, formatCostUsd } from '@/lib/format-usage'
import type { UsageLocale } from '@/lib/format-usage'
import { cn } from '@/lib/utils'

export interface SpendRankRow {
  key: string
  label: string
  provider?: string
  tokens: number
  costUsd: number
}

export function SpendRankList({
  rows,
  locale,
  testId,
  onRowClick,
}: {
  rows: SpendRankRow[]
  locale: UsageLocale
  testId: string
  onRowClick?: (row: SpendRankRow) => void
}) {
  return (
    <ul data-testid={testId} className="divide-y divide-border">
      {rows.map((row) => {
        const markName = row.provider ?? row.label
        const content = (
          <>
            <ProviderMark name={markName} />
            <span className="min-w-0 flex-1 truncate font-medium">{row.label}</span>
            <span className="metric-mono shrink-0 text-muted-foreground">
              {formatCompactCount(row.tokens, locale)}
            </span>
            <span className="metric-mono w-[5.5rem] shrink-0 text-right font-semibold">
              {formatCostUsd(row.costUsd)}
            </span>
          </>
        )
        if (onRowClick) {
          return (
            <li key={row.key}>
              <button
                type="button"
                data-testid={`${testId}-row-${row.key}`}
                onClick={() => onRowClick(row)}
                className={cn(
                  'flex w-full items-center gap-2.5 py-2.5 text-left text-sm',
                  'hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50',
                )}
              >
                {content}
              </button>
            </li>
          )
        }
        return (
          <li
            key={row.key}
            data-testid={`${testId}-row-${row.key}`}
            className="flex items-center gap-2.5 py-2.5 text-sm"
          >
            {content}
          </li>
        )
      })}
    </ul>
  )
}
