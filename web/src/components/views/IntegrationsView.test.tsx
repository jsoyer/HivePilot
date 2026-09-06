import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { Role } from '@/lib/role-context'

const {
  fetchTypedTools,
  fetchMcpServers,
  fetchPluginPacks,
  importOpenApi,
  syncMcpServer,
  installPluginPack,
  useRoleMock,
} = vi.hoisted(() => ({
  fetchTypedTools: vi.fn(),
  fetchMcpServers: vi.fn(),
  fetchPluginPacks: vi.fn(),
  importOpenApi: vi.fn(),
  syncMcpServer: vi.fn(),
  installPluginPack: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return {
    ...actual,
    fetchTypedTools,
    fetchMcpServers,
    fetchPluginPacks,
    importOpenApi,
    syncMcpServer,
    installPluginPack,
  }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { IntegrationsView } from './IntegrationsView'

let container: HTMLDivElement
let root: Root

const RANK: Record<string, number> = { read: 1, run: 2, approve: 3, admin: 4 }

function mockRole(role: Role) {
  useRoleMock.mockReturnValue({
    role,
    can: (needed: Role) => RANK[role] >= RANK[needed],
  })
}

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('IntegrationsView', () => {
  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    mockRole('admin')
    fetchTypedTools.mockReset().mockResolvedValue({
      tools: [
        {
          qualified_name: 'openapi__demo__ping',
          local_name: 'ping',
          source_kind: 'openapi',
          source_id: 'demo',
        },
      ],
    })
    fetchMcpServers.mockReset().mockResolvedValue({
      servers: [{ id: 7, name: 'remote', transport: 'http', url: 'https://mcp.example/sse' }],
      cost_note: '',
    })
    fetchPluginPacks.mockReset().mockResolvedValue({
      packs: [
        {
          pack: {
            name: 'skills-kit',
            version: '1.0.0',
            description: 'Improve + shadcn',
            source: 'bundled',
            plugins: [],
            capabilities: [],
            credentials: [],
          },
          compatible: true,
          blockers: [],
          warnings: [],
        },
      ],
    })
    importOpenApi.mockReset().mockResolvedValue({ source: { name: 'demo' }, tools: [] })
    syncMcpServer.mockReset().mockResolvedValue({ tools: [] })
    installPluginPack.mockReset().mockResolvedValue({ pack: 'skills-kit', installed: [], restart_required: true })
  })

  afterEach(() => {
    act(() => {
      root.unmount()
    })
    container.remove()
  })

  it('lists typed tools, HTTP MCP sync, packs, and coming-soon catalogs', async () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <IntegrationsView />
        </LanguageProvider>,
      )
    })
    await flush()
    expect(container.querySelector('[data-testid="integrations-view"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="typed-tool-openapi__demo__ping"]')?.textContent).toContain(
      'openapi__demo__ping',
    )
    expect(container.querySelector('[data-testid="mcp-sync-7"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="pack-install-skills-kit"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="integrations-composio"]')?.textContent).toMatch(/Coming soon/)
    expect(container.querySelector('[data-testid="integrations-pipedream"]')?.textContent).toMatch(/Coming soon/)
  })

  it('imports OpenAPI as admin', async () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <IntegrationsView />
        </LanguageProvider>,
      )
    })
    await flush()
    const name = container.querySelector('[data-testid="openapi-name"]') as HTMLInputElement
    const text = container.querySelector('[data-testid="openapi-text"]') as HTMLTextAreaElement
    const nameSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
    const textSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!
    act(() => {
      nameSetter.call(name, 'demo')
      name.dispatchEvent(new Event('input', { bubbles: true }))
      textSetter.call(text, '{"openapi":"3.1.0"}')
      text.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => {
      ;(container.querySelector('[data-testid="openapi-import"]') as HTMLButtonElement).click()
      await Promise.resolve()
    })
    expect(importOpenApi).toHaveBeenCalledWith({ text: '{"openapi":"3.1.0"}', name: 'demo' })
  })

  it('disables write actions for a read token', async () => {
    mockRole('read')
    act(() => {
      root.render(
        <LanguageProvider>
          <IntegrationsView />
        </LanguageProvider>,
      )
    })
    await flush()
    expect((container.querySelector('[data-testid="openapi-import"]') as HTMLButtonElement).disabled).toBe(true)
    expect((container.querySelector('[data-testid="mcp-sync-7"]') as HTMLButtonElement).disabled).toBe(true)
    expect((container.querySelector('[data-testid="pack-install-skills-kit"]') as HTMLButtonElement).disabled).toBe(
      true,
    )
  })
})
