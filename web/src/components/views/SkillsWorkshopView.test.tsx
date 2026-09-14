import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Role } from '@/lib/role-context'
import type { SkillEvolutionCard, SkillProposal } from '@/lib/pollen-api'

const {
  fetchSkillProposals,
  acceptSkillProposal,
  rejectSkillProposal,
  fetchSkillEvolutions,
  approveSkillEvolution,
  rejectSkillEvolution,
  acceptSkillEvolution,
  useRoleMock,
} = vi.hoisted(() => ({
  fetchSkillProposals: vi.fn(),
  acceptSkillProposal: vi.fn(),
  rejectSkillProposal: vi.fn(),
  fetchSkillEvolutions: vi.fn(),
  approveSkillEvolution: vi.fn(),
  rejectSkillEvolution: vi.fn(),
  acceptSkillEvolution: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return {
    ...actual,
    fetchSkillProposals,
    acceptSkillProposal,
    rejectSkillProposal,
    fetchSkillEvolutions,
    approveSkillEvolution,
    rejectSkillEvolution,
    acceptSkillEvolution,
  }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

vi.mock('./EvolutionLineage', () => ({
  EvolutionLineage: ({ lineage }: { lineage: SkillEvolutionCard['lineage'] }) => (
    <div data-testid="evolution-lineage-stub">{lineage.nodes.map((node) => node.id).join(',')}</div>
  ),
}))

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

function evolution(over: Partial<SkillEvolutionCard> = {}): SkillEvolutionCard {
  return {
    id: 'e1',
    kind: 'skill_evolution',
    status: 'PENDING',
    name: 'faulty',
    evolution_type: 'fix',
    origin: 'fixed',
    content_hash: 'digest-1',
    merge_key: 'mk1',
    applied: false,
    would_mutate: false,
    reason: 'hitl_required',
    validation: { result: 'approve' },
    diffs: [
      {
        path: 'SKILL.md',
        before: '# old',
        after: '# new',
        unified: '--- a/SKILL.md\n+++ b/SKILL.md\n+# new',
      },
      {
        path: 'notes.md',
        before: '',
        after: 'note',
        unified: '--- a/notes.md\n+++ b/notes.md\n+note',
      },
    ],
    lineage: {
      nodes: [
        { id: 'parent', label: 'parent', kind: 'parent' },
        { id: 'faulty', label: 'faulty', kind: 'draft', origin: 'fixed' },
      ],
      edges: [{ source: 'parent', target: 'faulty' }],
    },
    ...over,
  }
}

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  fetchSkillProposals.mockReset()
  acceptSkillProposal.mockReset()
  rejectSkillProposal.mockReset()
  fetchSkillEvolutions.mockReset()
  approveSkillEvolution.mockReset()
  rejectSkillEvolution.mockReset()
  acceptSkillEvolution.mockReset()
  mockRole('approve')
  fetchSkillProposals.mockResolvedValue([proposal()])
  fetchSkillEvolutions.mockResolvedValue([evolution()])
  acceptSkillProposal.mockResolvedValue(proposal({ status: 'accepted' }))
  rejectSkillProposal.mockResolvedValue(proposal({ status: 'rejected' }))
  approveSkillEvolution.mockResolvedValue(evolution({ status: 'APPROVED' }))
  rejectSkillEvolution.mockResolvedValue(evolution({ status: 'REJECTED' }))
  acceptSkillEvolution.mockResolvedValue(evolution({ status: 'APPROVED', applied: true }))
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
    const accept = Array.from(container.querySelectorAll('[data-testid="workshop-row-p1"] button')).find(
      (el) => /accept|appliquer|accepter/i.test(el.textContent || ''),
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
    expect(acceptSkillEvolution).not.toHaveBeenCalled()
  })

  it('shows multi-file diffs and lineage for an evolution draft', async () => {
    await mount()
    expect(container.querySelector('[data-testid="evolution-diff-e1-SKILL.md"]')?.textContent).toContain(
      '# new',
    )
    expect(container.querySelector('[data-testid="evolution-diff-e1-notes.md"]')?.textContent).toContain(
      'note',
    )
    expect(container.querySelector('[data-testid="evolution-lineage-stub"]')?.textContent).toContain(
      'parent',
    )
  })

  it('keeps accept disabled until HITL approve', async () => {
    await mount()
    const accept = container.querySelector('[data-testid="evolution-accept-e1"]') as HTMLButtonElement
    expect(accept.disabled).toBe(true)
    const approve = Array.from(container.querySelectorAll('[data-testid="evolution-row-e1"] button')).find(
      (el) => /approve|approuver/i.test(el.textContent || ''),
    )
    expect(approve).toBeTruthy()
    await act(async () => {
      approve?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(approveSkillEvolution).toHaveBeenCalledWith('e1')
    expect(acceptSkillEvolution).not.toHaveBeenCalled()
  })

  it('accepts only after the draft is approved', async () => {
    fetchSkillEvolutions.mockResolvedValue([evolution({ status: 'APPROVED', would_mutate: true })])
    await mount()
    const accept = container.querySelector('[data-testid="evolution-accept-e1"]') as HTMLButtonElement
    expect(accept.disabled).toBe(false)
    await act(async () => {
      accept.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(acceptSkillEvolution).toHaveBeenCalledWith('e1', 'digest-1')
  })
})
