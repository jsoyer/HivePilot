import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type { RunSummary } from '@/lib/pollen-api'
import type { Role } from '@/lib/role-context'

const { fetchRuns, createRun, cancelRun, fetchRun, fetchProjectNames, fetchTaskNames, useRoleMock } =
  vi.hoisted(() => ({
    fetchRuns: vi.fn(),
    createRun: vi.fn(),
    cancelRun: vi.fn(),
    fetchRun: vi.fn(),
    fetchProjectNames: vi.fn(),
    fetchTaskNames: vi.fn(),
    useRoleMock: vi.fn(),
  }))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchRuns, createRun, cancelRun, fetchRun, fetchProjectNames, fetchTaskNames }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

const { useEventStreamMock, lastStreamHandler } = vi.hoisted(() => {
  const ref: { current: ((event: { entity_type: string }) => void) | null } = { current: null }
  return {
    lastStreamHandler: ref,
    useEventStreamMock: vi.fn(),
  }
})

vi.mock('@/lib/use-event-stream', () => ({ useEventStream: useEventStreamMock }))

import { boardPlacement, isHistoryRun, RunBoardView } from './RunBoardView'

function setSelectValue(select: HTMLSelectElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value')!.set!
  setter.call(select, value)
  select.dispatchEvent(new Event('change', { bubbles: true }))
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

async function mountResolved() {
  await act(async () => {
    mount()
    await Promise.resolve()
  })
}

function clickTab(name: 'board' | 'history') {
  act(() => {
    ;(container.querySelector(`[data-testid="runs-surface-${name}"]`) as HTMLButtonElement).click()
  })
}

beforeEach(() => {
  window.localStorage.clear()
  fetchRuns.mockReset()
  createRun.mockReset()
  cancelRun.mockReset()
  fetchRun.mockReset()
  fetchProjectNames.mockReset()
  fetchTaskNames.mockReset()
  fetchProjectNames.mockResolvedValue(['acme-web'])
  fetchTaskNames.mockResolvedValue(['deploy'])
  fetchRun.mockResolvedValue({
    run_id: 7,
    project: 'acme-web',
    task: 'deploy',
    status: 'running',
    steps: [],
  })
  useRoleMock.mockReset()
  lastStreamHandler.current = null
  useEventStreamMock.mockReset()
  useEventStreamMock.mockImplementation(
    (onEvent: (event: { entity_type: string }) => void, opts?: { enabled?: boolean }) => {
      if (opts?.enabled !== false) lastStreamHandler.current = onEvent
    },
  )
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

describe('boardPlacement', () => {
  it('sends done and cancelled to History, and keeps live work on the four columns', () => {
    expect(boardPlacement('new')).toBe('queued')
    expect(boardPlacement('running')).toBe('running')
    expect(boardPlacement('approval')).toBe('waitingApproval')
    expect(boardPlacement('failed')).toBe('failed')
    expect(boardPlacement('success')).toBe('history')
    expect(boardPlacement('complete')).toBe('history')
    expect(boardPlacement('cancelled')).toBe('history')
    expect(boardPlacement('paused')).toBe('waitingApproval')
    expect(isHistoryRun('success')).toBe(true)
    expect(isHistoryRun('running')).toBe(false)
  })
})

describe('RunBoardView', () => {
  it('shows a loading indicator before the list resolves', () => {
    fetchRuns.mockReturnValue(new Promise(() => {}))
    mockRole('run', 1)
    mount()
    expect(container.querySelector('[role="status"]')).not.toBeNull()
  })

  it('CRITICAL: content first — no creation form occupies the view before any content', async () => {
    fetchRuns.mockResolvedValue([run({ id: 1 })])
    mockRole('run', 1)
    await mountResolved()

    expect(container.querySelector('form')).toBeNull()
    expect(container.querySelector('#new-run-task')).toBeNull()
    expect(container.querySelector('[data-testid="run-board-kanban-scroll"]')).not.toBeNull()
  })

  it('CRITICAL: the New run button opens the creation drawer, and only for a run-rank token', async () => {
    fetchRuns.mockResolvedValue([run({ id: 1 })])
    mockRole('run', 1)
    await mountResolved()

    const newRun = Array.from(container.querySelectorAll('button')).find((b) =>
      /new run/i.test(b.textContent ?? ''),
    ) as HTMLButtonElement
    expect(newRun).toBeDefined()

    await act(async () => {
      newRun.click()
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(container.querySelector('[role="dialog"]')).not.toBeNull()
    expect(container.querySelector('#new-run-task')).not.toBeNull()
  })

  it('CRITICAL: hides the creation affordance entirely when the caller ranks below run', async () => {
    fetchRuns.mockResolvedValue([run({ id: 1 })])
    mockRole('read', 0)
    await mountResolved()

    expect(
      Array.from(container.querySelectorAll('button')).some((b) => /new run/i.test(b.textContent ?? '')),
    ).toBe(false)
  })

  it('CRITICAL: an empty board explains what would fill it and offers the action', async () => {
    fetchRuns.mockResolvedValue([])
    mockRole('run', 1)
    await mountResolved()

    const empty = container.querySelector('[data-testid="run-board-empty"]')
    expect(empty).not.toBeNull()
    expect(empty?.textContent).toMatch(/no runs yet/i)
    expect(empty?.textContent).toMatch(/history/i)
    expect(empty?.querySelector('[data-slot="empty-state-action"] button')).not.toBeNull()
  })

  it('an empty board for a read-only token offers no action it cannot perform', async () => {
    fetchRuns.mockResolvedValue([])
    mockRole('read', 0)
    await mountResolved()

    const empty = container.querySelector('[data-testid="run-board-empty"]')
    expect(empty).not.toBeNull()
    expect(empty?.querySelector('[data-slot="empty-state-action"]')).toBeNull()
  })

  it('CRITICAL: live statuses land on the four board columns; Done never does', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 1, status: 'new' }),
      run({ id: 2, status: 'running' }),
      run({ id: 3, status: 'approval' }),
      run({ id: 4, status: 'failed' }),
      run({ id: 5, status: 'success' }),
      run({ id: 6, status: 'paused' }),
    ])
    mockRole('run', 1)
    await mountResolved()

    const at = (col: string) => container.querySelector(`[data-testid="run-board-column-${col}"]`)
    expect(at('queued')?.querySelector('[data-testid="run-board-card-1"]')).not.toBeNull()
    expect(at('running')?.querySelector('[data-testid="run-board-card-2"]')).not.toBeNull()
    expect(at('waitingApproval')?.querySelector('[data-testid="run-board-card-3"]')).not.toBeNull()
    expect(at('failed')?.querySelector('[data-testid="run-board-card-4"]')).not.toBeNull()
    expect(at('waitingApproval')?.querySelector('[data-testid="run-board-card-6"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="run-board-column-done"]')).toBeNull()
    expect(container.querySelector('[data-testid="run-board-column-other"]')).toBeNull()
    expect(container.querySelector('[data-testid="run-board-card-5"]')).toBeNull()
  })

  it('CRITICAL: History lists completed runs and never a Done kanban column', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 5, status: 'success', finished_at: '2026-07-18T10:00:08Z' }),
      run({ id: 8, status: 'cancelled', finished_at: '2026-07-18T10:00:04Z' }),
      run({ id: 2, status: 'running' }),
    ])
    mockRole('run', 1)
    await mountResolved()

    clickTab('history')

    expect(container.querySelector('[data-testid="run-board-column-done"]')).toBeNull()
    expect(container.querySelector('[data-testid="run-history-table"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="run-history-row-5"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="run-history-row-8"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="run-history-row-2"]')).toBeNull()
    expect(container.querySelector('[data-testid="run-history-caption"]')?.textContent).toMatch(/2/)
  })

  it('the Done shortcut opens History', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 5, status: 'success', finished_at: '2026-07-18T10:00:08Z' }),
      run({ id: 2, status: 'running' }),
    ])
    mockRole('run', 1)
    await mountResolved()

    const shortcut = container.querySelector('[data-testid="runs-done-shortcut"]') as HTMLButtonElement
    expect(shortcut.textContent).toMatch(/done \(1\)/i)
    act(() => {
      shortcut.click()
    })
    expect(container.querySelector('[data-testid="run-history-row-5"]')).not.toBeNull()
    expect(
      container.querySelector('[data-testid="runs-surface-history"]')?.getAttribute('aria-selected'),
    ).toBe('true')
  })

  it('column counts reflect the number of cards in each column', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 1, status: 'running' }),
      run({ id: 2, status: 'running' }),
      run({ id: 3, status: 'failed' }),
    ])
    mockRole('run', 1)
    await mountResolved()

    expect(container.querySelector('[data-testid="run-board-count-running"]')?.textContent).toBe('2')
    expect(container.querySelector('[data-testid="run-board-count-failed"]')?.textContent).toBe('1')
    expect(container.querySelector('[data-testid="run-board-count-queued"]')?.textContent).toBe('0')
  })

  it('CRITICAL: empty columns stay equal-width rails with an em dash', async () => {
    fetchRuns.mockResolvedValue([run({ id: 1, status: 'running' })])
    mockRole('run', 1)
    await mountResolved()

    const running = container.querySelector('[data-testid="run-board-column-running"]')
    const queued = container.querySelector('[data-testid="run-board-column-queued"]')

    expect(running?.getAttribute('data-empty')).toBe('false')
    expect(queued?.getAttribute('data-empty')).toBe('true')
    expect(running?.className).toMatch(/flex-1/)
    expect(queued?.className).toMatch(/flex-1/)
    expect(queued?.textContent).toContain('—')
    expect(container.textContent).not.toMatch(/nothing here/i)
  })

  it('CRITICAL: the Kanban scroll container owns its overflow and is keyboard-focusable', async () => {
    fetchRuns.mockResolvedValue([run({ id: 1, status: 'running' })])
    mockRole('run', 1)
    await mountResolved()

    const scrollContainer = container.querySelector('[data-testid="run-board-kanban-scroll"]')
    expect(scrollContainer?.className).toMatch(/\bkanban-scroll\b/)
    expect(scrollContainer?.className).toMatch(/overflow-x-auto/)
    expect(scrollContainer?.className).toMatch(/\bmin-w-0\b/)
    expect(scrollContainer?.getAttribute('tabindex')).toBe('0')
    expect(scrollContainer?.getAttribute('role')).toBe('region')
    expect(scrollContainer?.getAttribute('aria-label')).toMatch(/scroll/i)
  })

  it('CRITICAL: never renders RunSummary.detail (untrusted free text) on a card', async () => {
    fetchRuns.mockResolvedValue([run({ id: 1, status: 'failed', detail: 'SECRET INTERNAL DETAIL' })])
    mockRole('run', 1)
    await mountResolved()

    expect(container.textContent).not.toContain('SECRET INTERNAL DETAIL')
  })

  it('a card shows title, project, and age — not a fabricated reason', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 1, task: 'groomer-scan', project: 'noxys', status: 'failed' }),
    ])
    mockRole('run', 1)
    await mountResolved()

    const card = container.querySelector('[data-testid="run-board-card-1"]')
    expect(card?.textContent).toContain('groomer-scan')
    expect(card?.textContent).toContain('noxys')
    expect(card?.textContent).toContain('#1')
    expect(container.querySelector('[data-testid="run-board-reason-1"]')).toBeNull()
  })

  it('CRITICAL: a card carries age with the full stamp on hover', async () => {
    const started = '2026-07-18T10:00:00Z'
    fetchRuns.mockResolvedValue([run({ id: 1, started_at: started, finished_at: '2026-07-18T10:00:08Z' })])
    mockRole('run', 1)
    await mountResolved()

    const card = container.querySelector('[data-testid="run-board-card-1"]')
    expect(card?.querySelector(`[title="${new Date(started).toLocaleString()}"]`)).not.toBeNull()
  })

  it('applies a 2px crit stripe to failed cards only — no glow, no waiting stripe', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 1, status: 'failed' }),
      run({ id: 2, status: 'approval' }),
      run({ id: 3, status: 'running' }),
    ])
    mockRole('run', 1)
    await mountResolved()

    const failed = container.querySelector('[data-testid="run-board-card-1"]')
    expect(failed?.className).toMatch(/border-l-2/)
    expect(failed?.className).toMatch(/border-l-\[var\(--color-crit\)\]/)
    expect(failed?.className).toMatch(/shadow-none/)
    expect(container.querySelector('[data-testid="run-board-card-2"]')?.className).not.toMatch(
      /border-l-\[var\(--color-(crit|warn)\)\]/,
    )
    expect(container.querySelector('[data-testid="run-board-card-3"]')?.className).not.toMatch(
      /border-l-\[var\(--color-(crit|warn)\)\]/,
    )
  })

  it('CRITICAL: filters the board by project, offering only values present on the board', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 1, project: 'acme-web' }),
      run({ id: 2, project: 'api' }),
      run({ id: 3, project: 'api' }),
    ])
    mockRole('run', 1)
    await mountResolved()

    const projectFilter = container.querySelector('#run-filter-project') as HTMLSelectElement
    expect(Array.from(projectFilter.querySelectorAll('option')).map((o) => o.value)).toEqual([
      '__all__',
      'acme-web',
      'api',
    ])

    act(() => {
      setSelectValue(projectFilter, 'api')
    })

    expect(container.querySelector('[data-testid="run-board-card-1"]')).toBeNull()
    expect(container.querySelector('[data-testid="run-board-card-2"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="run-board-result-count"]')?.textContent).toContain('2')
  })

  it('History can filter completed runs by status', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 5, status: 'success', finished_at: '2026-07-18T10:00:08Z' }),
      run({ id: 8, status: 'cancelled', finished_at: '2026-07-18T10:00:04Z' }),
    ])
    mockRole('run', 1)
    await mountResolved()
    clickTab('history')

    act(() => {
      setSelectValue(container.querySelector('#run-filter-status') as HTMLSelectElement, 'cancelled')
    })

    expect(container.querySelector('[data-testid="run-history-row-8"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="run-history-row-5"]')).toBeNull()
  })

  it('a filter combination with no match explains itself and offers a way back', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 1, project: 'acme-web', task: 'deploy' }),
      run({ id: 2, project: 'api', task: 'audit' }),
    ])
    mockRole('run', 1)
    await mountResolved()

    act(() => {
      setSelectValue(container.querySelector('#run-filter-project') as HTMLSelectElement, 'acme-web')
    })
    act(() => {
      setSelectValue(container.querySelector('#run-filter-task') as HTMLSelectElement, 'audit')
    })

    expect(container.textContent).toMatch(/no run matches these filters/i)
    const clear = Array.from(container.querySelectorAll('button')).find((b) =>
      /clear filters/i.test(b.textContent ?? ''),
    ) as HTMLButtonElement
    act(() => {
      clear.click()
    })
    expect(container.querySelector('[data-testid="run-board-card-1"]')).not.toBeNull()
  })

  it('CRITICAL: the density toggle persists across mounts', async () => {
    fetchRuns.mockResolvedValue([run({ id: 1, finished_at: '2026-07-18T10:00:08Z' })])
    mockRole('run', 1)
    await mountResolved()

    expect(container.querySelector('[data-testid="run-board-card-1"]')?.className).toMatch(/\bp-3\b/)

    act(() => {
      ;(container.querySelector('[data-testid="run-board-density-compact"]') as HTMLButtonElement).click()
    })
    expect(container.querySelector('[data-testid="run-board-card-1"]')?.className).toMatch(/\bp-2\b/)

    act(() => {
      root.unmount()
    })
    root = createRoot(container)
    await mountResolved()
    expect(container.querySelector('[data-testid="run-board-card-1"]')?.className).toMatch(/\bp-2\b/)
  })

  it('CRITICAL: clicking a card opens the run detail panel for that run', async () => {
    fetchRuns.mockResolvedValue([run({ id: 7 })])
    mockRole('run', 1)
    await mountResolved()

    const card = container.querySelector('[data-testid="run-board-card-7"]') as HTMLElement
    await act(async () => {
      card.click()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="dialog"]')).not.toBeNull()
    expect(fetchRun).toHaveBeenCalledWith(7)
  })

  it('clicking a History row opens the run detail panel', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 7, status: 'success', finished_at: '2026-07-18T10:00:08Z' }),
    ])
    mockRole('run', 1)
    await mountResolved()
    clickTab('history')

    const row = container.querySelector('[data-testid="run-history-row-7"]') as HTMLElement
    await act(async () => {
      row.click()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="dialog"]')).not.toBeNull()
    expect(fetchRun).toHaveBeenCalledWith(7)
  })

  it('CRITICAL: submits a new run from the drawer and refreshes the board', async () => {
    fetchRuns.mockResolvedValueOnce([run({ id: 1 })]).mockResolvedValue([run({ id: 9 })])
    mockRole('run', 1)
    createRun.mockResolvedValue({ run_id: 9, status: 'running' })
    await mountResolved()

    const newRun = Array.from(container.querySelectorAll('button')).find((b) =>
      /new run/i.test(b.textContent ?? ''),
    ) as HTMLButtonElement
    await act(async () => {
      newRun.click()
      await Promise.resolve()
      await Promise.resolve()
    })

    act(() => {
      setSelectValue(container.querySelector('#new-run-task') as HTMLSelectElement, 'deploy')
    })
    act(() => {
      setSelectValue(container.querySelector('#new-run-project') as HTMLSelectElement, 'acme-web')
    })

    await act(async () => {
      ;(container.querySelector('form') as HTMLFormElement).dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }),
      )
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(createRun).toHaveBeenCalledWith({
      task: 'deploy',
      project: 'acme-web',
      extra_prompt: undefined,
      auto_git: false,
    })
    expect(fetchRuns.mock.calls.length).toBeGreaterThanOrEqual(2)
    expect(container.querySelector('#new-run-task')).toBeNull()
  })

  it('CRITICAL: Stop button on a running card calls cancelRun (after confirm) and never opens the detail panel', async () => {
    fetchRuns
      .mockResolvedValueOnce([run({ id: 7, status: 'running' })])
      .mockResolvedValue([run({ id: 7, status: 'cancelled' })])
    mockRole('run', 1)
    cancelRun.mockResolvedValue({ run_id: 7, status: 'cancelling' })
    await mountResolved()

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
    await mountResolved()

    expect(container.querySelector('[role="alert"]')).toBeNull()
    expect(container.textContent).toMatch(/run-rank/i)
  })

  it('renders a cancelled run in History, not on the board', async () => {
    fetchRuns.mockResolvedValue([run({ id: 1, status: 'cancelled' })])
    mockRole('run', 1)
    await mountResolved()

    expect(container.querySelector('[data-testid="run-board-card-1"]')).toBeNull()
    clickTab('history')
    expect(container.querySelector('[data-testid="run-history-row-1"]')).not.toBeNull()
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
    expect(container.textContent).toContain('Tableau')
    expect(container.textContent).toContain('Historique')
  })

  it('refreshes the board when a run change arrives on the realtime stream', async () => {
    fetchRuns.mockResolvedValueOnce([run({ id: 1, status: 'running' })])
    fetchRuns.mockResolvedValue([run({ id: 1, status: 'success' }), run({ id: 2, status: 'running' })])
    mockRole('run', 1)
    await mountResolved()
    expect(fetchRuns).toHaveBeenCalledTimes(1)
    expect(container.querySelector('[data-testid="run-board-card-2"]')).toBeNull()

    await act(async () => {
      lastStreamHandler.current?.({
        id: 10,
        kind: 'step.recorded',
        entity_type: 'run',
        entity_id: '1',
        tenant: 'default',
        payload: {},
      } as never)
      await new Promise((resolve) => setTimeout(resolve, 300))
      await new Promise((resolve) => setTimeout(resolve, 20))
      await Promise.resolve()
    })

    expect(fetchRuns).toHaveBeenCalledTimes(2)
    expect(container.querySelector('[data-testid="run-board-card-2"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="run-board-card-1"]')).toBeNull()
  })

  it('ignores non-run change events (no refetch)', async () => {
    fetchRuns.mockResolvedValue([run({ id: 1, status: 'running' })])
    mockRole('run', 1)
    await mountResolved()
    expect(fetchRuns).toHaveBeenCalledTimes(1)

    await act(async () => {
      lastStreamHandler.current?.({
        id: 5,
        kind: 'role.updated',
        entity_type: 'role',
        entity_id: 'ciso',
        tenant: 'default',
        payload: null,
      } as never)
      await new Promise((resolve) => setTimeout(resolve, 300))
    })

    expect(fetchRuns).toHaveBeenCalledTimes(1)
  })

  it('does not subscribe to the realtime stream when the board is forbidden', async () => {
    const { ApiForbiddenError } = await import('@/lib/api')
    fetchRuns.mockRejectedValue(new ApiForbiddenError())
    mockRole('read', 0)
    await mountResolved()

    expect(useEventStreamMock).toHaveBeenCalled()
    const lastCall = useEventStreamMock.mock.calls.at(-1)
    expect(lastCall?.[1]).toEqual({ enabled: false })
  })

  it('an all-done tenant still shows the four empty live columns plus a History shortcut', async () => {
    fetchRuns.mockResolvedValue([
      run({ id: 5, status: 'success', finished_at: '2026-07-18T10:00:08Z' }),
    ])
    mockRole('run', 1)
    await mountResolved()

    expect(container.querySelector('[data-testid="run-board-column-queued"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="run-board-column-done"]')).toBeNull()
    expect(container.querySelector('[data-testid="runs-done-shortcut"]')).not.toBeNull()
  })
})
