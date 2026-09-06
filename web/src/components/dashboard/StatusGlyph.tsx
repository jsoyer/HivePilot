import { Circle, CircleAlert, CircleCheck, CircleDashed, Eye, Loader2 } from 'lucide-react'
import type { ComponentType } from 'react'
import { type AttentionZone, attentionZone } from '@/lib/status-contract'
import { cn } from '@/lib/utils'

/**
 * A single, glanceable status glyph in one fixed slot (HP-44) — the attention
 * system for the live board (Agent-Orchestrator's "spinner > icon > dot"
 * idea, adapted). The glyph is driven purely by the shared derived-status
 * contract's attention ZONE (`@/lib/status-contract`), so it always agrees
 * with the board columns and the Home attention widgets:
 *
 *   working   → a spinning loader (accent)         — an agent is running it
 *   needs_you → a pulsing alert (crit)             — a failure or a decision
 *   in_review → an eye (warn)                      — under review
 *   ready     → a check (good)                     — finished successfully
 *   queued    → a dashed ring (muted)              — accepted, not started
 *   other     → a faint dot (muted)                — paused / cancelled / …
 *
 * Decorative by default (`aria-hidden`): every place it renders also shows the
 * status in text (a badge or a label), which is the accessible source. Pass a
 * `label` to make it an announced `img` where no adjacent text exists.
 */
interface ZoneStyle {
  Icon: ComponentType<{ className?: string }>
  className: string
  spin?: boolean
  pulse?: boolean
}

const ZONE_STYLE: Record<AttentionZone, ZoneStyle> = {
  working: { Icon: Loader2, className: 'text-primary', spin: true },
  needs_you: { Icon: CircleAlert, className: 'text-[var(--color-crit)]', pulse: true },
  in_review: { Icon: Eye, className: 'text-[var(--color-warn)]' },
  ready: { Icon: CircleCheck, className: 'text-[var(--color-good)]' },
  queued: { Icon: CircleDashed, className: 'text-muted-foreground' },
  other: { Icon: Circle, className: 'text-muted-foreground/60' },
}

export interface StatusGlyphProps {
  status: string
  className?: string
  /** When set, the glyph is announced (role="img") with this label instead of
   * being decorative — use where no adjacent status text exists. */
  label?: string
}

export function StatusGlyph({ status, className, label }: StatusGlyphProps) {
  const zone = attentionZone(status)
  const { Icon, className: tone, spin, pulse } = ZONE_STYLE[zone]
  return (
    <Icon
      data-testid="status-glyph"
      data-zone={zone}
      {...(label ? { role: 'img', 'aria-label': label } : { 'aria-hidden': true })}
      className={cn(
        'size-3.5 shrink-0',
        tone,
        spin && 'animate-spin',
        pulse && 'animate-pulse',
        className,
      )}
    />
  )
}
