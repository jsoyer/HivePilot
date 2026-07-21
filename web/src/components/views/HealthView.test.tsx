import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PluginsHealthResponse } from '@/lib/mirador-api'
import type { Role } from '@/lib/role-context'

const { fetchPluginsHealth, togglePlugin, useRoleMock } = vi.hoisted(() => ({
  fetchPluginsHealth: vi.fn(),
  togglePlugin: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, fetchPluginsHealth, togglePlugin }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { HealthView } from './HealthView'

let container: HTMLDivElement
let root: Root

const health: PluginsHealthResponse = {
  plugins: [
    { name: 'rtk', status: 'ok', detail: 'reachable' },
    { name: 'mem0', status: 'degraded', detail: 'self-hosted, slow' },
    { name: 'obsidian', status: 'error', detail: 'vault path missing' },
  ],
  disabled: [],
}

const healthWithDisabled: PluginsHealthResponse = {
  ...health,
  disabled: ['tmux'],
}

function mockRole(role: Role | null, rank: number) {
  useRoleMock.mockReturnValue({
    role,
    rank,
    can: (required: Role) => {
      if (role == null) return false
      const order: Role[] = ['read', 'run', 'approve', 'admin']
      return order.indexOf(role) >= order.indexOf(required)
    },
  })
}

function mount() {
  act(() => {
    root.render(<HealthView />)
  })
}

beforeEach(() => {
  fetchPluginsHealth.mockReset()
  togglePlugin.mockReset()
  useRoleMock.mockReset()
  mockRole(null, Number.NEGATIVE_INFINITY)
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

describe('HealthView', () => {
  it('shows a loading indicator before data resolves', () => {
    fetchPluginsHealth.mockReturnValue(new Promise(() => {}))
    mount()
    expect(container.querySelector('[role="status"]')).not.toBeNull()
  })

  it('renders one badge per plugin with its status and detail', async () => {
    fetchPluginsHealth.mockResolvedValue(health)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('rtk')
    expect(container.textContent).toContain('ok')
    expect(container.textContent).toContain('mem0')
    expect(container.textContent).toContain('degraded')
    expect(container.textContent).toContain('obsidian')
    expect(container.textContent).toContain('error')
    expect(container.textContent).toContain('vault path missing')
  })

  it('shows an empty state when no plugins are registered', async () => {
    fetchPluginsHealth.mockResolvedValue({ plugins: [], disabled: [] } satisfies PluginsHealthResponse)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/no plugins/i)
  })

  it('shows an error card when the endpoint rejects', async () => {
    fetchPluginsHealth.mockRejectedValue(new Error('unreachable'))

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')?.textContent).toContain('unreachable')
  })

  // -------------------------------------------------------------------------
  // Sprint 5: admin-gated enable/disable toggle
  // -------------------------------------------------------------------------

  it('CRITICAL: hides the toggle control when the caller ranks below admin', async () => {
    fetchPluginsHealth.mockResolvedValue(health)
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('button[aria-label="Disable rtk"]')).toBeNull()
    expect(container.querySelector('button[aria-label="Enable rtk"]')).toBeNull()
  })

  it('shows the toggle control for an admin token', async () => {
    fetchPluginsHealth.mockResolvedValue(health)
    mockRole('admin', 3)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.querySelector('button[aria-label="Disable rtk"]')).not.toBeNull()
    expect(container.querySelector('button[aria-label="Disable mem0"]')).not.toBeNull()
    expect(container.querySelector('button[aria-label="Disable obsidian"]')).not.toBeNull()
  })

  it('CRITICAL: clicking the toggle shows a "restart required" badge on that row only', async () => {
    fetchPluginsHealth.mockResolvedValue(health)
    mockRole('admin', 3)
    togglePlugin.mockResolvedValue({ name: 'rtk', disabled: true, restart_required: true })

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const rtkRow = (
      container.querySelector('button[aria-label="Disable rtk"]') as HTMLElement
    ).closest('li') as HTMLElement
    const mem0Row = (
      container.querySelector('button[aria-label="Disable mem0"]') as HTMLElement
    ).closest('li') as HTMLElement

    await act(async () => {
      rtkRow.querySelector('button')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(togglePlugin).toHaveBeenCalledWith('rtk')
    expect(rtkRow.textContent).toMatch(/restart required/i)
    expect(mem0Row.textContent).not.toMatch(/restart required/i)
  })

  it('flips the button label to Enable after a disable succeeds', async () => {
    fetchPluginsHealth.mockResolvedValue(health)
    mockRole('admin', 3)
    togglePlugin.mockResolvedValue({ name: 'rtk', disabled: true, restart_required: true })

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const disableButton = container.querySelector(
      'button[aria-label="Disable rtk"]',
    ) as HTMLButtonElement

    await act(async () => {
      disableButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('button[aria-label="Enable rtk"]')).not.toBeNull()
    expect(container.querySelector('button[aria-label="Disable rtk"]')).toBeNull()
  })

  it('shows an inline "insufficient role" message on a 403 from the toggle', async () => {
    fetchPluginsHealth.mockResolvedValue(health)
    mockRole('admin', 3)
    const { ApiForbiddenError } = await import('@/lib/api')
    togglePlugin.mockRejectedValue(new ApiForbiddenError())

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const disableButton = container.querySelector(
      'button[aria-label="Disable rtk"]',
    ) as HTMLButtonElement

    await act(async () => {
      disableButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    const alert = container.querySelector('[role="alert"]')
    expect(alert?.textContent).toMatch(/insufficient role/i)
  })

  // -------------------------------------------------------------------------
  // Follow-up: re-enable disabled plugins from the web (Health tab)
  // -------------------------------------------------------------------------

  it('renders a disabled plugin with an admin "Enable" toggle', async () => {
    fetchPluginsHealth.mockResolvedValue(healthWithDisabled)
    mockRole('admin', 3)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('tmux')
    expect(container.textContent).toMatch(/disabled plugins/i)
    expect(container.querySelector('button[aria-label="Enable tmux"]')).not.toBeNull()
  })

  it('clicking a disabled plugin\'s Enable toggle calls togglePlugin and shows the restart badge', async () => {
    fetchPluginsHealth.mockResolvedValue(healthWithDisabled)
    mockRole('admin', 3)
    togglePlugin.mockResolvedValue({ name: 'tmux', disabled: false, restart_required: true })

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const enableButton = container.querySelector(
      'button[aria-label="Enable tmux"]',
    ) as HTMLButtonElement
    const tmuxRow = enableButton.closest('li') as HTMLElement

    await act(async () => {
      enableButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(togglePlugin).toHaveBeenCalledWith('tmux')
    expect(tmuxRow.textContent).toMatch(/restart required/i)
    expect(container.querySelector('button[aria-label="Disable tmux"]')).not.toBeNull()
  })

  it('CRITICAL: non-admin does not see the re-enable toggle for a disabled plugin', async () => {
    fetchPluginsHealth.mockResolvedValue(healthWithDisabled)
    mockRole('run', 1)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('tmux')
    expect(container.querySelector('button[aria-label="Enable tmux"]')).toBeNull()
    expect(container.querySelector('button[aria-label="Disable tmux"]')).toBeNull()
  })

  // -------------------------------------------------------------------------
  // Dedupe: a plugin that is BOTH currently-loaded (data.plugins) AND flagged
  // for disable-on-restart (data.disabled) must render exactly once.
  // -------------------------------------------------------------------------

  const healthWithPendingDisable: PluginsHealthResponse = {
    plugins: [
      { name: 'rtk', status: 'ok', detail: 'reachable' },
      { name: 'tmux', status: 'ok', detail: 'session active' },
    ],
    // tmux is active right now AND already flagged in plugins_disabled --
    // the "disable" click from a previous session hasn't taken effect yet
    // because the API process hasn't restarted.
    disabled: ['tmux'],
  }

  it('CRITICAL: a plugin in both plugins and disabled renders exactly once, in the health section, with a pending-disable badge', async () => {
    fetchPluginsHealth.mockResolvedValue(healthWithPendingDisable)
    mockRole('admin', 3)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const tmuxRows = Array.from(container.querySelectorAll('li')).filter((li) =>
      li.textContent?.includes('tmux'),
    )
    expect(tmuxRows).toHaveLength(1)
    expect(tmuxRows[0].textContent).toMatch(/disable pending/i)
    // Seeded as already-flagged: the toggle should read "Enable" (undo the
    // pending disable), not "Disable" (which would be a no-op re-flag).
    expect(tmuxRows[0].querySelector('button[aria-label="Enable tmux"]')).not.toBeNull()
    expect(tmuxRows[0].querySelector('button[aria-label="Disable tmux"]')).toBeNull()

    // No "Disabled plugins" section at all -- tmux was the only disabled
    // name and it's already accounted for above.
    expect(container.textContent).not.toMatch(/disabled plugins/i)
  })

  it('keeps a truly-disabled plugin (not loaded) in the "Disabled plugins" section when another plugin is both loaded and pending-disable', async () => {
    fetchPluginsHealth.mockResolvedValue({
      plugins: healthWithPendingDisable.plugins,
      disabled: [...healthWithPendingDisable.disabled, 'obsidian'],
    } satisfies PluginsHealthResponse)
    mockRole('admin', 3)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    // obsidian is not loaded -- it belongs in "Disabled plugins" with Enable.
    expect(container.textContent).toMatch(/disabled plugins/i)
    expect(container.querySelector('button[aria-label="Enable obsidian"]')).not.toBeNull()

    // tmux is loaded -- still rendered exactly once, in the health section.
    const tmuxRows = Array.from(container.querySelectorAll('li')).filter((li) =>
      li.textContent?.includes('tmux'),
    )
    expect(tmuxRows).toHaveLength(1)
    expect(tmuxRows[0].textContent).toMatch(/disable pending/i)
  })

  it('clicking Enable on a pending-disable-but-active plugin clears the pending badge and flips to Disable', async () => {
    fetchPluginsHealth.mockResolvedValue(healthWithPendingDisable)
    mockRole('admin', 3)
    togglePlugin.mockResolvedValue({ name: 'tmux', disabled: false, restart_required: true })

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    const enableButton = container.querySelector(
      'button[aria-label="Enable tmux"]',
    ) as HTMLButtonElement
    const tmuxRow = enableButton.closest('li') as HTMLElement

    await act(async () => {
      enableButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(togglePlugin).toHaveBeenCalledWith('tmux')
    expect(tmuxRow.querySelector('button[aria-label="Disable tmux"]')).not.toBeNull()
    expect(tmuxRow.textContent).not.toMatch(/disable pending/i)
  })
})
