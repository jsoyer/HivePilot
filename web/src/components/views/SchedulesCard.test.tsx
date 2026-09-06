import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { Role } from '@/lib/role-context'

const { fetchSchedules, triggerSchedule, useRoleMock } = vi.hoisted(() => ({
  fetchSchedules: vi.fn(),
  triggerSchedule: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchSchedules, triggerSchedule }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { SchedulesCard } from './SchedulesCard'

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
  fetchSchedules.mockReset()
  triggerSchedule.mockReset()
  useRoleMock.mockReset()
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

describe('SchedulesCard', () => {
  it('lists yaml schedules and fires a named trigger', async () => {
    mockRole('run')
    fetchSchedules.mockResolvedValue({
      schedules: [
        {
          name: 'docs-weekly',
          task: 'docs',
          source: null,
          projects: ['example-api'],
          interval_minutes: 60,
          enabled: true,
          remember: false,
          last_run_at: null,
        },
      ],
    })
    triggerSchedule.mockResolvedValue({
      schedule_name: 'docs-weekly',
      status: 'triggered',
      detail: 'ok',
    })
    await act(async () => {
      root.render(
        <LanguageProvider>
          <SchedulesCard />
        </LanguageProvider>,
      )
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(container.querySelector('[data-testid="schedule-docs-weekly"]')?.textContent).toContain(
      'docs-weekly',
    )
    const button = container.querySelector('button') as HTMLButtonElement
    await act(async () => {
      button.click()
      await Promise.resolve()
    })
    expect(triggerSchedule).toHaveBeenCalledWith('docs-weekly')
  })
})
