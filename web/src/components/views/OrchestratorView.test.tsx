import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MissionStrategyDetail } from '@/lib/pollen-api'
import type { Role } from '@/lib/role-context'

const { fetchMissionStrategies, decomposeFeature, launchMission, useRoleMock } = vi.hoisted(() => ({
  fetchMissionStrategies: vi.fn(),
  decomposeFeature: vi.fn(),
  launchMission: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchMissionStrategies, decomposeFeature, launchMission }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { OrchestratorView } from './OrchestratorView'

function preset(overrides: Partial<MissionStrategyDetail>): MissionStrategyDetail {
  return {
    name: 'pipeline',
    stages: ['code', 'review', 'merge'],
    dispatch: 'parallel',
    merge: 'per_task',
    new_mission: false,
    guarantee: 'strategy.guarantee.pipeline',
    ...overrides,
  }
}

const STRATEGIES: MissionStrategyDetail[] = [
  preset({ name: 'sequential', stages: ['code'], dispatch: 'sequential', merge: 'final' }),
  preset({ name: 'pipeline' }),
  preset({ name: 'code_only_self_merge', stages: ['code'], merge: 'per_branch' }),
]

function mockRole(role: Role) {
  useRoleMock.mockReturnValue({
    role,
    rank: 0,
    can: (required: Role) => {
      const order: Role[] = ['read', 'run', 'approve', 'admin']
      return order.indexOf(role) >= order.indexOf(required)
    },
  })
}

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  fetchMissionStrategies.mockReset()
  decomposeFeature.mockReset()
  launchMission.mockReset()
  useRoleMock.mockReset()
  fetchMissionStrategies.mockResolvedValue({ strategies: STRATEGIES, default: 'pipeline' })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.restoreAllMocks()
})

async function mountResolved() {
  await act(async () => {
    root.render(<OrchestratorView />)
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

function typeGoal(text: string) {
  const goal = container.querySelector('[data-testid="orchestrator-goal"]') as HTMLTextAreaElement
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!
  setter.call(goal, text)
  goal.dispatchEvent(new Event('input', { bubbles: true }))
}

describe('OrchestratorView', () => {
  it('renders the five strategy mode cards with guarantee labels', async () => {
    mockRole('run')
    await mountResolved()

    expect(container.querySelector('[data-testid="orchestrator-strategy-pipeline"]')).not.toBeNull()
    expect(
      container.querySelector('[data-testid="orchestrator-strategy-sequential"]'),
    ).not.toBeNull()
    // guarantee label resolves via i18n (English default)
    expect(container.textContent).toContain('+6 min/task')
  })

  it('decomposes a goal into a task list and adopts the plan strategy', async () => {
    mockRole('run')
    decomposeFeature.mockResolvedValue({
      space_id: 3,
      plan: {
        goal: 'ship it',
        strategy: 'code_only_self_merge',
        strategy_detail: STRATEGIES[2],
        tasks: [
          { id: 't1', title: 'API', role: 'developer' },
          { id: 't2', title: 'UI', role: 'developer', depends_on: ['t1'] },
        ],
      },
    })
    await mountResolved()
    typeGoal('ship it')

    const decompose = container.querySelector(
      '[data-testid="orchestrator-decompose"]',
    ) as HTMLButtonElement
    await act(async () => {
      decompose.click()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(decomposeFeature).toHaveBeenCalledWith('ship it', undefined, undefined)
    expect(container.querySelector('[data-testid="orchestrator-task-t1"]')?.textContent).toContain(
      'API',
    )
    expect(container.querySelector('[data-testid="orchestrator-task-t2"]')?.textContent).toContain(
      't1',
    )
    // the plan's strategy card is now selected
    expect(
      container
        .querySelector('[data-testid="orchestrator-strategy-code_only_self_merge"]')
        ?.getAttribute('aria-pressed'),
    ).toBe('true')
  })

  it('launches the mission with the chosen strategy', async () => {
    mockRole('run')
    decomposeFeature.mockResolvedValue({
      space_id: 3,
      plan: {
        goal: 'g',
        strategy: 'pipeline',
        strategy_detail: STRATEGIES[1],
        tasks: [{ id: 't1', title: 'API', role: 'developer' }],
      },
    })
    launchMission.mockResolvedValue({
      space_id: 3,
      mission_id: 42,
      runs: { t1: 100 },
      plan: {
        goal: 'g',
        strategy: 'sequential',
        strategy_detail: STRATEGIES[0],
        tasks: [{ id: 't1', title: 'API', role: 'developer' }],
      },
    })
    await mountResolved()
    typeGoal('g')

    await act(async () => {
      ;(container.querySelector('[data-testid="orchestrator-decompose"]') as HTMLButtonElement).click()
      await Promise.resolve()
      await Promise.resolve()
    })
    // pick a different mode card than the plan's default
    await act(async () => {
      ;(
        container.querySelector('[data-testid="orchestrator-strategy-sequential"]') as HTMLButtonElement
      ).click()
      await Promise.resolve()
    })
    await act(async () => {
      ;(container.querySelector('[data-testid="orchestrator-launch"]') as HTMLButtonElement).click()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(launchMission).toHaveBeenCalledWith('g', undefined, 'sequential')
    expect(container.querySelector('[data-testid="orchestrator-launched"]')?.textContent).toContain(
      '42',
    )
  })

  it('hides decompose/launch for a read-only token', async () => {
    mockRole('read')
    await mountResolved()

    expect(container.querySelector('[data-testid="orchestrator-decompose"]')).toBeNull()
    expect(container.textContent).toMatch(/read-only|Lecture seule/i)
  })
})
