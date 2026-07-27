import { X } from 'lucide-react'
import { type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { useEscapeKey } from '@/lib/use-escape-key'
import { cn } from '@/lib/utils'

export interface DrawerProps {
  title: ReactNode
  /** Accessible name for the dialog. Defaults to `title` when it is a plain
   * string; required otherwise, because a `ReactNode` title cannot be used
   * as an `aria-label`. */
  ariaLabel?: string
  /** Accessible name for the close control. */
  closeLabel: string
  onClose: () => void
  /** Optional controls rendered in the header, left of the close button. */
  headerAction?: ReactNode
  children: ReactNode
  className?: string
}

/**
 * The right-side drawer used for every Pollen drill-down and every secondary
 * creation flow.
 *
 * Three views had each hand-rolled the same backdrop + fixed panel + close
 * button, and none of them closed on Escape. This one does, and it owns the
 * scroll: the panel scrolls, never the page body — which is also what keeps a
 * wide table inside a drawer from scrolling the whole document sideways.
 *
 * Creation forms live in here rather than inline at the top of a view: an
 * operator opens Pollen to READ state, so a form must never occupy the space
 * where the content belongs.
 */
export function Drawer({
  title,
  ariaLabel,
  closeLabel,
  onClose,
  headerAction,
  children,
  className,
}: DrawerProps) {
  // This component only mounts while open, so the hook is always enabled
  // here. `nav/SidebarNav.tsx` shares the same hook for its own mobile
  // drawer, which stays mounted (translated off-canvas) and so passes its
  // real open state.
  useEscapeKey(true, onClose)

  const label = ariaLabel ?? (typeof title === 'string' ? title : undefined)

  return (
    <>
      <div
        data-slot="drawer-backdrop"
        aria-hidden="true"
        className="fixed inset-0 z-40 bg-black/50"
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={label}
        className={cn(
          'fixed inset-y-0 right-0 z-50 flex w-full max-w-lg flex-col gap-4 overflow-y-auto border-l border-border bg-card p-4 shadow-xl sm:p-6',
          className,
        )}
      >
        <div className="flex items-start justify-between gap-3">
          <h2 className="font-heading text-lg font-semibold">{title}</h2>
          <div className="flex shrink-0 items-center gap-2">
            {headerAction}
            <Button type="button" variant="ghost" size="icon-sm" aria-label={closeLabel} onClick={onClose}>
              <X className="size-4" />
            </Button>
          </div>
        </div>
        {children}
      </div>
    </>
  )
}
