import type * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * Styled NATIVE `<select>`.
 *
 * Deliberately not a custom listbox: every value this app offers in a
 * select is a short, enumerable, server-known set (project, task, graph
 * source, pipeline, time window), and a native control gets keyboard
 * navigation, type-ahead, mobile pickers and screen-reader semantics for
 * free — with a visible focus ring, which a bare `<select>` in this theme
 * did not reliably have.
 *
 * Exists because Pollen kept re-declaring the same ~6 utility classes
 * inline on raw `<select>` elements (GraphView had two copies); a single
 * primitive keeps the field styling identical to `Input`'s.
 */
function Select({ className, ...props }: React.ComponentProps<'select'>) {
  return (
    <select
      data-slot="select"
      className={cn(
        'h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 text-sm text-foreground transition-colors outline-none',
        'focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50',
        'disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50',
        'dark:bg-input/30',
        className,
      )}
      {...props}
    />
  )
}

export { Select }
