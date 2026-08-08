import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiForbiddenError } from '@/lib/api'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type { EfficiencySummary } from '@/lib/pollen-api'

const mocks = vi.hoisted(() => ({
  fetchEfficiency: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, ...mocks }
})

import { EfficiencyView } from './EfficiencyView'

let container: HTMLDivElement
let root: Root

const efficiency: EfficiencySummary = {
  headroom: {
    total_compressions: 42,
    chars_saved: 12000,
    avg_ratio: 0.6,
    p95_ratio: 0.85,
    est_tokens_saved: 3000,
  },
  proxy: null,
  rtk: {
    gain_pct: 62.5,
    tokens_saved: 15000,
    total_commands: 320,
    saved_series: [
      { date: '2026-07-18', saved_tokens: 500 },
      { date: '2026-07-19', saved_tokens: 900 },
    ],
    top_commands: null,
  },
}

function mount() {
  act(() => {
    root.render(<EfficiencyView />)
  })
}

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockReset()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
  vi.restoreAllMocks()
})

describe('EfficiencyView — compression proxy', () => {
  it('says the proxy is not answering rather than showing a fabricated 0%', async () => {
    // A proxy nobody can reach is a degraded system; a proxy reporting zero
    // saved is a working one with nothing to compress. Showing "$0.0000" for
    // both would hide the first behind the second.
    mocks.fetchEfficiency.mockResolvedValue({ ...efficiency, proxy: null })
    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="efficiency-proxy-empty"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="efficiency-proxy-section"]')).toBeNull()
  })

  it('reports what the proxy actually did when it answers', async () => {
    mocks.fetchEfficiency.mockResolvedValue({
      ...efficiency,
      proxy: {
        summary: {
          api_requests: 10,
          mode: 'token',
          compression: {
            requests_compressed: 10,
            total_tokens_removed: 15493,
            avg_compression_pct: 3.1,
          },
          cost: { without_headroom_usd: 0.4, with_headroom_usd: 0.37, total_saved_usd: 0.03 },
        },
      },
    })
    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const section = container.querySelector('[data-testid="efficiency-proxy-section"]')
    expect(section).not.toBeNull()
    expect(section?.textContent).toContain('15,493')
    expect(section?.textContent).toContain('token')
    expect(section?.textContent).toContain('$0.0300')
  })

  it('survives a server too old to send the field at all', async () => {
    // Not the same as `null`. A server that predates this field omits it, and
    // an `=== null` guard would fall straight through to `proxy.summary` and
    // crash the panel on a payload that is merely out of date.
    const { proxy: _dropped, ...withoutProxy } = efficiency
    mocks.fetchEfficiency.mockResolvedValue(withoutProxy)
    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="efficiency-proxy-empty"]')).not.toBeNull()
  })

  it('survives a proxy payload missing the fields it wants', async () => {
    // The shape belongs to the proxy, not to us. A version bump that drops
    // or renames a field must degrade to zeros, not blank the panel.
    mocks.fetchEfficiency.mockResolvedValue({ ...efficiency, proxy: {} })
    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="efficiency-proxy-section"]')).not.toBeNull()
  })
})

describe('EfficiencyView', () => {
  it('shows a loading indicator before data resolves', () => {
    mocks.fetchEfficiency.mockReturnValue(new Promise(() => {}))
    mount()
    expect(container.querySelectorAll('[role="status"]').length).toBeGreaterThan(0)
  })

  it('renders headroom gauges/readouts and the rtk gain + saved series when both are present', async () => {
    mocks.fetchEfficiency.mockResolvedValue(efficiency)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    // Headroom
    expect(container.textContent).toContain('3,000')
    expect(container.textContent).toContain('42')
    expect(container.querySelector('[data-slot="gauge"]')).not.toBeNull()

    // rtk
    expect(container.textContent).toContain('62.5%')
    expect(container.textContent).toContain('15,000')
    expect(container.textContent).toContain('320')
    expect(container.querySelector('[data-slot="sparkline"]')).not.toBeNull()

    // top_commands is always null on the wire — never rendered/fabricated
    expect(container.textContent).not.toMatch(/top command/i)
  })

  it('shows an honest "not available" state when headroom has recorded nothing yet', async () => {
    mocks.fetchEfficiency.mockResolvedValue({
      headroom: { total_compressions: 0, chars_saved: 0, avg_ratio: 0, p95_ratio: 0, est_tokens_saved: 0 },
      proxy: null,
      rtk: null,
    } satisfies EfficiencySummary)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/headroom.*not reporting|not available/i)
  })

  it('shows an honest "rtk not available" state when rtk is null', async () => {
    mocks.fetchEfficiency.mockResolvedValue({
      headroom: efficiency.headroom,
      proxy: null,
      rtk: null,
    } satisfies EfficiencySummary)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/rtk not available/i)
    // Headroom section still renders normally alongside the rtk fallback.
    expect(container.textContent).toContain('3,000')
  })

  it('never fabricates numbers — a null rtk never shows a $ or % figure for it', async () => {
    mocks.fetchEfficiency.mockResolvedValue({
      headroom: efficiency.headroom,
      proxy: null,
      rtk: null,
    } satisfies EfficiencySummary)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="efficiency-rtk-section"]')).toBeNull()
    const rtkEmpty = container.querySelector('[data-testid="efficiency-rtk-empty"]')
    expect(rtkEmpty).not.toBeNull()
    expect(rtkEmpty?.textContent).not.toMatch(/\d+%/)
    expect(rtkEmpty?.textContent).not.toMatch(/\$\d/)
  })

  it('renders an ApiForbiddenError as an error card instead of crashing', async () => {
    mocks.fetchEfficiency.mockRejectedValue(new ApiForbiddenError())

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')).not.toBeNull()
  })

  it('renders French labels when the language is fr', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    mocks.fetchEfficiency.mockResolvedValue(efficiency)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <EfficiencyView />
        </LanguageProvider>,
      )
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Gain rtk')
  })
})

describe('degenerate data must not be drawn as a trend', () => {
  async function renderWithSeries(saved_series: { date: string; saved_tokens: number }[]) {
    mocks.fetchEfficiency.mockResolvedValue({
      ...efficiency,
      proxy: null,
      rtk: { ...efficiency.rtk, saved_series },
    })
    await act(async () => {
      mount()
      await Promise.resolve()
    })
  }

  it('does not draw a trend from a single point', async () => {
    // One sample drew a flat stub that read as a real, flat trend --
    // worse than saying nothing.
    await renderWithSeries([{ date: '2026-08-01', saved_tokens: 49 }])
    expect(container.textContent).toContain('No daily series recorded yet.')
  })

  it('draws a trend once there are two points', async () => {
    await renderWithSeries([
      { date: '2026-08-01', saved_tokens: 40 },
      { date: '2026-08-02', saved_tokens: 50 },
    ])
    expect(container.textContent).not.toContain('No daily series recorded yet.')
  })
})

describe('zero compressions is not one fact but two', () => {
  async function renderHeadroom(headroom: Record<string, unknown>) {
    mocks.fetchEfficiency.mockResolvedValue({
      ...efficiency,
      headroom: { ...efficiency.headroom, total_compressions: 0, ...headroom },
    })
    await act(async () => {
      mount()
      await Promise.resolve()
    })
  }

  it('says it ran and declined when skips are recorded', async () => {
    await renderHeadroom({ total_skipped: 7, skip_reasons: { non_shrinking: 7 } })
    expect(container.textContent).toContain('ran and declined')
    expect(container.textContent).toContain('non_shrinking')
  })

  it('says it never ran when there is neither', async () => {
    await renderHeadroom({ total_skipped: 0, skip_reasons: {} })
    expect(container.textContent).toContain('has not run yet')
  })

  it('degrades to never-ran when the API omits the fields entirely', async () => {
    // An older API has no skip telemetry; that must not be read as activity.
    await renderHeadroom({})
    expect(container.textContent).toContain('has not run yet')
  })
})

describe('EfficiencyView — prompt cache', () => {
  it('names the step that creates cache it never reads back', async () => {
    // The global hit rate is a shop window. On the reference deployment it
    // read a healthy 85% while one step, `ceo intake`, created ~43k tokens
    // of cache per run and read back ~16k — ten times, at 1.25x input for a
    // creation against 0.1x for a read. An aggregate cannot show that: a
    // busy healthy step drowns a quiet pathological one, and the number
    // that looks fine is exactly why nobody looks further.
    mocks.fetchEfficiency.mockResolvedValue({
      ...efficiency,
      cache: {
        steps: 132,
        hit_rate: 0.85,
        cache_read: 36770730,
        cache_creation: 5342937,
        unamortised: [
          { step: 'ceo intake', runs: 12, cache_read: 461032, cache_creation: 536379, amortisation: 0.35 },
        ],
      },
    })
    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="efficiency-cache-section"]')).not.toBeNull()
    expect(container.textContent).toContain('ceo intake')
  })

  it('says nothing was measured rather than showing a fabricated 0%', async () => {
    // A rate of zero reads as "the cache never works". No model step having
    // run yet is a different and much quieter statement.
    mocks.fetchEfficiency.mockResolvedValue({
      ...efficiency,
      cache: { steps: 0, hit_rate: null, cache_read: 0, cache_creation: 0, unamortised: [] },
    })
    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="efficiency-cache-empty"]')).not.toBeNull()
  })

  it('survives a server too old to send the field', async () => {
    // `undefined` as well as `null`: an out-of-date server simply omits it,
    // and a panel that crashed on that would take the whole view with it.
    mocks.fetchEfficiency.mockResolvedValue({ ...efficiency, cache: undefined })
    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="efficiency-cache-empty"]')).not.toBeNull()
  })

  it('shows no offender list when every step amortises', async () => {
    mocks.fetchEfficiency.mockResolvedValue({
      ...efficiency,
      cache: { steps: 40, hit_rate: 0.93, cache_read: 900, cache_creation: 100, unamortised: [] },
    })
    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="efficiency-cache-section"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="efficiency-cache-offenders"]')).toBeNull()
  })
})
