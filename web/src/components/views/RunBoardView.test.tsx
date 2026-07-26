import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type { RunSummary } from '@/lib/mirador-api'
import type { Role } from '@/lib/role-context'

const { fetchRuns, createRun, cancelRun, fetchRun, useRoleMock } = vi.hoisted(() => ({
  fetchRuns: vi.fn(),
  createRun: vi.fn(),
  cancelRun: vi.fn(),
  fetchRun: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, fetchRuns, createRun, cancelRun, fetchRun }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { RunBoardView } from './RunBoardView'

function setNativeValue(input: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
  nativeSetter.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

function run(overrides: Partial<RunSummary>): RunSummary {
  return {
    id: 1,
    project: 'acme-web',
    task: 'deploy',
    status: 'running',
    started_at: '2026-07-18T10:00:00Z',
    finished_at: null,
    ...overrides,
  }
}

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

let container: HTMLDivElement
let root: Root

function mount() {
  act(() => {
    root.render(<RunBoardView />)
  })
}

beforeEach(() => {
  fetchRuns.mockReset()
  createRun.mockReset()
  cancelRun.mockReset()
  fetchRun.mockReset()
  fetchRun.mockResolvedValue({
    run_id: 7,
    project: 'acme-web',
    task: 'deploy',
    status: 'running',
    steps: [],
  })
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

describe('RunBoardView', () => {
  it('shows a loading indicator before the list resolves', () => {
    fetchRuns.mockReturnValue(new Promise(() => {}))
    mockRole('run', 1)
    mount()
    expect(container.querySelector('[role="status"]')).not.toBeNull()
  })

  it('shows a single honest empty state when there are no runs at all', async () => {
    fetchRuns.mockResolvedValue([])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="run-board-empty"]')).not.toBeNull()
    expect(container.textContent).toMatch(/no runs yet/i)
  })

  it('CRITICAL: maps every real run status to the correct column, faithfully (not invented)', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 1, status: 'new' }), // queued
      run({ id: 2, status: 'running' }), // running
      run({ id: 3, status: 'approval' }), // waiting approval
      run({ id: 4, status: 'failed' }), // failed
      run({ id: 5, status: 'success' }), // done
      run({ id: 6, status: 'paused' }), // other (not one of the 5 canonical buckets)
    ])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const queued = container.querySelector('[data-testid="run-board-column-queued"]')
    const running = container.querySelector('[data-testid="run-board-column-running"]')
    const waiting = container.querySelector('[data-testid="run-board-column-waitingApproval"]')
    const failed = container.querySelector('[data-testid="run-board-column-failed"]')
    const done = container.querySelector('[data-testid="run-board-column-done"]')
    const other = container.querySelector('[data-testid="run-board-column-other"]')

    expect(queued?.querySelector('[data-testid="run-board-card-1"]')).not.toBeNull()
    expect(running?.querySelector('[data-testid="run-board-card-2"]')).not.toBeNull()
    expect(waiting?.querySelector('[data-testid="run-board-card-3"]')).not.toBeNull()
    expect(failed?.querySelector('[data-testid="run-board-card-4"]')).not.toBeNull()
    expect(done?.querySelector('[data-testid="run-board-card-5"]')).not.toBeNull()
    expect(other?.querySelector('[data-testid="run-board-card-6"]')).not.toBeNull()
  })

  it('column counts reflect the number of cards in each column', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 1, status: 'running' }),
      run({ id: 2, status: 'running' }),
      run({ id: 3, status: 'failed' }),
    ])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="run-board-count-running"]')?.textContent).toBe('2')
    expect(container.querySelector('[data-testid="run-board-count-failed"]')?.textContent).toBe('1')
    expect(container.querySelector('[data-testid="run-board-count-queued"]')?.textContent).toBe('0')
  })

  it('shows an honest per-column empty message for a column with zero cards (when the board is not fully empty)', async () => {
    fetchRuns.mockResolvedValue([run({ id: 1, status: 'running' })])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const queued = container.querySelector('[data-testid="run-board-column-queued"]')
    expect(queued?.textContent).toMatch(/nothing here/i)
  })

  it('CRITICAL: the Kanban scroll container keeps overflow-x and exposes a discoverable, keyboard-focusable affordance', async () => {
    fetchRuns.mockResolvedValue([run({ id: 1, status: 'running' })])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const scrollContainer = container.querySelector('[data-testid="run-board-kanban-scroll"]')
    expect(scrollContainer).not.toBeNull()
    // Themed, always-visible scrollbar class (see index.css `.kanban-scroll`)
    expect(scrollContainer?.className).toMatch(/\bkanban-scroll\b/)
    // The scroll still actually happens on `sm:` and up.
    expect(scrollContainer?.className).toMatch(/overflow-x-auto/)
    // Never defeated by an ancestor flex context.
    expect(scrollContainer?.className).toMatch(/\bmin-w-0\b/)
    // Keyboard-scrollable: a focusable region, not mouse-drag-only.
    expect(scrollContainer?.getAttribute('tabindex')).toBe('0')
    expect(scrollContainer?.getAttribute('role')).toBe('region')
    expect(scrollContainer?.getAttribute('aria-label')).toMatch(/scroll/i)
  })

  it('never renders RunSummary.detail (untrusted free text) on a card', async () => {
    fetchRuns.mockResolvedValue([run({ id: 1, detail: 'SECRET INTERNAL DETAIL' })])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).not.toContain('SECRET INTERNAL DETAIL')
  })

  it('applies a severity stripe to failed/waiting-approval cards, not to done/running/queued cards', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 1, status: 'failed' }),
      run({ id: 2, status: 'approval' }),
      run({ id: 3, status: 'success' }),
      run({ id: 4, status: 'running' }),
    ])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const failedCard = container.querySelector('[data-testid="run-board-card-1"]')
    const waitingCard = container.querySelector('[data-testid="run-board-card-2"]')
    const doneCard = container.querySelector('[data-testid="run-board-card-3"]')
    const runningCard = container.querySelector('[data-testid="run-board-card-4"]')

    expect(failedCard?.className).toMatch(/border-l-\[var\(--color-crit\)\]/)
    expect(waitingCard?.className).toMatch(/border-l-\[var\(--color-warn\)\]/)
    expect(doneCard?.className).not.toMatch(/border-l-\[var\(--color-(crit|warn)\)\]/)
    expect(runningCard?.className).not.toMatch(/border-l-\[var\(--color-(crit|warn)\)\]/)
  })

  it('CRITICAL: clicking a card opens the run detail panel for that run', async () => {
    fetchRuns.mockResolvedValue([run({ id: 7 })])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const card = container.querySelector('[data-testid="run-board-card-7"]') as HTMLElement
    await act(async () => {
      card.click()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="dialog"]')).not.toBeNull()
    expect(fetchRun).toHaveBeenCalledWith(7)
  })

  it('CRITICAL: hides the New Run form when the caller ranks below run', async () => {
    fetchRuns.mockResolvedValue([])
    mockRole('read', 0)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('#new-run-task')).toBeNull()
  })

  it('CRITICAL: submits a new run via createRun and refreshes the board', async () => {
    fetchRuns.mockResolvedValueOnce([]).mockResolvedValueOnce([run({ id: 9 })])
    mockRole('run', 1)
    createRun.mockResolvedValue({ run_id: 9, status: 'running' })

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const taskInput = container.querySelector('#new-run-task') as HTMLInputElement
    const projectInput = container.querySelector('#new-run-project') as HTMLInputElement
    act(() => {
      setNativeValue(taskInput, 'deploy')
      setNativeValue(projectInput, 'acme-web')
    })

    const form = container.querySelector('form') as HTMLFormElement
    await act(async () => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(createRun).toHaveBeenCalledWith({
      task: 'deploy',
      project: 'acme-web',
      extra_prompt: undefined,
      auto_git: false,
    })
    expect(fetchRuns).toHaveBeenCalledTimes(2)
  })

  it('CRITICAL: Stop button on a running card calls cancelRun (after confirm) and never opens the detail panel', async () => {
    fetchRuns.mockResolvedValueOnce([run({ id: 7, status: 'running' })]).mockResolvedValueOnce([
      run({ id: 7, status: 'cancelled' }),
    ])
    mockRole('run', 1)
    cancelRun.mockResolvedValue({ run_id: 7, status: 'cancelling' })

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const stopButton = container.querySelector('[aria-label="Stop run 7"]') as HTMLButtonElement
    await act(async () => {
      stopButton.click()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(cancelRun).toHaveBeenCalledWith(7)
    expect(container.querySelector('[role="dialog"]')).toBeNull()
  })

  it('CRITICAL: polls fetchRuns on an interval (<=3s) so status transitions show up', async () => {
    vi.useFakeTimers()
    fetchRuns.mockResolvedValue([run({ id: 1 })])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(fetchRuns).toHaveBeenCalledTimes(1)

    await act(async () => {
      vi.advanceTimersByTime(3000)
      await Promise.resolve()
    })

    expect(fetchRuns.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('CRITICAL: a plain read token 403 on GET /v1/runs shows a graceful message, not a crash', async () => {
    const { ApiForbiddenError } = await import('@/lib/api')
    fetchRuns.mockRejectedValue(new ApiForbiddenError())
    mockRole('read', 0)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')).toBeNull()
    expect(container.textContent).toMatch(/run-rank/i)
  })

  it('renders a cancelled run in the "Other" column with distinct destructive styling', async () => {
    fetchRuns.mockResolvedValue([run({ id: 1, status: 'cancelled' })])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const other = container.querySelector('[data-testid="run-board-column-other"]')
    expect(other?.querySelector('[data-testid="run-board-card-1"]')).not.toBeNull()
    const badge = Array.from(container.querySelectorAll('span')).find((el) => el.textContent === 'cancelled')
    expect(badge?.className).toMatch(/destructive/)
  })

  it('renders French column labels when the language is fr', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    fetchRuns.mockResolvedValue([])
    mockRole('run', 1)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <RunBoardView />
        </LanguageProvider>,
      )
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Exécutions')
  })
})
