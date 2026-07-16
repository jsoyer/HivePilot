import type { ReactNode } from 'react'
import { describeApiError } from '@/lib/format-error'
import type { AsyncState } from '@/lib/use-async-data'

interface AsyncSectionProps<T> {
  state: AsyncState<T>
  /** Whether the resolved data should render as the "no data yet" empty state. */
  isEmpty: (data: T) => boolean
  emptyMessage?: string
  /** Override the default `describeApiError`-based message (e.g. Mem0's
   * dedicated admin-required card, which does NOT go through this — it
   * short-circuits before rendering `AsyncSection` at all). */
  errorMessage?: (error: unknown) => string
  children: (data: T) => ReactNode
}

/**
 * Shared loading/error/empty/data renderer for one async section of a
 * Mirador view. Every tab is built from one or more of these so no panel
 * can ever render a blank screen or leave an unhandled promise rejection
 * silently on screen.
 */
export function AsyncSection<T>({
  state,
  isEmpty,
  emptyMessage = 'No data yet.',
  errorMessage,
  children,
}: AsyncSectionProps<T>) {
  if (state.status === 'loading') {
    return (
      <div role="status" className="animate-pulse text-sm text-muted-foreground">
        Loading…
      </div>
    )
  }

  if (state.status === 'error') {
    const message = errorMessage ? errorMessage(state.error) : describeApiError(state.error)
    return (
      <div
        role="alert"
        className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
      >
        {message}
      </div>
    )
  }

  if (isEmpty(state.data)) {
    return <p className="text-sm text-muted-foreground">{emptyMessage}</p>
  }

  return <>{children(state.data)}</>
}
