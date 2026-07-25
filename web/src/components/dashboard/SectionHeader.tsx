import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

export interface SectionHeaderProps {
  /** The "instrument" section number — e.g. `"01"`. A plain string (not a
   * computed sequence) so call sites stay in control of numbering across a
   * page that may render a variable set of sections. */
  index: string
  title: string
  /** Optional right-aligned meta text — e.g. "streaming · 12s ago". Omitted
   * entirely (never a fabricated placeholder) when a section has nothing
   * honest to report there. */
  meta?: ReactNode
  className?: string
}

/**
 * The IA/Cyber identity's numbered section header (mirrors the reference
 * mockup's `.sec` — `01 Posture`, `02 Flow map`, ...): a phosphor-toned mono
 * index, the section title, and an optional right-aligned meta line. Purely
 * presentational — reused above any Home/Federation/Operate section that
 * wants the mockup's instrument-panel identity instead of a plain
 * `CardTitle`.
 */
export function SectionHeader({ index, title, meta, className }: SectionHeaderProps) {
  return (
    <div data-slot="section-header" className={cn('flex items-baseline gap-3', className)}>
      <span
        data-slot="section-header-index"
        aria-hidden="true"
        className="metric-mono text-xs text-[var(--color-good)]"
      >
        {index}
      </span>
      <h3 className="text-sm font-semibold tracking-wide text-foreground">{title}</h3>
      {meta != null && (
        <span data-slot="section-header-meta" className="metric-mono ml-auto text-xs text-muted-foreground">
          {meta}
        </span>
      )}
    </div>
  )
}
