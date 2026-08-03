import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { formatAge, formatDurationSeconds, formatElapsed, formatTimestamp } from './format-time'

describe('formatDurationSeconds', () => {
  it('picks the coarsest useful unit', () => {
    expect(formatDurationSeconds(45)).toBe('45s')
    expect(formatDurationSeconds(90)).toBe('1m')
    expect(formatDurationSeconds(3600 * 5)).toBe('5h')
    expect(formatDurationSeconds(86400 * 3)).toBe('3d')
  })

  it('never returns a negative duration', () => {
    expect(formatDurationSeconds(-10)).toBe('0s')
  })
})

describe('formatAge', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T12:00:00Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('measures elapsed time from the given instant to now', () => {
    expect(formatAge('2026-01-01T11:58:00Z')).toBe('2m')
  })

  it('renders an em-dash for missing or unparseable input, never a fabricated 0', () => {
    expect(formatAge(null)).toBe('—')
    expect(formatAge(undefined)).toBe('—')
    expect(formatAge('not a date')).toBe('—')
  })

  it('clamps a future timestamp to 0s rather than reporting a negative age', () => {
    expect(formatAge('2026-01-01T12:05:00Z')).toBe('0s')
  })
})

describe('formatElapsed', () => {
  it('measures the span between two instants', () => {
    expect(formatElapsed('2026-01-01T12:00:00Z', '2026-01-01T12:00:08Z')).toBe('8s')
  })

  it('renders an em-dash when either end is missing or unparseable', () => {
    expect(formatElapsed(null, '2026-01-01T12:00:08Z')).toBe('—')
    expect(formatElapsed('2026-01-01T12:00:00Z', null)).toBe('—')
    expect(formatElapsed('nope', 'nope')).toBe('—')
  })
})

describe('formatTimestamp', () => {
  it('renders a parseable instant in the viewer local time zone', () => {
    // Rendered via `toLocaleString`, so assert against the same conversion
    // rather than hardcoding a zone-dependent string.
    const iso = '2026-01-01T12:00:00Z'
    expect(formatTimestamp(iso)).toBe(new Date(iso).toLocaleString())
  })

  it('renders an em-dash for missing input', () => {
    expect(formatTimestamp(null)).toBe('—')
    expect(formatTimestamp(undefined)).toBe('—')
  })

  it('passes unparseable input through verbatim rather than inventing a date', () => {
    expect(formatTimestamp('whenever')).toBe('whenever')
  })
})

describe('formatClock', () => {
  it('renders only the local time-of-day for a compact card line', async () => {
    const { formatClock } = await import('./format-time')
    const iso = '2026-01-01T12:34:56Z'
    expect(formatClock(iso)).toBe(new Date(iso).toLocaleTimeString())
    expect(formatClock(null)).toBe('—')
  })
})

describe('a naive API timestamp is UTC, not local', () => {
  // The API returns SQLite `CURRENT_TIMESTAMP`: UTC with no zone designator.
  // Reading it as local shifted every instant in Pollen by the viewer's
  // offset — a run started seconds ago showed "started 2h ago" in Paris.

  it('does not inflate the age by the local UTC offset', () => {
    const now = new Date('2026-08-03T09:15:41Z')
    vi.useFakeTimers()
    vi.setSystemTime(now)
    try {
      // 20 seconds before `now`, in the shape the API actually sends.
      expect(formatAge('2026-08-03 09:15:21')).toBe('20s')
    } finally {
      vi.useRealTimers()
    }
  })

  it('leaves a string that already declares its zone alone', () => {
    const withZ = formatElapsed('2026-08-03T09:00:00Z', '2026-08-03T09:01:00Z')
    const withOffset = formatElapsed('2026-08-03T11:00:00+02:00', '2026-08-03T11:01:00+02:00')
    expect(withZ).toBe('1m')
    expect(withOffset).toBe('1m')
  })

  it('measures a span between two naive stamps consistently', () => {
    expect(formatElapsed('2026-08-03 09:15:21', '2026-08-03 09:18:21')).toBe('3m')
  })

  it('still refuses an unparseable instant', () => {
    expect(formatAge('not a date')).toBe('—')
  })
})
