import { cn } from '@/lib/utils'

interface SwitchProps {
  checked: boolean
  onCheckedChange: () => void
  disabled?: boolean
  /** Required. A switch whose only label is its visual state is unusable with
   * a screen reader, and this one turns plugins on and off. */
  'aria-label': string
  title?: string
}

/**
 * A two-state toggle.
 *
 * A real `<button role="switch">` rather than a styled `<div>`: it must be
 * reachable by keyboard and announce its state, and `aria-checked` is what
 * carries that. The visual thumb is decorative and hidden from the
 * accessibility tree.
 *
 * `disabled` covers both "your token may not do this" (non-admin) and "a
 * request is in flight". They look the same to the user and should: in both
 * cases clicking again would do nothing useful.
 */
export function Switch({
  checked,
  onCheckedChange,
  disabled = false,
  'aria-label': ariaLabel,
  title,
}: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      title={title}
      disabled={disabled}
      onClick={onCheckedChange}
      className={cn(
        'inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full border border-transparent transition-colors',
        'focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none',
        'disabled:cursor-not-allowed disabled:opacity-50',
        checked ? 'bg-primary' : 'bg-input dark:bg-input/60',
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          'pointer-events-none block size-5 rounded-full bg-background shadow-sm transition-transform',
          checked ? 'translate-x-5' : 'translate-x-0.5',
        )}
      />
    </button>
  )
}
