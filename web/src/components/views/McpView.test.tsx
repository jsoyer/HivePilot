import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { McpCatalogEntry, McpServer } from '@/lib/pollen-api'
import type { Role } from '@/lib/role-context'

const {
  fetchMcpServers,
  fetchMcpCatalog,
  importMcpConfig,
  addMcpFromCatalog,
  probeMcpServer,
  deleteMcpServer,
  useRoleMock,
} = vi.hoisted(() => ({
  fetchMcpServers: vi.fn(),
  fetchMcpCatalog: vi.fn(),
  importMcpConfig: vi.fn(),
  addMcpFromCatalog: vi.fn(),
  probeMcpServer: vi.fn(),
  deleteMcpServer: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return {
    ...actual,
    fetchMcpServers,
    fetchMcpCatalog,
    importMcpConfig,
    addMcpFromCatalog,
    probeMcpServer,
    deleteMcpServer,
  }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { McpView } from './McpView'

let container: HTMLDivElement
let root: Root

const RANK: Record<string, number> = { read: 1, run: 2, approve: 3, admin: 4 }

function mockRole(role: Role) {
  useRoleMock.mockReturnValue({
    role,
    can: (needed: Role) => RANK[role] >= RANK[needed],
  })
}

function server(overrides: Partial<McpServer> = {}): McpServer {
  return {
    id: 1,
    name: 'memory',
    transport: 'stdio',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-memory'],
    last_probe_status: 'missing',
    last_probe_detail: 'npx is not on PATH',
    ...overrides,
  }
}

function catalogEntry(overrides: Partial<McpCatalogEntry> = {}): McpCatalogEntry {
  return {
    name: 'github',
    description: 'GitHub issues and PRs.',
    transport: 'stdio',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-github'],
    paste: 'npx -y @modelcontextprotocol/server-github',
    installed: false,
    ...overrides,
  }
}

async function mountLoaded() {
  await act(async () => {
    root.render(
      <LanguageProvider>
        <McpView />
      </LanguageProvider>,
    )
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('McpView', () => {
  beforeEach(() => {
    fetchMcpServers.mockReset().mockResolvedValue({ servers: [], cost_note: '' })
    fetchMcpCatalog.mockReset().mockResolvedValue({ catalog: [catalogEntry()] })
    importMcpConfig.mockReset()
    addMcpFromCatalog.mockReset()
    probeMcpServer.mockReset()
    deleteMcpServer.mockReset()
    mockRole('admin')
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
  })

  it('shows the paste box for an admin and lists catalog entries', async () => {
    await mountLoaded()
    expect(container.querySelector('[data-testid="mcp-paste"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="mcp-catalog-github"]')?.textContent).toContain(
      'GitHub',
    )
  })

  it('hides import for a read-only token', async () => {
    mockRole('read')
    await mountLoaded()
    expect(container.querySelector('[data-testid="mcp-paste"]')).toBeNull()
    expect(container.textContent).toMatch(/Read-only|Lecture seule/i)
  })

  it('renders an installed server row with its probe status', async () => {
    fetchMcpServers.mockResolvedValue({ servers: [server()], cost_note: '' })
    await mountLoaded()
    expect(container.querySelector('[data-testid="mcp-row-memory"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="mcp-status-memory"]')?.textContent).toContain(
      'missing',
    )
  })

  it('imports pasted JSON', async () => {
    importMcpConfig.mockResolvedValue({ drafts: [], servers: [server()], stripped_env_keys: [] })
    fetchMcpServers
      .mockResolvedValueOnce({ servers: [], cost_note: '' })
      .mockResolvedValue({ servers: [server()], cost_note: '' })
    await mountLoaded()
    const textarea = container.querySelector('[data-testid="mcp-paste"]') as HTMLTextAreaElement
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!
    await act(async () => {
      setter.call(textarea, '{"mcpServers":{"memory":{"command":"npx"}}}')
      textarea.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => {
      ;(container.querySelector('[data-testid="mcp-import"]') as HTMLButtonElement).click()
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(importMcpConfig).toHaveBeenCalled()
  })
})
