import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import roleMemorySource from './RoleMemoryView.tsx?raw'
import { ApiForbiddenError } from '@/lib/api'
import type { HindsightRolePanel, HindsightStatusResponse } from '@/lib/pollen-api'

const mocks = vi.hoisted(() => ({
  fetchHindsightStatus: vi.fn(),
  fetchHindsightRolePanel: vi.fn(),
  createHindsightMentalModel: vi.fn(),
  updateHindsightMentalModel: vi.fn(),
  refreshHindsightMentalModel: vi.fn(),
  curateHindsightMemory: vi.fn(),
  can: vi.fn((role: string) => role === 'run'),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, ...mocks }
})

vi.mock('@/lib/role-context', () => ({
  useRole: () => ({ role: 'run', can: mocks.can }),
}))

import { RoleMemoryView } from './RoleMemoryView'

const status: HindsightStatusResponse = {
  configured: true,
  roles: [
    { name: 'developer', display_name: 'Gustave', bank_id: 'role:developer' },
    { name: 'reviewer', display_name: null, bank_id: 'role:reviewer' },
  ],
}

const panel: HindsightRolePanel = {
  configured: true,
  role: 'developer',
  bank_id: 'role:developer',
  mental_models: [
    {
      id: 'prefs',
      name: 'Preferences',
      source_query: 'What does the user prefer?',
      content: 'Dark mode.',
      last_refreshed_at: '2026-09-01T00:00:00Z',
      is_stale: true,
      tags: ['ui'],
    },
  ],
  observations: [
    {
      id: 'obs-1',
      text: 'Prefers dark mode',
      fact_type: 'observation',
      state: 'valid',
      proof_count: 2,
      confidence: 0.81,
      quotes: [{ text: 'use dark theme', source_id: 'w1' }],
      evidence: [{ id: 'w1', text: 'use dark theme', fact_type: 'world', state: 'valid' }],
      edited_at: null,
    },
  ],
}

let container: HTMLDivElement
let root: Root

function mount() {
  act(() => {
    root.render(<RoleMemoryView />)
  })
}

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

beforeEach(() => {
  mocks.fetchHindsightStatus.mockResolvedValue(status)
  mocks.fetchHindsightRolePanel.mockResolvedValue(panel)
  mocks.can.mockImplementation((role: string) => role === 'run')
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
  vi.clearAllMocks()
})

describe('RoleMemoryView', () => {
  it('loads the first role bank and shows models plus observations', async () => {
    mount()
    await flush()

    expect(mocks.fetchHindsightStatus).toHaveBeenCalled()
    expect(mocks.fetchHindsightRolePanel).toHaveBeenCalledWith('developer')
    expect(container.textContent).toContain('role:developer')
    expect(container.textContent).toContain('Preferences')
    expect(container.textContent).toContain('Dark mode.')
    expect(container.textContent).toContain('Prefers dark mode')
    expect(container.textContent).toContain('use dark theme')
    expect(container.textContent).toContain('81%')
    expect(container.textContent).toContain('Proofs')
  })

  it('shows the unconfigured detail without calling Hindsight mutations', async () => {
    mocks.fetchHindsightStatus.mockResolvedValue({
      ...status,
      configured: false,
      detail: 'Hindsight is disabled (HIVEPILOT_HINDSIGHT_ENABLED).',
    })
    mocks.fetchHindsightRolePanel.mockResolvedValue({
      ...panel,
      configured: false,
      mental_models: [],
      observations: [],
      detail: 'Hindsight is disabled (HIVEPILOT_HINDSIGHT_ENABLED).',
    })
    mount()
    await flush()

    expect(container.querySelector('[data-testid="role-memory-unconfigured"]')?.textContent).toMatch(
      /disabled/i,
    )
    expect(container.querySelector('[data-testid="role-memory-create-model"]')).toBeNull()
  })

  it('creates a mental model when the operator can run', async () => {
    mocks.createHindsightMentalModel.mockResolvedValue({
      ok: true,
      mental_model: panel.mental_models[0],
    })
    mount()
    await flush()

    const name = container.querySelector(
      '[data-testid="role-memory-create-model"] input',
    ) as HTMLInputElement
    const query = container.querySelector(
      '[data-testid="role-memory-create-model"] textarea',
    ) as HTMLTextAreaElement
    const setInput = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
    const setArea = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set
    await act(async () => {
      setInput?.call(name, 'Style')
      name.dispatchEvent(new Event('input', { bubbles: true }))
      setArea?.call(query, 'What style?')
      query.dispatchEvent(new Event('input', { bubbles: true }))
      await Promise.resolve()
    })
    const form = container.querySelector('[data-testid="role-memory-create-model"]') as HTMLFormElement
    await act(async () => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      await Promise.resolve()
    })
    expect(mocks.createHindsightMentalModel).toHaveBeenCalledWith('developer', {
      name: 'Style',
      source_query: 'What style?',
    })
  })

  it('hides edit controls when the token cannot run', async () => {
    mocks.can.mockReturnValue(false)
    mount()
    await flush()
    expect(container.querySelector('[data-testid="role-memory-create-model"]')).toBeNull()
    expect(container.textContent).not.toContain('Correct source fact')
  })

  it('renders a 403 as the token banner, not a generic alert', async () => {
    mocks.fetchHindsightStatus.mockRejectedValue(new ApiForbiddenError())
    mount()
    await flush()
    expect(container.querySelector('[data-testid="role-memory-forbidden"]')).not.toBeNull()
    expect(container.querySelector('[role="alert"]')).toBeNull()
  })

  it('renders Hindsight strings as literal text', async () => {
    mocks.fetchHindsightRolePanel.mockResolvedValue({
      ...panel,
      mental_models: [
        {
          ...panel.mental_models[0],
          name: '<img src=x onerror=alert(1)>',
          content: '<script>alert(2)</script>',
        },
      ],
      observations: [
        {
          ...panel.observations[0],
          text: '<b>xss</b>',
          quotes: [{ text: '<img src=y>', source_id: 'w1' }],
        },
      ],
    })
    mount()
    await flush()
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>')
    expect(container.textContent).toContain('<script>alert(2)</script>')
    expect(container.textContent).toContain('<b>xss</b>')
  })

  it('never uses dangerouslySetInnerHTML', () => {
    expect(roleMemorySource).not.toContain('dangerouslySetInnerHTML')
  })
})
