import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiForbiddenError } from '@/lib/api'
import { LanguageProvider } from '@/lib/i18n'

const { fetchRuns } = vi.hoisted(() => ({ fetchRuns: vi.fn() }))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchRuns }
})

import { MissionsRail } from './MissionsRail'

let container: HTMLDivElement
let root: Root

async function mount() {
  await act(async () => {
    root.render(
      <LanguageProvider>
        <MissionsRail />
      </LanguageProvider>,
    )
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('MissionsRail', () => {
  beforeEach(() => {
    fetchRuns.mockReset()
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
  })

  it('renders a compact run row with a status glyph', async () => {
    fetchRuns.mockResolvedValue([
      {
        id: 9,
        project: 'hive',
        task: 'lint',
        status: 'pending',
        started_at: '2026-07-18T10:00:00Z',
      },
    ])
    await mount()
    expect(container.querySelector('[data-testid="espaces-rail-row-9"]')?.textContent).toContain('hive')
    expect(container.querySelector('[data-testid="status-glyph"]')?.getAttribute('data-zone')).toBe(
      'queued',
    )
    expect(fetchRuns).toHaveBeenCalledWith(12)
  })

  it('explains a forbidden runs list without crashing', async () => {
    fetchRuns.mockRejectedValue(new ApiForbiddenError())
    await mount()
    expect(container.querySelector('[data-testid="espaces-rail"]')?.textContent).toMatch(/run-rank/i)
  })
})
