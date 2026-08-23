import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { AgentsAdminResponse } from '@/lib/pollen-api'

const { fetchAgentsAdmin, agentAction, agentLogin } = vi.hoisted(() => ({
  fetchAgentsAdmin: vi.fn(),
  agentAction: vi.fn(),
  agentLogin: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchAgentsAdmin, agentAction, agentLogin }
})

import { AgentBinariesCard } from './AgentBinariesCard'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  fetchAgentsAdmin.mockReset()
  agentAction.mockReset()
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
})

function render(canAdmin = true) {
  act(() => {
    root.render(
      <LanguageProvider>
        <AgentBinariesCard canAdmin={canAdmin} />
      </LanguageProvider>,
    )
  })
}

async function flush() {
  await act(async () => {
    await Promise.resolve()
  })
}

const GROK = {
  kind: 'grok',
  name: 'Grok Build CLI',
  vendor: 'xAI',
  binary: 'grok',
  docs_url: 'https://docs.x.ai/build/overview',
  installable: true,
  updatable: true,
  on_service_path: true,
  installed_version: '1.0.5',
  auth: 'present',
  login_available: true,
}

function roster(agents: object[]): AgentsAdminResponse {
  return { agents } as AgentsAdminResponse
}

describe('AgentBinariesCard', () => {
  it('renders the version and the on-path badge from the SERVICE view', async () => {
    fetchAgentsAdmin.mockResolvedValue(roster([GROK]))

    render()
    await flush()

    expect(container.textContent).toContain('1.0.5')
    expect(container.textContent).toContain('Grok Build CLI')
  })

  it('a not-visible binary is NEUTRAL, never red — not installed is a choice', async () => {
    // The regression this pins: seven never-installed kinds rendered a wall
    // of destructive badges at the top of the Plugins page, and the operator
    // read them as plugins in error. From the service's view "not installed"
    // and "off the units' PATH" are the same fact; neither is a defect.
    fetchAgentsAdmin.mockResolvedValue(
      roster([{ ...GROK, on_service_path: false, installed_version: null, auth: 'unknown' }]),
    )

    render()
    await flush()

    expect(container.querySelector('.text-destructive')).toBeFalsy()
    // the explanation survives as a title on the neutral badge
    expect(container.querySelector('[title]')).toBeTruthy()
  })

  it('absent auth is red only when the service can SEE the binary', async () => {
    fetchAgentsAdmin.mockResolvedValue(
      roster([
        { ...GROK, kind: 'grok', auth: 'absent', on_service_path: true },
        { ...GROK, kind: 'cursor', name: 'Cursor', auth: 'absent', on_service_path: false },
      ]),
    )

    render()
    await flush()

    const destructive = Array.from(container.querySelectorAll('.text-destructive'))
    expect(destructive.length).toBeGreaterThan(0)
    const rows = Array.from(container.querySelectorAll('tr'))
    const cursorRow = rows.find((r) => r.textContent?.includes('Cursor'))
    expect(cursorRow?.querySelector('.text-destructive')).toBeFalsy()
  })

  it('one click never runs anything — the confirm step IS the consent', async () => {
    fetchAgentsAdmin.mockResolvedValue(roster([GROK]))
    render()
    await flush()

    const update = Array.from(container.querySelectorAll('button')).find((b) =>
      /update|mettre/i.test(b.textContent ?? ''),
    )
    expect(update).toBeTruthy()
    act(() => update!.dispatchEvent(new MouseEvent('click', { bubbles: true })))
    await flush()

    expect(agentAction).not.toHaveBeenCalled()

    const confirm = Array.from(container.querySelectorAll('button')).find((b) =>
      /confirm/i.test(b.textContent ?? ''),
    )
    expect(confirm).toBeTruthy()
    agentAction.mockResolvedValue({
      kind: 'grok',
      action: 'update',
      ok: true,
      version_before: '1.0.5',
      version_after: '1.0.6',
      on_service_path: true,
    })
    act(() => confirm!.dispatchEvent(new MouseEvent('click', { bubbles: true })))
    await flush()

    expect(agentAction).toHaveBeenCalledWith('grok', 'update')
    expect(container.textContent).toContain('1.0.5')
    expect(container.textContent).toContain('1.0.6')
  })

  it('a docs-only kind gets a LINK, never a button that lies', async () => {
    fetchAgentsAdmin.mockResolvedValue(
      roster([{ ...GROK, kind: 'qwen-code', name: 'Qwen', installable: false, updatable: false }]),
    )

    render()
    await flush()

    const link = container.querySelector('a[href="https://docs.x.ai/build/overview"]')
    expect(link).toBeTruthy()
    const buttons = Array.from(container.querySelectorAll('button')).filter((b) =>
      /install|update/i.test(b.textContent ?? ''),
    )
    expect(buttons).toHaveLength(0)
  })

  it('a non-admin sees the state but cannot press the buttons', async () => {
    fetchAgentsAdmin.mockResolvedValue(roster([GROK]))

    render(false)
    await flush()

    const actionable = Array.from(container.querySelectorAll('button')).filter(
      (b) => /install|update/i.test(b.textContent ?? '') && !(b as HTMLButtonElement).disabled,
    )
    expect(actionable).toHaveLength(0)
  })

  it('a failed action surfaces instead of pretending', async () => {
    fetchAgentsAdmin.mockResolvedValue(roster([GROK]))
    agentAction.mockRejectedValue(new Error('exit 1'))
    render()
    await flush()

    const update = Array.from(container.querySelectorAll('button')).find((b) =>
      /update/i.test(b.textContent ?? ''),
    )
    act(() => update!.dispatchEvent(new MouseEvent('click', { bubbles: true })))
    await flush()
    const confirm = Array.from(container.querySelectorAll('button')).find((b) =>
      /confirm/i.test(b.textContent ?? ''),
    )
    act(() => confirm!.dispatchEvent(new MouseEvent('click', { bubbles: true })))
    await flush()

    expect(container.textContent).toMatch(/exit 1|failed|échec/i)
  })
})


describe('AgentBinariesCard auth (#33)', () => {
  it('an unauthenticated agent with a verified flow gets a Login button that shows the URL', async () => {
    fetchAgentsAdmin.mockResolvedValue(roster([{ ...GROK, auth: 'absent' }]))
    agentLogin.mockResolvedValue({
      kind: 'grok',
      url: 'https://accounts.x.ai/activate?c=1',
      log: '/x.log',
    })
    render()
    await flush()

    const login = Array.from(container.querySelectorAll('button')).find((b) =>
      /login|connexion/i.test(b.textContent ?? ''),
    )
    expect(login).toBeTruthy()
    act(() => login!.dispatchEvent(new MouseEvent('click', { bubbles: true })))
    await flush()

    expect(agentLogin).toHaveBeenCalledWith('grok')
    const link = container.querySelector('a[href="https://accounts.x.ai/activate?c=1"]')
    expect(link).toBeTruthy()
  })

  it('an already-authenticated agent gets NO login button', async () => {
    fetchAgentsAdmin.mockResolvedValue(roster([{ ...GROK, auth: 'present' }]))
    render()
    await flush()

    const login = Array.from(container.querySelectorAll('button')).find((b) =>
      /login|connexion/i.test(b.textContent ?? ''),
    )
    expect(login).toBeFalsy()
  })

  it('unknown is a badge, never a button — no verified flow, no guess', async () => {
    fetchAgentsAdmin.mockResolvedValue(
      roster([{ ...GROK, kind: 'codex', auth: 'unknown', login_available: false }]),
    )
    render()
    await flush()

    expect(container.textContent).toMatch(/unknown|inconnu/i)
    const login = Array.from(container.querySelectorAll('button')).find((b) =>
      /login|connexion/i.test(b.textContent ?? ''),
    )
    expect(login).toBeFalsy()
  })
})
