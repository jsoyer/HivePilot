import { useT } from '@/lib/i18n'
import { cn } from '@/lib/utils'

export interface IssuesChipProps {
  count: number
  /** Hide the chip until the first count is known — no "All clear" flash. */
  ready: boolean
  onClick?: () => void
}

/**
 * Single header status chip — "All clear" / "N issues".
 * Count is the Alerts feed (Failed + Degraded + down Classifier).
 * Clicking opens the Alerts door.
 */
export function IssuesChip({ count, ready, onClick }: IssuesChipProps) {
  const t = useT()
  if (!ready) {
    return null
  }

  const label = count === 0 ? t('header.allClear') : t('header.issues', { count })
  const hasIssues = count > 0

  return (
    <button
      type="button"
      data-testid="issues-chip"
      data-count={count}
      onClick={onClick}
      className={cn(
        'touch-target inline-flex h-8 items-center rounded-full border px-3 text-sm font-medium',
        hasIssues
          ? 'border-border bg-card text-foreground'
          : 'border-border bg-card text-muted-foreground',
      )}
    >
      {label}
    </button>
  )
}
