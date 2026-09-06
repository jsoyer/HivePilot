import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Role } from '@/lib/role-context'
import type { SkillProposal } from '@/lib/pollen-api'

const { fetchSkillProposals, acceptSkillProposal, rejectSkillProposal, useRoleMock } = vi.hoisted(
  () => ({
    fetchSkillProposals: vi.fn(),
    acceptSkillProposal: vi.fn(),
    rejectSkillProposal: vi.fn(),
    useRoleMock: vi.fn(),
  }),
)

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchSkillProposals, acceptSkillProposal, rejectSkillProposal }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { SkillsWorkshopView } from './SkillsWorkshopView'

const RANK: Record<string, number> = { read: 1, run: 2, approve: 3, admin: 4 }

function mockRole(role: Role) {
  useRoleMock.mockReturnValue({
    role,
    can: (needed: Role) => RANK[role] >= RANK[needed],
  })
}

function proposal(over: Partial<SkillProposal> = {}): SkillProposal {
  return {
    id: 'p1',
    skill_name: 'demo',
    tenant: 'default',
    status: 'proposed',
    provider: 'directory:/tmp/skills/demo',
    run_id: 3,
    step: 'impl',
    rationale: 'step failed',
    base_digest: 'abc',
    patch_json: '{}',
    diff_text: '+ Always run tests',
    created_ts: '2026-09-06T00:00:00',
    decided_ts: null,
    decided_by: null,
    ...over,
  }
}

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  fetchSkillProposals.mockReset()
  acceptSkillProposal.mockReset()
  rejectSkillProposal.mockReset()
  mockRole('approve')
  fetchSkillProposals.mockResolvedValue([proposal()])
  acceptSkillProposal.mockResolvedValue(proposal({ status: 'accepted' }))
  rejectSkillProposal.mockResolvedValue(proposal({ status: 'rejected' }))
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
})

async function mount() {
  await act(async () => {
    root.render(<SkillsWorkshopView />)
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('SkillsWorkshopView', () => {
  it('shows the proposed diff', async () => {
    await mount()
    expect(container.querySelector('[data-testid="workshop-diff-p1"]')?.textContent).toContain(
      'Always run tests',
    )
  })

  it('accepts a proposal when the operator has approve rank', async () => {
    await mount()
    const accept = Array.from(container.querySelectorAll('button')).find((el) =>
      /accept|appliquer/i.test(el.textContent || ''),
    )
    expect(accept).toBeTruthy()
    await act(async () => {
      accept?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(acceptSkillProposal).toHaveBeenCalledWith('p1')
  })

  it('hides accept/reject for a read token', async () => {
    mockRole('read')
    await mount()
    expect(container.textContent).toMatch(/approve|approuver/i)
    expect(acceptSkillProposal).not.toHaveBeenCalled()
  })
})
