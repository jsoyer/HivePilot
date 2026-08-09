import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { PluginCatalogResponse } from '@/lib/pollen-api'
import type { Role } from '@/lib/role-context'

const { fetchPluginCatalog, installPlugin, togglePlugin, useRoleMock } = vi.hoisted(() => ({
  fetchPluginCatalog: vi.fn(),
  installPlugin: vi.fn(),
  togglePlugin: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchPluginCatalog, installPlugin, togglePlugin }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { PluginsView } from './PluginsView'

let container: HTMLDivElement
let root: Root

const RANK: Record<string, number> = { read: 1, run: 2, approve: 3, admin: 4 }

function mockRole(role: Role) {
  useRoleMock.mockReturnValue({
    role,
    can: (needed: Role) => RANK[role] >= RANK[needed],
  })
}

const catalog: PluginCatalogResponse = {
  plugins: [
    {
      name: 'rtk',
      description: 'Wraps a shell step with `rtk proxy` to cut token usage.',
      prereq_kind: 'binary',
      prereq_detail: 'the `rtk` binary on PATH',
      installed: true,
      enabled: true,
      env_flag: 'HIVEPILOT_RTK_ENABLED',
    },
    {
      name: 'onepassword',
      description: 'A `secrets` provider backed by 1Password.',
      prereq_kind: 'pip',
      prereq_detail: 'the `op` CLI on PATH, or `pip install onepassword-sdk`',
      installed: false,
      enabled: false,
      env_flag: 'HIVEPILOT_ONEPASSWORD_ENABLED',
    },
  ],
}

function mount() {
  act(() =>
    root.render(
      <LanguageProvider>
        <PluginsView />
      </LanguageProvider>,
    ),
  )
}

async function mountLoaded() {
  await act(async () => {
    mount()
    await Promise.resolve()
  })
}

describe('PluginsView', () => {
  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    fetchPluginCatalog.mockReset()
    installPlugin.mockReset()
    togglePlugin.mockReset()
    useRoleMock.mockReset()
    mockRole('admin')
    fetchPluginCatalog.mockResolvedValue(catalog)
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
    window.localStorage.clear()
  })

  it('renders one card per plugin, with its description', async () => {
    // A switch with no description is a switch nobody dares flip.
    await mountLoaded()

    expect(container.querySelector('[data-testid="plugin-card-rtk"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="plugin-card-onepassword"]')).not.toBeNull()
    expect(container.textContent).toContain('cut token usage')
  })

  it('shows plugins that are written but NOT installed', async () => {
    // The whole reason this is built on /plugins/catalog and not
    // /plugins/health: health only reports what loaded, so the ~23 inert
    // plugins would be invisible — which is how they stayed inert.
    await mountLoaded()

    const card = container.querySelector('[data-testid="plugin-card-onepassword"]')
    expect(card?.textContent).toMatch(/not installed/i)
  })

  it('reflects on/off state in the switch', async () => {
    await mountLoaded()

    const on = container.querySelector('[data-testid="plugin-card-rtk"] button[role="switch"]')
    const off = container.querySelector(
      '[data-testid="plugin-card-onepassword"] button[role="switch"]',
    )

    expect(on?.getAttribute('aria-checked')).toBe('true')
    expect(off?.getAttribute('aria-checked')).toBe('false')
  })

  it('INSTALLS when the plugin is not on disk yet', async () => {
    // Three different actions wear one control. Toggling an uninstalled
    // plugin has to install it, or the switch is decorative.
    installPlugin.mockResolvedValue({
      name: 'onepassword',
      installed_to: '/x/onepassword.py',
      enabled: true,
      restart_required: true,
      prereq_detail: 'the `op` CLI on PATH',
    })
    await mountLoaded()

    const sw = container.querySelector(
      '[data-testid="plugin-card-onepassword"] button[role="switch"]',
    ) as HTMLButtonElement
    await act(async () => {
      sw.click()
      await Promise.resolve()
    })

    expect(installPlugin).toHaveBeenCalledWith('onepassword')
    expect(togglePlugin).not.toHaveBeenCalled()
  })

  it('TOGGLES when the plugin is already installed', async () => {
    togglePlugin.mockResolvedValue({ name: 'rtk', disabled: true, restart_required: true })
    await mountLoaded()

    const sw = container.querySelector(
      '[data-testid="plugin-card-rtk"] button[role="switch"]',
    ) as HTMLButtonElement
    await act(async () => {
      sw.click()
      await Promise.resolve()
    })

    expect(togglePlugin).toHaveBeenCalledWith('rtk')
    expect(installPlugin).not.toHaveBeenCalled()
  })

  it('surfaces the prerequisite after installing, without being asked', async () => {
    // Installing also enables, so the prerequisite is the ONLY thing left
    // between "on" and "actually working".
    installPlugin.mockResolvedValue({
      name: 'onepassword',
      installed_to: '/x/onepassword.py',
      enabled: true,
      restart_required: true,
      prereq_detail: 'the `op` CLI on PATH',
    })
    await mountLoaded()

    const sw = container.querySelector(
      '[data-testid="plugin-card-onepassword"] button[role="switch"]',
    ) as HTMLButtonElement
    await act(async () => {
      sw.click()
      await Promise.resolve()
    })

    const card = container.querySelector('[data-testid="plugin-card-onepassword"]')
    expect(card?.textContent).toContain('onepassword-sdk')
  })

  it('says a restart is needed after a change', async () => {
    // PluginManager scans once at construction. A UI implying otherwise sends
    // the operator hunting a plugin that is on disk, enabled, and doing
    // nothing.
    togglePlugin.mockResolvedValue({ name: 'rtk', disabled: true, restart_required: true })
    await mountLoaded()

    const sw = container.querySelector(
      '[data-testid="plugin-card-rtk"] button[role="switch"]',
    ) as HTMLButtonElement
    await act(async () => {
      sw.click()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/restart/i)
  })

  it('states that the prerequisite is NOT installed for you', async () => {
    // Leaving it implicit is how a plugin ends up enabled, on disk, and
    // doing nothing. HivePilot fetches the plugin file and stops there.
    await mountLoaded()

    const buttons = Array.from(container.querySelectorAll('button'))
    const reveal = buttons.find((b) => /requirement/i.test(b.textContent ?? ''))
    await act(async () => {
      reveal?.click()
      await Promise.resolve()
    })

    expect(container.textContent).toMatch(/not installed for you|install it yourself|does not install/i)
  })

  it('disables every switch for a non-admin token', async () => {
    mockRole('read')
    await mountLoaded()

    const switches = Array.from(
      container.querySelectorAll<HTMLButtonElement>('button[role="switch"]'),
    )

    expect(switches.length).toBeGreaterThan(0)
    expect(switches.every((s) => s.disabled)).toBe(true)
  })

  it('shows an error without losing the card', async () => {
    togglePlugin.mockRejectedValue(new Error('boom'))
    await mountLoaded()

    const sw = container.querySelector(
      '[data-testid="plugin-card-rtk"] button[role="switch"]',
    ) as HTMLButtonElement
    await act(async () => {
      sw.click()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="plugin-card-rtk"]')).not.toBeNull()
  })
})
