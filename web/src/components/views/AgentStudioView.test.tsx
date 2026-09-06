import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { StudioRole } from '@/lib/pollen-api'
import type { Role } from '@/lib/role-context'

const { fetchRoles, createRole, updateRole, deleteRole, useRoleMock } = vi.hoisted(() => ({
  fetchRoles: vi.fn(),
  createRole: vi.fn(),
  updateRole: vi.fn(),
  deleteRole: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchRoles, createRole, updateRole, deleteRole }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { AgentStudioView } from './AgentStudioView'

let container: HTMLDivElement
let root: Root

const RANK: Record<string, number> = { read: 1, run: 2, approve: 3, admin: 4 }

function mockRole(role: Role) {
  useRoleMock.mockReturnValue({
    role,
    can: (needed: Role) => RANK[role] >= RANK[needed],
  })
}

function role(overrides: Partial<StudioRole> = {}): StudioRole {
  return {
    name: 'developer',
    title: 'Developer',
    display_name: 'Gustave',
    model_profile: 'implementation',
    runner: 'openai',
    inputs: ['spec'],
    outputs: ['code'],
    can_block: false,
    order: 1,
    prompt_text: 'Write the code.',
    ...overrides,
  }
}

async function mount() {
  await act(async () => {
    root.render(
      <LanguageProvider>
        <AgentStudioView />
      </LanguageProvider>,
    )
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('AgentStudioView', () => {
  beforeEach(() => {
    fetchRoles.mockReset()
    createRole.mockReset()
    updateRole.mockReset()
    deleteRole.mockReset()
    useRoleMock.mockReset()
    mockRole('admin')
    fetchRoles.mockResolvedValue({ roles: [role()] })
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
  })

  it('lists store roles with their names', async () => {
    await mount()
    expect(container.querySelector('[data-testid="studio-row-developer"]')).not.toBeNull()
    expect(container.textContent).toContain('Gustave')
  })

  it('hides the new-role control for a read token', async () => {
    mockRole('read')
    await mount()
    expect(container.querySelector('[data-testid="studio-new"]')).toBeNull()
  })

  it('refuses to save an empty new role', async () => {
    await mount()
    await act(async () => {
      container.querySelector<HTMLButtonElement>('[data-testid="studio-new"]')?.click()
    })
    await act(async () => {
      container.querySelector<HTMLFormElement>('[data-testid="studio-editor"]')?.requestSubmit()
    })
    expect(createRole).not.toHaveBeenCalled()
    expect(container.querySelector('[data-testid="studio-error"]')?.textContent).toMatch(/required/i)
  })

  it('saves an existing role via PUT', async () => {
    updateRole.mockResolvedValue(role())
    await mount()
    await act(async () => {
      container.querySelector<HTMLTableRowElement>('[data-testid="studio-row-developer"]')?.click()
    })
    await act(async () => {
      container.querySelector<HTMLFormElement>('[data-testid="studio-editor"]')?.requestSubmit()
    })
    expect(updateRole).toHaveBeenCalledWith(
      'developer',
      expect.objectContaining({ name: 'developer', title: 'Developer', prompt_text: 'Write the code.' }),
    )
  })

  it('opens an existing role for edit', async () => {
    await mount()
    await act(async () => {
      container.querySelector<HTMLTableRowElement>('[data-testid="studio-row-developer"]')?.click()
    })
    const name = container.querySelector<HTMLInputElement>('[data-testid="studio-field-name"]')
    expect(name?.value).toBe('developer')
    expect(name?.disabled).toBe(true)
    expect(container.querySelector('[data-testid="studio-delete"]')).not.toBeNull()
  })
})
