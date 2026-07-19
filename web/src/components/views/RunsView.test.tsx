import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { RunSummary } from '@/lib/mirador-api'
import type { Role } from '@/lib/role-context'

const { fetchRuns, createRun, cancelRun, useRoleMock } = vi.hoisted(() => ({
  fetchRuns: vi.fn(),
  createRun: vi.fn(),
  cancelRun: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, fetchRuns, createRun, cancelRun }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { RunsView } from './RunsView'

function setNativeValue(input: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
  nativeSetter.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

const SAMPLE_RUN: RunSummary = {
  id: 7,
  project: 'acme-web',
  task: 'deploy',
  status: 'running',
  started_at: '2026-07-18T10:00:00Z',
  finished_at: null,
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
    root.render(<RunsView />)
  })
}

beforeEach(() => {
  fetchRuns.mockReset()
  createRun.mockReset()
  cancelRun.mockReset()
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

describe('RunsView', () => {
  it('shows a loading indicator before the list resolves', () => {
    fetchRuns.mockReturnValue(new Promise(() => {}))
    mockRole('run', 1)
    mount()
    expect(container.querySelector('[role="status"]')).not.toBeNull()
  })

  it('shows the empty state when there are no runs', async () => {
    fetchRuns.mockResolvedValue([])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/no runs yet/i)
  })

  it('renders run rows with id, project, task, status, started, finished — never detail', async () => {
    fetchRuns.mockResolvedValue([{ ...SAMPLE_RUN, detail: 'SECRET INTERNAL DETAIL' }])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('7')
    expect(container.textContent).toContain('acme-web')
    expect(container.textContent).toContain('deploy')
    expect(container.textContent).toContain('running')
    expect(container.textContent).not.toContain('SECRET INTERNAL DETAIL')
  })

  it('CRITICAL: hides the New Run form when the caller ranks below run', async () => {
    fetchRuns.mockResolvedValue([SAMPLE_RUN])
    mockRole('read', 0)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('#new-run-task')).toBeNull()
    expect(container.textContent).toMatch(/read-only/i)
  })

  it('shows the New Run form when the caller has a run-rank token', async () => {
    fetchRuns.mockResolvedValue([SAMPLE_RUN])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('#new-run-task')).not.toBeNull()
    expect(container.querySelector('#new-run-project')).not.toBeNull()
  })

  it('CRITICAL: New Run submit is disabled until both task and project are filled', async () => {
    fetchRuns.mockResolvedValue([])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const submitButton = Array.from(container.querySelectorAll('button')).find(
      (btn) => btn.textContent === 'New Run',
    ) as HTMLButtonElement
    expect(submitButton.disabled).toBe(true)

    const taskInput = container.querySelector('#new-run-task') as HTMLInputElement
    const projectInput = container.querySelector('#new-run-project') as HTMLInputElement
    act(() => {
      setNativeValue(taskInput, 'deploy')
    })
    expect(submitButton.disabled).toBe(true)
    act(() => {
      setNativeValue(projectInput, 'acme-web')
    })
    expect(submitButton.disabled).toBe(false)
  })

  it('CRITICAL: submits a new run via createRun and refreshes the list', async () => {
    fetchRuns.mockResolvedValueOnce([]).mockResolvedValueOnce([SAMPLE_RUN])
    mockRole('run', 1)
    createRun.mockResolvedValue({ run_id: 7, status: 'running' })

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

  it('CRITICAL: polls fetchRuns on an interval (≤3s) so status transitions show up', async () => {
    vi.useFakeTimers()
    fetchRuns.mockResolvedValue([SAMPLE_RUN])
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

  it('stops polling after unmount', async () => {
    vi.useFakeTimers()
    fetchRuns.mockResolvedValue([SAMPLE_RUN])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const callsBeforeUnmount = fetchRuns.mock.calls.length
    act(() => {
      root.unmount()
    })
    container.remove()

    await act(async () => {
      vi.advanceTimersByTime(9000)
      await Promise.resolve()
    })

    expect(fetchRuns.mock.calls.length).toBe(callsBeforeUnmount)
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

  it('shows an inline "insufficient role" message on a 403 from createRun', async () => {
    fetchRuns.mockResolvedValue([])
    mockRole('run', 1)
    const { ApiForbiddenError } = await import('@/lib/api')
    createRun.mockRejectedValue(new ApiForbiddenError())

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

    const alert = container.querySelector('[role="alert"]')
    expect(alert?.textContent).toMatch(/insufficient role/i)
  })

  it('CRITICAL: hides the Stop button when the caller ranks below run', async () => {
    fetchRuns.mockResolvedValue([SAMPLE_RUN])
    mockRole('read', 0)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[aria-label="Stop run 7"]')).toBeNull()
  })

  it('CRITICAL: shows a Stop button for a running run when the caller has a run-rank token', async () => {
    fetchRuns.mockResolvedValue([SAMPLE_RUN])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[aria-label="Stop run 7"]')).not.toBeNull()
  })

  it('does not show a Stop button for a non-running run even with a run-rank token', async () => {
    fetchRuns.mockResolvedValue([{ ...SAMPLE_RUN, status: 'success' }])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[aria-label="Stop run 7"]')).toBeNull()
  })

  it('CRITICAL: requires confirmation before calling cancelRun', async () => {
    fetchRuns.mockResolvedValue([SAMPLE_RUN])
    mockRole('run', 1)
    vi.spyOn(window, 'confirm').mockReturnValue(false)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const stopButton = container.querySelector('[aria-label="Stop run 7"]') as HTMLButtonElement
    await act(async () => {
      stopButton.click()
      await Promise.resolve()
    })

    expect(window.confirm).toHaveBeenCalledTimes(1)
    expect(cancelRun).not.toHaveBeenCalled()
  })

  it('CRITICAL: calling Stop after confirming invokes cancelRun and refreshes the list', async () => {
    fetchRuns.mockResolvedValueOnce([SAMPLE_RUN]).mockResolvedValueOnce([
      { ...SAMPLE_RUN, status: 'cancelled' },
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
    expect(fetchRuns).toHaveBeenCalledTimes(2)
  })

  it('CRITICAL: renders a cancelled run distinctly, not folded into the generic secondary bucket', async () => {
    fetchRuns.mockResolvedValue([{ ...SAMPLE_RUN, status: 'cancelled' }])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('cancelled')
    const badge = Array.from(container.querySelectorAll('span')).find(
      (el) => el.textContent === 'cancelled',
    )
    expect(badge).toBeDefined()
    // 'destructive' variant styling (distinguishes it from the plain
    // 'secondary' bucket every other/unknown status falls into).
    expect(badge?.className).toMatch(/destructive/)
  })

  it('shows an inline "insufficient role" message on a 403 from cancelRun', async () => {
    fetchRuns.mockResolvedValue([SAMPLE_RUN])
    mockRole('run', 1)
    const { ApiForbiddenError } = await import('@/lib/api')
    cancelRun.mockRejectedValue(new ApiForbiddenError())

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

    const alert = container.querySelector('[role="alert"]')
    expect(alert?.textContent).toMatch(/insufficient role/i)
  })
})
