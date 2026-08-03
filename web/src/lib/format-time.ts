/**
 * Shared time formatting for Pollen.
 *
 * Five views had each grown their OWN private copy of `formatAge`/
 * `formatElapsed`/`formatTimestamp` ("each view owns a small copy of this
 * helper" — a convention that drifted into five subtly different em-dash
 * rules). One implementation, one honesty contract:
 *
 * A missing or unparseable instant renders `'—'`, never `0s` and never
 * "now". Timestamps are always converted to the VIEWER's local time zone
 * (the API stores UTC).
 */

export const EM_DASH = '—'

/** Coarsest-useful-unit duration: `3d`, `5h`, `12m`, `45s`. Negative input
 * clamps to `0s` — a clock skew must not print a negative duration. */
export function formatDurationSeconds(totalSeconds: number): string {
  const seconds = Math.max(0, Math.round(totalSeconds))
  const days = Math.floor(seconds / 86400)
  if (days > 0) return `${days}d`
  const hours = Math.floor((seconds % 86400) / 3600)
  if (hours > 0) return `${hours}h`
  const minutes = Math.floor((seconds % 3600) / 60)
  if (minutes > 0) return `${minutes}m`
  return `${seconds}s`
}

/** True when the string already says which zone it is in. */
const HAS_ZONE = /([zZ]|[+-]\d{2}:?\d{2})$/

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null
  // The API returns SQLite `CURRENT_TIMESTAMP` values: `"2026-08-03 09:15:21"`
  // — UTC, but carrying no zone designator. `new Date()` reads that shape as
  // LOCAL time, so every instant in Pollen was shifted by the viewer's UTC
  // offset: a run started seconds ago read "started 2h ago" in Paris, and the
  // clock times shown were UTC wearing a local label.
  //
  // Only a genuinely naive string is stamped; anything that already declares a
  // zone (or an offset) is left exactly as sent.
  const normalised = HAS_ZONE.test(iso) ? iso : `${iso.trim().replace(' ', 'T')}Z`
  const date = new Date(normalised)
  return Number.isNaN(date.getTime()) ? null : date
}

/** Elapsed time from `iso` until now. `'—'` when absent/unparseable. */
export function formatAge(iso: string | null | undefined): string {
  const date = parse(iso)
  if (date === null) return EM_DASH
  return formatDurationSeconds((Date.now() - date.getTime()) / 1000)
}

/** Elapsed time BETWEEN two instants (a finished run's real duration).
 * `'—'` when either end is absent/unparseable — never a half-computed span. */
export function formatElapsed(
  startIso: string | null | undefined,
  endIso: string | null | undefined,
): string {
  const start = parse(startIso)
  const end = parse(endIso)
  if (start === null || end === null) return EM_DASH
  return formatDurationSeconds((end.getTime() - start.getTime()) / 1000)
}

/**
 * Full local date+time. Absent input renders `'—'`; input we cannot parse is
 * passed through VERBATIM rather than replaced — if the backend ever sends a
 * shape this function does not understand, an operator should see the raw
 * value, not a silent em-dash that hides a data bug.
 */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return EM_DASH
  const date = parse(iso)
  return date === null ? iso : date.toLocaleString()
}

/** Local time-of-day only — for dense list rows where the date is implied
 * by context and the full stamp is available on hover/in the drill-down. */
export function formatClock(iso: string | null | undefined): string {
  const date = parse(iso)
  return date === null ? EM_DASH : date.toLocaleTimeString()
}
