import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

export interface EmptyStateProps {
  /** Optional decorative glyph. Always `aria-hidden` — it repeats the title. */
  icon?: ReactNode
  /** What is empty, stated plainly. */
  title: string
  /** What would fill it, and/or what the operator can do about it. This is
   * the whole point of the component: "Nothing here." tells an operator
   * nothing they didn't already see. */
  body: string
  /** Optional primary action (a Button) that would start filling it. */
  action?: ReactNode
  className?: string
  'data-testid'?: string
}

/**
 * The one empty state used across Pollen.
 *
 * Design rule this encodes: an empty panel must answer "what would fill
 * this, and what can I do?" — never just "nothing". The operator review
 * that prompted this rebuild called out five panels that each said some
 * variant of "Nothing here." and stopped there.
 *
 * Deliberately quiet: dashed hairline, muted text, no accent colour. An
 * empty state is not an alert — it must not compete with a real failure
 * for attention.
 */
export function EmptyState({
  icon,
  title,
  body,
  action,
  className,
  'data-testid': testId,
}: EmptyStateProps) {
  return (
    <div
      data-slot="empty-state"
      data-testid={testId}
      className={cn(
        'flex flex-col items-start gap-2 rounded-lg border border-dashed border-border bg-muted/20 p-4',
        className,
      )}
    >
      <div className="flex items-center gap-2">
        {icon != null && (
          <span data-slot="empty-state-icon" aria-hidden="true" className="text-muted-foreground">
            {icon}
          </span>
        )}
        <span className="text-sm font-medium text-foreground">{title}</span>
      </div>
      <p className="max-w-prose text-sm text-muted-foreground">{body}</p>
      {action != null && (
        <div data-slot="empty-state-action" className="pt-1">
          {action}
        </div>
      )}
    </div>
  )
}
