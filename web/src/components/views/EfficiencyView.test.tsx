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
