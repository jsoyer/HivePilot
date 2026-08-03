import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type { AutopilotState } from '@/lib/pollen-api'
import type { Role } from '@/lib/role-context'

const { fetchAutopilot, pauseAutopilot, resumeAutopilot, useRoleMock } = vi.hoisted(() => ({
  fetchAutopilot: vi.fn(),
  pauseAutopilot: vi.fn(),
  resumeAutopilot: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchAutopilot, pauseAutopilot, resumeAutopilot }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { AutopilotView } from './AutopilotView'

function mockRole(role: Role, rank: number) {
  useRoleMock.mockReturnValue({
    role,
    rank,
    can: (required: Role) => {
      const order: Role[] = ['read', 'run', 'approve', 'admin']
      return order.indexOf(role) >= order.indexOf(required)
    },
  })
}

const BASE_STATE: AutopilotState = {
  tenant: 'default',
  paused: false,
  queue: [],
  queue_depth: 0,
  budget_daily_usd: null,
  budget_spent_today: null,
  budget_remaining: null,
  recent_dispatches: [],
  auto_dispatch_allowlist: [],
}

let container: HTMLDivElement
let root: Root

function mount() {
  act(() => {
    root.render(<AutopilotView />)
  })
}

beforeEach(() => {
  fetchAutopilot.mockReset()
  pauseAutopilot.mockReset()
  resumeAutopilot.mockReset()
  useRoleMock.mockReset()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
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
  vi.useRealTimers()
})

describe('AutopilotView', () => {
  it('shows a loading indicator before the state resolves', () => {
    fetchAutopilot.mockReturnValue(new Promise(() => {}))
    mockRole('run', 1)
    mount()
    expect(container.querySelector('[role="status"]')).not.toBeNull()
  })

  it('renders Active status when not paused', async () => {
    fetchAutopilot.mockResolvedValue({ ...BASE_STATE, paused: false })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Active')
    expect(container.textContent).not.toContain('Paused')
  })

  it('renders Paused status when paused', async () => {
    fetchAutopilot.mockResolvedValue({ ...BASE_STATE, paused: true })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Paused')
  })

  it('renders the queue with pipeline/project/reason/age', async () => {
    fetchAutopilot.mockResolvedValue({
      ...BASE_STATE,
      queue: [
        {
          id: 1,
          pipeline: 'nightly-build',
          project: 'acme-web',
          reason: 'scheduled sweep',
          state: 'queued',
          enqueued_at: new Date(Date.now() - 60_000).toISOString(),
        },
      ],
      queue_depth: 1,
    })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('nightly-build')
    expect(container.textContent).toContain('acme-web')
    expect(container.textContent).toContain('scheduled sweep')
  })

  it('renders recent dispatches with pipeline/project/outcome/at', async () => {
    fetchAutopilot.mockResolvedValue({
      ...BASE_STATE,
      recent_dispatches: [
        { pipeline: 'nightly-build', project: 'acme-web', outcome: 'done', at: new Date().toISOString() },
        { pipeline: 'drift-scan', project: 'acme-api', outcome: 'blocked', at: new Date().toISOString() },
      ],
    })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('nightly-build')
    expect(container.textContent).toContain('done')
    expect(container.textContent).toContain('drift-scan')
    expect(container.textContent).toContain('blocked')
  })

  it('renders the allowlist as chips', async () => {
    fetchAutopilot.mockResolvedValue({
      ...BASE_STATE,
      auto_dispatch_allowlist: ['nightly-build', 'drift-scan'],
    })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('nightly-build')
    expect(container.textContent).toContain('drift-scan')
  })

  it('CRITICAL: honest empty queue shows "queue empty", not a blank list', async () => {
    fetchAutopilot.mockResolvedValue({ ...BASE_STATE, queue: [] })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="autopilot-queue-empty"]')).not.toBeNull()
  })

  it('CRITICAL: honest empty dispatches shows an empty message', async () => {
    fetchAutopilot.mockResolvedValue({ ...BASE_STATE, recent_dispatches: [] })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="autopilot-dispatches-empty"]')).not.toBeNull()
  })

  it('CRITICAL: an empty allowlist states the CONSEQUENCE, not just that it is empty', async () => {
    fetchAutopilot.mockResolvedValue({ ...BASE_STATE, auto_dispatch_allowlist: [] })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const empty = container.querySelector('[data-testid="autopilot-allowlist-empty"]')
    expect(empty).not.toBeNull()
    // Says what it means...
    expect(empty?.textContent).toMatch(/never run one/i)
    // ...and what would change it.
    expect(empty?.textContent).toMatch(/auto_dispatch in policies\.yaml/i)
  })

  it('CRITICAL: budget_daily_usd null explains what to configure, and renders no fake gauge', async () => {
    fetchAutopilot.mockResolvedValue({ ...BASE_STATE, budget_daily_usd: null })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="autopilot-no-budget"]')).not.toBeNull()
    expect(container.querySelector('[data-slot="gauge"]')).toBeNull()
  })

  it('CRITICAL: null spent/remaining render "unknown", never a fabricated 0 or full budget', async () => {
    fetchAutopilot.mockResolvedValue({
      ...BASE_STATE,
      budget_daily_usd: 50,
      budget_spent_today: null,
      budget_remaining: null,
    })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).not.toMatch(/\$0\.00/)
    expect(container.textContent).toMatch(/unknown/i)
    // A gauge fraction can't be computed honestly without a known spend —
    // no gauge should render when spend is unknown.
    expect(container.querySelector('[data-slot="gauge"]')).toBeNull()
  })

  it('renders a real burn gauge when both budget and spend are known', async () => {
    fetchAutopilot.mockResolvedValue({
      ...BASE_STATE,
      budget_daily_usd: 50,
      budget_spent_today: 10,
      budget_remaining: 40,
    })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-slot="gauge"]')).not.toBeNull()
    expect(container.textContent).toContain('$50.00')
    expect(container.textContent).toContain('$10.00')
    expect(container.textContent).toContain('$40.00')
  })

  it('CRITICAL: Pause button (with confirm) calls pauseAutopilot and refetches', async () => {
    fetchAutopilot.mockResolvedValueOnce({ ...BASE_STATE, paused: false }).mockResolvedValueOnce({
      ...BASE_STATE,
      paused: true,
    })
    mockRole('run', 1)
    pauseAutopilot.mockResolvedValue({ tenant: 'default', paused: true })

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const pauseButton = container.querySelector('button[aria-label="Pause"]') as HTMLButtonElement
    expect(pauseButton).not.toBeNull()

    await act(async () => {
      pauseButton.click()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(window.confirm).toHaveBeenCalled()
    expect(pauseAutopilot).toHaveBeenCalled()
    expect(fetchAutopilot).toHaveBeenCalledTimes(2)
  })

  it('CRITICAL: Resume button (with confirm) calls resumeAutopilot and refetches', async () => {
    fetchAutopilot.mockResolvedValueOnce({ ...BASE_STATE, paused: true }).mockResolvedValueOnce({
      ...BASE_STATE,
      paused: false,
    })
    mockRole('run', 1)
    resumeAutopilot.mockResolvedValue({ tenant: 'default', paused: false })

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const resumeButton = container.querySelector('button[aria-label="Resume"]') as HTMLButtonElement
    expect(resumeButton).not.toBeNull()

    await act(async () => {
      resumeButton.click()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(window.confirm).toHaveBeenCalled()
    expect(resumeAutopilot).toHaveBeenCalled()
    expect(fetchAutopilot).toHaveBeenCalledTimes(2)
  })

  it('does not call pause/resume if the confirm dialog is dismissed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    fetchAutopilot.mockResolvedValue({ ...BASE_STATE, paused: false })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const pauseButton = container.querySelector('button[aria-label="Pause"]') as HTMLButtonElement
    await act(async () => {
      pauseButton.click()
      await Promise.resolve()
    })

    expect(pauseAutopilot).not.toHaveBeenCalled()
  })

  it('CRITICAL: the control button is disabled for a caller ranked below run', async () => {
    fetchAutopilot.mockResolvedValue({ ...BASE_STATE, paused: false })
    mockRole('read', 0)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const pauseButton = container.querySelector('button[aria-label="Pause"]') as HTMLButtonElement
    expect(pauseButton).not.toBeNull()
    expect(pauseButton.disabled).toBe(true)
  })

  it('CRITICAL: a 403 from the control POST is handled gracefully, never a crash', async () => {
    fetchAutopilot.mockResolvedValue({ ...BASE_STATE, paused: false })
    mockRole('run', 1)
    const { ApiForbiddenError } = await import('@/lib/api')
    pauseAutopilot.mockRejectedValue(new ApiForbiddenError())

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const pauseButton = container.querySelector('button[aria-label="Pause"]') as HTMLButtonElement
    await act(async () => {
      pauseButton.click()
      await Promise.resolve()
      await Promise.resolve()
    })

    const alert = container.querySelector('[role="alert"]')
    expect(alert?.textContent).toMatch(/insufficient role|higher-privilege/i)
  })

  it('CRITICAL: a 403 on the GET shows ApiForbiddenError handled gracefully, never a crash', async () => {
    const { ApiForbiddenError } = await import('@/lib/api')
    fetchAutopilot.mockRejectedValue(new ApiForbiddenError())
    mockRole('read', 0)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')).toBeNull()
    expect(container.querySelector('[data-testid="autopilot-forbidden"]')).not.toBeNull()
  })

  it('CRITICAL: pipeline/project/reason with markup render as literal text, never injected markup (XSS)', async () => {
    const malicious = '<img src=x onerror=alert(1)>'
    fetchAutopilot.mockResolvedValue({
      ...BASE_STATE,
      queue: [
        {
          id: 1,
          pipeline: malicious,
          project: malicious,
          reason: malicious,
          state: 'queued',
          enqueued_at: new Date().toISOString(),
        },
      ],
      queue_depth: 1,
    })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain(malicious)
    expect(container.querySelector('img')).toBeNull()
  })

  it('CRITICAL: polls fetchAutopilot on an interval so state transitions show up', async () => {
    vi.useFakeTimers()
    fetchAutopilot.mockResolvedValue({ ...BASE_STATE })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(fetchAutopilot).toHaveBeenCalledTimes(1)

    await act(async () => {
      vi.advanceTimersByTime(10_000)
      await Promise.resolve()
    })

    expect(fetchAutopilot.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('renders French labels when the language is fr (P1a)', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    fetchAutopilot.mockResolvedValue({ ...BASE_STATE, paused: true })
    mockRole('run', 1)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <AutopilotView />
        </LanguageProvider>,
      )
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Autopilote')
    expect(container.textContent).toContain('En pause')
  })

  it('CRITICAL: every empty section says what would fill it, never just "nothing"', async () => {
    fetchAutopilot.mockResolvedValue({
      ...BASE_STATE,
      queue: [],
      recent_dispatches: [],
      auto_dispatch_allowlist: [],
    })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const queue = container.querySelector('[data-testid="autopilot-queue-empty"]')
    const dispatches = container.querySelector('[data-testid="autopilot-dispatches-empty"]')
    expect(queue?.textContent).toMatch(/scheduled pipeline or a drift scan/i)
    expect(dispatches?.textContent).toMatch(/each time autopilot drains an objective/i)
    // The old copy is gone.
    expect(container.textContent).not.toMatch(/queue empty\./i)
    expect(container.textContent).not.toMatch(/no dispatches yet\./i)
  })

  it('CRITICAL: renders as one card, not five near-empty stacked cards', async () => {
    fetchAutopilot.mockResolvedValue({
      ...BASE_STATE,
      queue: [],
      recent_dispatches: [],
      auto_dispatch_allowlist: [],
    })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelectorAll('[data-slot="card"]')).toHaveLength(1)
  })

  it('distinguishes an unconfigured budget (em-dash) from an unmeasurable spend ("unknown")', async () => {
    fetchAutopilot.mockResolvedValue({
      ...BASE_STATE,
      budget_daily_usd: null,
      budget_spent_today: null,
      budget_remaining: null,
    })
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('\u2014')
    expect(container.textContent).toMatch(/unknown/i)
    expect(container.textContent).not.toMatch(/\$0\.00/)
  })
})

describe('status distinguishes enabled from able to act', () => {
  async function render(state: Record<string, unknown>) {
    fetchAutopilot.mockResolvedValue({ ...BASE_STATE, paused: false, ...state })
    mockRole('run', 1)
    await act(async () => {
      mount()
      await Promise.resolve()
    })
  }

  it('reads blocked, not active, when the budget is spent', async () => {
    // "Active" beside a 100%-burn gauge and `Remaining $0.00` claimed the
    // autopilot was working when the budget gate had already closed.
    await render({ budget_daily_usd: 2, budget_spent_today: 14.4, budget_remaining: 0 })
    expect(container.textContent).toContain('Blocked')
    expect(container.textContent).not.toContain('Active')
  })

  it('still reads active when budget remains', async () => {
    await render({ budget_daily_usd: 2, budget_spent_today: 0.5, budget_remaining: 1.5 })
    expect(container.textContent).toContain('Active')
    expect(container.textContent).not.toContain('Blocked')
  })

  it('does not claim blocked when spend is unknown', async () => {
    // An unmeasurable spend is neither free nor exhausted.
    await render({ budget_daily_usd: 2, budget_spent_today: null, budget_remaining: null })
    expect(container.textContent).not.toContain('Blocked')
  })

  it('says whose spend the ceiling measures', async () => {
    await render({ budget_daily_usd: 2, budget_spent_today: 1, budget_remaining: 1 })
    expect(container.textContent).toContain('all work')
  })
})
