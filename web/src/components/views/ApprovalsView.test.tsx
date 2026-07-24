import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type { Approval } from '@/lib/mirador-api'
import type { Role } from '@/lib/role-context'

const { fetchApprovals, postApproval, useRoleMock } = vi.hoisted(() => ({
  fetchApprovals: vi.fn(),
  postApproval: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, fetchApprovals, postApproval }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { ApprovalsView } from './ApprovalsView'

function setNativeValue(input: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
  nativeSetter.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

const SAMPLE_APPROVAL: Approval = {
  run_id: 42,
  project: 'acme-web',
  task: 'deploy',
  status: 'pending',
  requested_at: '2026-07-18T10:00:00Z',
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
    root.render(<ApprovalsView />)
  })
}

beforeEach(() => {
  fetchApprovals.mockReset()
  postApproval.mockReset()
  useRoleMock.mockReset()
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
})

describe('ApprovalsView', () => {
  it('shows a loading indicator before the list resolves', () => {
    fetchApprovals.mockReturnValue(new Promise(() => {}))
    mockRole('approve', 2)
    mount()
    expect(container.querySelector('[role="status"]')).not.toBeNull()
  })

  it('shows the empty state when there are no pending approvals', async () => {
    fetchApprovals.mockResolvedValue([])
    mockRole('approve', 2)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/no pending approvals/i)
  })

  it('renders approval rows with run id, project, task, requested-at, and status — never metadata', async () => {
    fetchApprovals.mockResolvedValue([
      { ...SAMPLE_APPROVAL, metadata: '{"extra_prompt":"SECRET INTERNAL PROMPT"}' },
    ])
    mockRole('approve', 2)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('42')
    expect(container.textContent).toContain('acme-web')
    expect(container.textContent).toContain('deploy')
    expect(container.textContent).toContain('pending')
    expect(container.textContent).not.toContain('SECRET INTERNAL PROMPT')
  })

  it('CRITICAL: hides Approve/Deny buttons when the caller ranks below approve', async () => {
    fetchApprovals.mockResolvedValue([SAMPLE_APPROVAL])
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('button[aria-label="Approve run 42"]')).toBeNull()
    expect(container.querySelector('button[aria-label="Deny run 42"]')).toBeNull()
    expect(container.textContent).toMatch(/read-only/i)
  })

  it('shows Approve/Deny buttons when the caller has an approve-rank token', async () => {
    fetchApprovals.mockResolvedValue([SAMPLE_APPROVAL])
    mockRole('approve', 2)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('button[aria-label="Approve run 42"]')).not.toBeNull()
    expect(container.querySelector('button[aria-label="Deny run 42"]')).not.toBeNull()
  })

  it('CRITICAL: Deny requires a non-empty reason before it can be submitted', async () => {
    fetchApprovals.mockResolvedValue([SAMPLE_APPROVAL])
    mockRole('approve', 2)
    postApproval.mockResolvedValue({ result: { success: true } })

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const denyToggle = container.querySelector('button[aria-label="Deny run 42"]') as HTMLElement
    act(() => {
      denyToggle.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    const findConfirmDeny = () =>
      Array.from(container.querySelectorAll('button')).find(
        (btn) => btn.textContent === 'Confirm deny',
      ) as HTMLButtonElement

    // No reason entered yet: the confirm button must stay disabled, and
    // clicking it (a disabled button never fires onClick) must not submit.
    expect(findConfirmDeny().disabled).toBe(true)
    act(() => {
      findConfirmDeny().dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    expect(postApproval).not.toHaveBeenCalled()

    const reasonInput = container.querySelector(
      'input[aria-label="Denial reason for run 42"]',
    ) as HTMLInputElement
    act(() => {
      setNativeValue(reasonInput, 'not ready for prod')
    })

    expect(findConfirmDeny().disabled).toBe(false)

    await act(async () => {
      findConfirmDeny().dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    expect(postApproval).toHaveBeenCalledWith(42, { approve: false, reason: 'not ready for prod' })
  })

  it('CRITICAL: shows a processing indicator and disables actions during an in-flight POST', async () => {
    fetchApprovals.mockResolvedValue([SAMPLE_APPROVAL])
    mockRole('approve', 2)
    postApproval.mockReturnValue(new Promise(() => {})) // never resolves in this test

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const approveButton = container.querySelector(
      'button[aria-label="Approve run 42"]',
    ) as HTMLButtonElement

    await act(async () => {
      approveButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    expect(container.querySelector('[role="status"]')?.textContent).toMatch(/processing/i)
    expect(approveButton.disabled).toBe(true)
    const denyButton = container.querySelector('button[aria-label="Deny run 42"]') as HTMLButtonElement
    expect(denyButton.disabled).toBe(true)
  })

  it('shows an inline "insufficient role" message on a 403 from the POST', async () => {
    fetchApprovals.mockResolvedValue([SAMPLE_APPROVAL])
    mockRole('approve', 2)
    const { ApiForbiddenError } = await import('@/lib/api')
    postApproval.mockRejectedValue(new ApiForbiddenError())

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const approveButton = container.querySelector(
      'button[aria-label="Approve run 42"]',
    ) as HTMLButtonElement

    await act(async () => {
      approveButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    const alert = container.querySelector('[role="alert"]')
    expect(alert?.textContent).toMatch(/insufficient role/i)
  })

  it('refreshes the list after a successful approve', async () => {
    fetchApprovals.mockResolvedValueOnce([SAMPLE_APPROVAL]).mockResolvedValueOnce([])
    mockRole('approve', 2)
    postApproval.mockResolvedValue({ result: { success: true } })

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const approveButton = container.querySelector(
      'button[aria-label="Approve run 42"]',
    ) as HTMLButtonElement

    await act(async () => {
      approveButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(fetchApprovals).toHaveBeenCalledTimes(2)
    expect(container.textContent).toMatch(/no pending approvals/i)
  })

  it('CRITICAL: a plain read/run token 403 on GET /v1/approvals shows a graceful message, not a crash', async () => {
    const { ApiForbiddenError } = await import('@/lib/api')
    fetchApprovals.mockRejectedValue(new ApiForbiddenError())
    mockRole('read', 0)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')).toBeNull()
    expect(container.textContent).toMatch(/run-rank/i)
  })

  it('renders French title, description, and table headers when the language is fr (P1a)', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    fetchApprovals.mockResolvedValue([SAMPLE_APPROVAL])
    mockRole('approve', 2)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <ApprovalsView />
        </LanguageProvider>,
      )
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Approbations')
    expect(container.textContent).toContain('Projet')
    expect(container.textContent).toContain('Tâche')
    expect(container.textContent).toContain('Approuver')
    expect(container.textContent).toContain('Refuser')
  })

  it('renders the French empty state when the language is fr', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    fetchApprovals.mockResolvedValue([])
    mockRole('approve', 2)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <ApprovalsView />
        </LanguageProvider>,
      )
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Aucune approbation en attente.')
  })
})
