import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type { PluginsHealthResponse } from '@/lib/pollen-api'
import type { Role } from '@/lib/role-context'

const { fetchPluginsHealth, togglePlugin, useRoleMock, fetchHealthProbes } = vi.hoisted(() => ({
  fetchPluginsHealth: vi.fn(),
  togglePlugin: vi.fn(),
  useRoleMock: vi.fn(),
  fetchHealthProbes: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchPluginsHealth, togglePlugin, fetchHealthProbes }
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
    { name: 'rtk', status: 'ok', detail: 'reachable', activity_available: false, activity: null },
    { name: 'mem0', status: 'degraded', detail: 'self-hosted, slow', activity_available: false, activity: null },
    { name: 'obsidian', status: 'error', detail: 'vault path missing', activity_available: false, activity: null },
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
      { name: 'rtk', status: 'ok', detail: 'reachable', activity_available: false, activity: null },
      { name: 'tmux', status: 'ok', detail: 'session active', activity_available: false, activity: null },
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

  it('renders French title and status words when the language is fr (P1a)', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    fetchPluginsHealth.mockResolvedValue(health)
    mockRole('admin', 3)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <HealthView />
        </LanguageProvider>,
      )
      await Promise.resolve()
    })

    expect(container.textContent).toContain('État des plugins')
    expect(container.textContent).toContain('dégradé')
    expect(container.textContent).toContain('erreur')
    expect(container.querySelector('button[aria-label="Désactiver rtk"]')).not.toBeNull()
  })

  /**
   * Activity is a second answer, independent of status.
   *
   * These tests exist because the previous view had no way to be wrong out
   * loud: `headroom` and `mem0` both rendered a green `ok` badge for weeks
   * while failing every single call. Each case below pins one distinction
   * that, if it collapsed, would let that happen again.
   */
  describe('activity', () => {
    /** A naive UTC timestamp *minutes* ago, matching the SQLite
     * `CURRENT_TIMESTAMP` format the API actually returns. */
    function recentUtc(minutesAgo: number): string {
      return new Date(Date.now() - minutesAgo * 60_000)
        .toISOString()
        .replace('T', ' ')
        .slice(0, 19)
    }

    it('reports how much a measurable plugin has actually done', async () => {
      fetchPluginsHealth.mockResolvedValue({
        plugins: [
          {
            name: 'mem0',
            status: 'ok',
            detail: 'self-host',
            activity_available: true,
            activity: {
              last_used: recentUtc(5),
              events: 42,
              window_days: 30,
              evidence: 'memory_events',
            },
          },
        ],
        disabled: [],
      } satisfies PluginsHealthResponse)

      await act(async () => {
        mount()
        await Promise.resolve()
      })

      expect(container.textContent).toContain('42 events')
      expect(container.textContent).toContain('1 exercised')
    })

    it('flags a plugin that reports ok but has never run', async () => {
      // The exact state headroom sat in: loads, configured, green badge, and
      // it has never once done anything. The badge alone must not stand.
      fetchPluginsHealth.mockResolvedValue({
        plugins: [
          {
            name: 'headroom',
            status: 'ok',
            detail: 'compressor ready',
            activity_available: true,
            activity: {
              last_used: null,
              events: 0,
              window_days: 30,
              evidence: 'headroom_compressions + headroom_skips',
            },
          },
        ],
        disabled: [],
      } satisfies PluginsHealthResponse)

      await act(async () => {
        mount()
        await Promise.resolve()
      })

      expect(container.textContent).toContain('never run')
      expect(container.textContent).toContain('reports ok, never ran')
      expect(container.textContent).toContain('1 never run')
    })

    it('does not credit a presence-only plugin with a zero reading', async () => {
      // `rtk` is a PATH check; nothing records its use. "0 events" would read
      // as "installed but idle" -- a measurement that was never taken.
      fetchPluginsHealth.mockResolvedValue({
        plugins: [
          {
            name: 'rtk',
            status: 'ok',
            detail: 'on PATH',
            activity_available: false,
            activity: null,
          },
        ],
        disabled: [],
      } satisfies PluginsHealthResponse)

      await act(async () => {
        mount()
        await Promise.resolve()
      })

      expect(container.textContent).toContain('presence check only')
      expect(container.textContent).toContain('1 presence-only')
      // Counted as neither exercised nor never-run: it was not measured.
      expect(container.textContent).toContain('0 never run')
      expect(container.textContent).toContain('0 exercised')
      expect(container.textContent).not.toContain('0 events')
    })

    it('separates an unreadable probe from a plugin that never ran', async () => {
      // Measurable, but the read failed. Showing this as "never run" would
      // report a missing measurement as a finding.
      fetchPluginsHealth.mockResolvedValue({
        plugins: [
          {
            name: 'headroom',
            status: 'ok',
            detail: 'compressor ready',
            activity_available: true,
            activity: null,
          },
        ],
        disabled: [],
      } satisfies PluginsHealthResponse)

      await act(async () => {
        mount()
        await Promise.resolve()
      })

      expect(container.textContent).toContain('activity unreadable')
      // A failed read is not a finding: it must not be counted as never-run,
      // and must not raise the ok-but-never-ran flag.
      expect(container.textContent).toContain('0 never run')
      expect(container.textContent).not.toContain('reports ok, never ran')
    })

    it('keeps a long-idle plugin distinguishable from one that never ran', async () => {
      // `events: 0` inside the window, but it did run once -- and we know
      // when. Collapsing this to "never" would discard the only evidence
      // that the plugin has ever worked at all.
      fetchPluginsHealth.mockResolvedValue({
        plugins: [
          {
            name: 'mem0',
            status: 'ok',
            detail: 'self-host',
            activity_available: true,
            activity: {
              last_used: '2026-01-04 09:00:00',
              events: 0,
              window_days: 30,
              evidence: 'memory_events',
            },
          },
        ],
        disabled: [],
      } satisfies PluginsHealthResponse)

      await act(async () => {
        mount()
        await Promise.resolve()
      })

      expect(container.textContent).toContain('nothing in 30 d')
      expect(container.textContent).toContain('0 never run')
      expect(container.textContent).not.toContain('reports ok, never ran')
    })

    it('lands every plugin in exactly one summary bucket', async () => {
      // A count that silently omits a case is worse than no count: it
      // reassures about ground it never covered. An earlier version had no
      // `idle` or `unreadable` bucket, so a plugin dead for ninety days fell
      // through every counter while the strip still read "0 never run".
      fetchPluginsHealth.mockResolvedValue({
        plugins: [
          {
            name: 'active-one',
            status: 'ok',
            detail: '',
            activity_available: true,
            activity: { last_used: recentUtc(2), events: 7, window_days: 30, evidence: 'e' },
          },
          {
            name: 'idle-one',
            status: 'ok',
            detail: '',
            activity_available: true,
            activity: {
              last_used: '2026-01-04 09:00:00',
              events: 0,
              window_days: 30,
              evidence: 'e',
            },
          },
          {
            name: 'never-one',
            status: 'ok',
            detail: '',
            activity_available: true,
            activity: { last_used: null, events: 0, window_days: 30, evidence: 'e' },
          },
          {
            name: 'unreadable-one',
            status: 'ok',
            detail: '',
            activity_available: true,
            activity: null,
          },
          {
            name: 'presence-one',
            status: 'ok',
            detail: '',
            activity_available: false,
            activity: null,
          },
        ],
        disabled: [],
      } satisfies PluginsHealthResponse)

      await act(async () => {
        mount()
        await Promise.resolve()
      })

      // 1 + 1 + 1 + 1 + 1 = 5 loaded. Nothing falls through.
      expect(container.textContent).toContain('5 loaded')
      expect(container.textContent).toContain('1 exercised')
      expect(container.textContent).toContain('1 idle')
      expect(container.textContent).toContain('1 never run')
      expect(container.textContent).toContain('1 presence-only')
      expect(container.textContent).toContain('1 unreadable')
    })
  })

  // ---------------------------------------------------------------------------
  // The two states that had no surface anywhere.
  //
  // `check_all()` only covers REGISTERED plugins, so a plugin that is enabled
  // AND installed but rolled back at load (capability policy) appeared in
  // neither the healthy list nor the disabled list. It simply was not there.
  // ---------------------------------------------------------------------------

  it('renders a capability-denied plugin, which appears in no other list', async () => {
    fetchPluginsHealth.mockResolvedValue({
      plugins: [],
      disabled: [],
      denied: [
        {
          name: 'token_savior',
          source: 'local-file',
          error: "declares ['filesystem'] not permitted by the policy",
          remediation: 'add the declared capability to HIVEPILOT_PLUGINS_CAPABILITY_POLICY',
        },
      ],
      not_installed: [],
    } satisfies PluginsHealthResponse)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('token_savior')
    expect(container.textContent).toMatch(/not loaded/i)
  })

  it('shows the denial reason and how to fix it', async () => {
    // A denial an operator cannot act on is only marginally better than
    // silence — the whole point is that this state was previously invisible.
    fetchPluginsHealth.mockResolvedValue({
      plugins: [],
      disabled: [],
      denied: [
        {
          name: 'token_savior',
          source: 'local-file',
          error: "declares ['filesystem'] not permitted by the policy",
          remediation: 'add the declared capability to HIVEPILOT_PLUGINS_CAPABILITY_POLICY',
        },
      ],
      not_installed: [],
    } satisfies PluginsHealthResponse)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('filesystem')
    expect(container.textContent).toContain('HIVEPILOT_PLUGINS_CAPABILITY_POLICY')
  })

  it('lists plugins that are written but not installed on this host', async () => {
    fetchPluginsHealth.mockResolvedValue({
      plugins: [],
      disabled: [],
      denied: [],
      not_installed: ['onepassword', 'bitwarden'],
    } satisfies PluginsHealthResponse)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('onepassword')
    expect(container.textContent).toContain('bitwarden')
    expect(container.textContent).toMatch(/not installed/i)
  })

  it('renders nothing extra when an older API omits the new fields', async () => {
    // Pollen's bundle ships with the engine but a host can lag. Requiring the
    // fields would blank the whole tab against an older backend — trading a
    // missing section for a missing page.
    fetchPluginsHealth.mockResolvedValue({
      plugins: [
        { name: 'rtk', status: 'ok', detail: 'reachable', activity_available: false, activity: null },
      ],
      disabled: [],
    } satisfies PluginsHealthResponse)

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('rtk')
    expect(container.textContent).not.toMatch(/not loaded/i)
    expect(container.textContent).not.toMatch(/not installed/i)
  })
})

describe('HealthView — the probes for things that go quiet', () => {
  // The plugin table reports what LOADED. These report whether two systems
  // that produce continuously still are — the failure nothing else here can
  // see, because an absence looks exactly like a healthy zero.

  beforeEach(() => {
    // The plugin table's own fetch, which these tests are not about. Left
    // unmocked it returns undefined and `useAsyncData` calls `.then` on it —
    // the same fragility the probe panel was just isolated against, on the
    // other fetch.
    fetchPluginsHealth.mockResolvedValue(health)
    useRoleMock.mockReturnValue({ role: 'admin' as Role, can: () => true })
  })

  function probeFixture(over: Record<string, unknown> = {}) {
    return {
      agent_surface: { state: 'not_configured', backend: null },
      otel: { state: 'ok', rows: 10882, age_hours: 0.2 },
      ...over,
    }
  }

  async function badges(fixture: Record<string, unknown>) {
    fetchHealthProbes.mockResolvedValue(fixture)
    await act(async () => {
      mount()
      await Promise.resolve()
    })
    return {
      surface: container.querySelector('[data-testid="health-probe-surface"]')?.textContent ?? '',
      otel: container.querySelector('[data-testid="health-probe-otel"]')?.textContent ?? '',
    }
  }

  it('says an unconfigured surface is unconfigured, not broken', async () => {
    // Asserted on the TEXT, not the CSS class: the Badge component's own base
    // classes mention `destructive`, so a class check passes in every state
    // and proves nothing. What matters is what the operator reads.
    //
    // A red badge on every deployment that never asked for a live agent
    // surface teaches people to ignore the badge.
    const { surface } = await badges(probeFixture())

    expect(surface).not.toEqual('')
    expect(surface.toLowerCase()).not.toContain('not respond')
  })

  it('names the backend that is not answering', async () => {
    const { surface } = await badges(
      probeFixture({ agent_surface: { state: 'unreachable', backend: 'herdr' } }),
    )

    expect(surface).toContain('herdr')
  })

  it('says telemetry STOPPED when rows exist but stopped coming', async () => {
    // The count still looks healthy — 10882 rows — and only the AGE says the
    // exporter died. That is the plausible zero this probe exists to catch.
    const { otel } = await badges(
      probeFixture({ otel: { state: 'stale', rows: 10882, age_hours: 72 } }),
    )

    expect(otel).not.toEqual('')
    expect(otel).toContain('72')
  })

  it('says telemetry never arrived, which is a different problem', async () => {
    // Points at configuration rather than at an exporter that used to work.
    const { otel } = await badges(
      probeFixture({ otel: { state: 'never_arrived', rows: 0, age_hours: null } }),
    )

    expect(otel).not.toEqual('')
    expect(otel).not.toContain('72')
  })
})
