import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type { RunDetail } from '@/lib/mirador-api'

const { fetchRun } = vi.hoisted(() => ({ fetchRun: vi.fn() }))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, fetchRun }
})

import { RunDetailPanel } from './RunDetailPanel'

const SAMPLE_DETAIL: RunDetail = {
  run_id: 42,
  project: 'acme-web',
  task: 'deploy',
  status: 'success',
  detail: 'run completed',
  started_at: '2026-07-18T10:00:00Z',
  finished_at: '2026-07-18T10:05:00Z',
  tenant: 'default',
  steps: [
    {
      step: 'plan',
      status: 'success',
      detail: 'planned ok',
      provider: 'anthropic',
      model: 'claude-sonnet',
      input_tokens: 100,
      output_tokens: 50,
      cost_usd: 0.01,
      timestamp: '2026-07-18T10:01:00Z',
    },
    {
      step: 'apply',
      status: 'success',
      provider: 'anthropic',
      model: 'claude-sonnet',
      input_tokens: 200,
      output_tokens: 80,
      cost_usd: 0.02,
      timestamp: '2026-07-18T10:04:00Z',
    },
  ],
}

let container: HTMLDivElement
let root: Root

function mount(runId: number | null, onClose: () => void = vi.fn()) {
  act(() => {
    root.render(<RunDetailPanel runId={runId} onClose={onClose} />)
  })
}

beforeEach(() => {
  fetchRun.mockReset()
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

describe('RunDetailPanel', () => {
  it('renders nothing when runId is null (panel closed)', () => {
    mount(null)
    expect(container.textContent).toBe('')
  })

  it('shows a loading indicator while the run detail is in flight', () => {
    fetchRun.mockReturnValue(new Promise(() => {}))
    mount(42)
    expect(container.querySelector('[role="status"]')).not.toBeNull()
  })

  it('renders overall status/detail/started/finished plus every step', async () => {
    fetchRun.mockResolvedValue(SAMPLE_DETAIL)

    await act(async () => {
      mount(42)
      await Promise.resolve()
    })

    expect(container.textContent).toContain('42')
    expect(container.textContent).toContain('acme-web')
    expect(container.textContent).toContain('deploy')
    expect(container.textContent).toContain('success')
    expect(container.textContent).toContain('run completed')
    expect(container.textContent).toContain('plan')
    expect(container.textContent).toContain('apply')
    expect(container.textContent).toContain('anthropic')
    expect(container.textContent).toContain('claude-sonnet')
  })

  it('shows an honest "no steps" message when steps is empty — never fabricates a timeline', async () => {
    fetchRun.mockResolvedValue({ ...SAMPLE_DETAIL, steps: [] })

    await act(async () => {
      mount(42)
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/no step detail recorded/i)
  })

  it('CRITICAL: renders untrusted detail/step text as plain text, never as injected markup (XSS)', async () => {
    const malicious = '<img src=x onerror=alert(1)>'
    fetchRun.mockResolvedValue({
      ...SAMPLE_DETAIL,
      detail: malicious,
      steps: [{ ...SAMPLE_DETAIL.steps[0], detail: malicious }],
    })

    await act(async () => {
      mount(42)
      await Promise.resolve()
    })

    // The raw string appears as escaped TEXT content...
    expect(container.textContent).toContain(malicious)
    // ...but never as an actual injected <img> element.
    expect(container.querySelector('img')).toBeNull()
  })

  it('CRITICAL: a 403 shows a graceful message, not a crash', async () => {
    const { ApiForbiddenError } = await import('@/lib/api')
    fetchRun.mockRejectedValue(new ApiForbiddenError())

    await act(async () => {
      mount(42)
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')).toBeNull()
    expect(container.textContent).toMatch(/run/i)
  })

  it('shows a generic error alert on a non-403 failure', async () => {
    fetchRun.mockRejectedValue(new Error('network down'))

    await act(async () => {
      mount(42)
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')).not.toBeNull()
  })

  it('calls onClose when the close button is clicked', async () => {
    fetchRun.mockResolvedValue(SAMPLE_DETAIL)
    const onClose = vi.fn()

    await act(async () => {
      mount(42, onClose)
      await Promise.resolve()
    })

    const closeButton = container.querySelector('[aria-label="Close run detail"]') as HTMLButtonElement
    act(() => {
      closeButton.click()
    })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when the backdrop is clicked', async () => {
    fetchRun.mockResolvedValue(SAMPLE_DETAIL)
    const onClose = vi.fn()

    await act(async () => {
      mount(42, onClose)
      await Promise.resolve()
    })

    const backdrop = container.querySelector('[data-testid="run-detail-backdrop"]') as HTMLElement
    act(() => {
      backdrop.click()
    })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('renders French title/labels when the language is fr', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    fetchRun.mockResolvedValue(SAMPLE_DETAIL)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <RunDetailPanel runId={42} onClose={vi.fn()} />
        </LanguageProvider>,
      )
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Étapes')
  })
})
