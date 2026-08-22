import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { AgentsAdminResponse } from '@/lib/pollen-api'

const { fetchAgentsAdmin, agentAction } = vi.hoisted(() => ({
  fetchAgentsAdmin: vi.fn(),
  agentAction: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchAgentsAdmin, agentAction }
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

  it('shows the off-path warning — the grok trap, reported not repeated', async () => {
    fetchAgentsAdmin.mockResolvedValue(roster([{ ...GROK, on_service_path: false }]))

    render()
    await flush()

    // the badge text comes from i18n; the destructive variant is the signal
    expect(container.querySelector('[title]')).toBeTruthy()
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
