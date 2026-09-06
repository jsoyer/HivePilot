import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { Role } from '@/lib/role-context'

const {
  fetchRoutines,
  fetchRoles,
  createRoutine,
  triggerRoutine,
  patchRoutine,
  deleteRoutine,
  useRoleMock,
} = vi.hoisted(() => ({
  fetchRoutines: vi.fn(),
  fetchRoles: vi.fn(),
  createRoutine: vi.fn(),
  triggerRoutine: vi.fn(),
  patchRoutine: vi.fn(),
  deleteRoutine: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return {
    ...actual,
    fetchRoutines,
    fetchRoles,
    createRoutine,
    triggerRoutine,
    patchRoutine,
    deleteRoutine,
  }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { RoutinesCard } from './RoutinesCard'

function mockRole(role: Role) {
  useRoleMock.mockReturnValue({
    role,
    rank: role === 'read' ? 0 : 1,
    can: (required: Role) => {
      const order: Role[] = ['read', 'run', 'approve', 'admin']
      return order.indexOf(role) >= order.indexOf(required)
    },
  })
}

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  fetchRoutines.mockReset()
  fetchRoles.mockReset()
  createRoutine.mockReset()
  triggerRoutine.mockReset()
  patchRoutine.mockReset()
  deleteRoutine.mockReset()
  useRoleMock.mockReset()
  fetchRoles.mockResolvedValue({
    roles: [{ name: 'developer', title: 'Developer', display_name: 'Gustave', command_task: 'developer' }],
  })
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.restoreAllMocks()
})

async function renderCard() {
  await act(async () => {
    root.render(
      <LanguageProvider>
        <RoutinesCard />
      </LanguageProvider>,
    )
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('RoutinesCard', () => {
  it('lists routines and fires a webhook trigger', async () => {
    mockRole('run')
    fetchRoutines.mockResolvedValue({
      routines: [
        {
          id: 'r1',
          tenant: 'default',
          role: 'developer',
          projects: ['example-api'],
          crons: ['0 9 * * 1'],
          timezone: 'Europe/Paris',
          next_run_at: null,
          last_run_at: null,
          enabled: true,
          replace_key: 'weekly-dev',
        },
      ],
    })
    triggerRoutine.mockResolvedValue({ routine_id: 'r1', status: 'triggered', detail: 'ok' })
    await renderCard()
    expect(container.querySelector('[data-testid="routine-weekly-dev"]')?.textContent).toContain(
      'developer',
    )
    const fire = Array.from(container.querySelectorAll('button')).find((btn) =>
      (btn.textContent || '').includes('Run now'),
    )
    expect(fire).toBeTruthy()
    await act(async () => {
      fire!.click()
      await Promise.resolve()
    })
    expect(triggerRoutine).toHaveBeenCalledWith('r1')
  })

  it('saves a new routine from the form', async () => {
    mockRole('run')
    fetchRoutines.mockResolvedValue({ routines: [] })
    createRoutine.mockResolvedValue({
      id: 'new',
      tenant: 'default',
      role: 'developer',
      projects: [],
      crons: ['0 9 * * 1'],
      timezone: 'UTC',
      next_run_at: null,
      last_run_at: null,
      enabled: true,
      replace_key: 'weekly-dev',
    })
    await renderCard()
    await act(async () => {
      container.querySelector<HTMLFormElement>('[data-testid="routine-form"]')!.requestSubmit()
      await Promise.resolve()
    })
    expect(createRoutine).toHaveBeenCalled()
    const payload = createRoutine.mock.calls[0][0] as { role: string; crons: string[] }
    expect(payload.role).toBe('developer')
    expect(payload.crons).toEqual(['0 9 * * 1'])
  })

  it('hides the form for a read token', async () => {
    mockRole('read')
    fetchRoutines.mockResolvedValue({ routines: [] })
    await renderCard()
    expect(container.querySelector('[data-testid="routine-form"]')).toBeNull()
    expect(container.textContent).toContain('run-rank')
  })
})
