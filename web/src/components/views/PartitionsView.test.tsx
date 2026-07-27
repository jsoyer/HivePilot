import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import { en } from '@/lib/i18n/en'
import { fr } from '@/lib/i18n/fr'
import type { PartitionDetail, PartitionPreview, PartitionSummary } from '@/lib/pollen-api'
import type { Role } from '@/lib/role-context'

const { fetchPartitions, fetchPartition, previewPartition, ratifyPartition, useRoleMock } =
  vi.hoisted(() => ({
    fetchPartitions: vi.fn(),
    fetchPartition: vi.fn(),
    previewPartition: vi.fn(),
    ratifyPartition: vi.fn(),
    useRoleMock: vi.fn(),
  }))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchPartitions, fetchPartition, previewPartition, ratifyPartition }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

import { PREVIEW_DEBOUNCE_MS, PartitionsView } from './PartitionsView'

const PLAN = {
  partition_version: 1,
  source: { kind: 'text', ref: 'docs/bug-1234.md', digest: 'sha256:aaa' },
  proposer: { role: 'partitioner', pipeline: 'propose-partition', run_id: 4711 },
  policy: { max_parallel: 3, on_task_failure: 'continue' },
  tasks: [
    {
      id: 'parse-guard',
      title: 'Guard the null deref in the parser',
      project: 'acme-api',
      pipeline: 'bugfix',
      prompt: 'guard it',
      depends_on: [],
      budget: { wall_clock_seconds: 1500, cost_usd: 1.5 },
      done_when: ['repro test passes'],
      outward: true,
    },
    {
      id: 'ship',
      title: 'Ship the fix',
      project: 'acme-api',
      pipeline: 'ship-it',
      prompt: 'ship it',
      depends_on: ['parse-guard'],
      budget: { wall_clock_seconds: 1200, cost_usd: 1.0 },
      done_when: ['PR opened'],
      outward: true,
    },
  ],
}

const SUMMARY: PartitionSummary = {
  id: 'part-abc123',
  tenant: 'default',
  status: 'proposed',
  source_kind: 'text',
  source_ref: 'docs/bug-1234.md',
  proposed_digest: 'sha256:deadbeef',
  ratified_digest: null,
  outward_consent: false,
  ratified_by: null,
  ratified_at: null,
  created_ts: '2026-07-27T10:00:00Z',
  updated_ts: '2026-07-27T10:00:00Z',
}

const DETAIL: PartitionDetail = {
  ...SUMMARY,
  proposed_json: JSON.stringify(PLAN),
  ratified_json: null,
  ratified_diff: null,
  outward_actions: ['forge_pr', 'git_push'],
  total_cost_usd: 2.5,
  waves: [['parse-guard'], ['ship']],
  parallelism: {
    requested: 3,
    effective: 1,
    concurrency_limit: 8,
    runner_cap: 1,
    runner_kinds: ['claude'],
    notes: ["runner_throttle caps 'claude' at claude_max_concurrency=1"],
  },
  tasks: [],
}

function preview(overrides: Partial<PartitionPreview> = {}): PartitionPreview {
  return {
    ok: true,
    code: null,
    status_code: null,
    detail: null,
    outward_actions: ['forge_pr', 'git_push'],
    total_cost_usd: 2.5,
    waves: [['parse-guard'], ['ship']],
    task_ids: ['parse-guard', 'ship'],
    parallelism: DETAIL.parallelism,
    ...overrides,
  }
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

function setTextareaValue(element: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!
  setter.call(element, value)
  element.dispatchEvent(new Event('input', { bubbles: true }))
}

function setInputValue(element: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
  setter.call(element, value)
  element.dispatchEvent(new Event('input', { bubbles: true }))
}

/** React implements `onBlur` on top of the bubbling `focusout` event (a bare
 * `blur` does not bubble and never reaches React's root listener), so a test
 * that dispatches `blur` silently asserts nothing. */
function blurInput(element: HTMLInputElement) {
  element.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
}

let container: HTMLDivElement
let root: Root

function mount() {
  act(() => {
    root.render(<PartitionsView />)
  })
}

/** Resolves the mounted promise chain. Two microtask flushes: one for the
 * list, one for whatever the drawer/preview kicked off in response. */
async function settle() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

async function openDrawer() {
  await settle()
  const review = container.querySelector(
    'button[aria-label="Review partition part-abc123"]',
  ) as HTMLButtonElement
  await act(async () => {
    review.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await Promise.resolve()
  })
  await settle()
}

function editor(): HTMLTextAreaElement {
  return document.querySelector('[data-testid="partition-plan-editor"]') as HTMLTextAreaElement
}

function dispatchButton(): HTMLButtonElement {
  return document.querySelector(
    'button[aria-label="Ratify and dispatch partition part-abc123"]',
  ) as HTMLButtonElement
}

function consentCheckbox(): HTMLInputElement {
  return document.querySelector(
    'input[type="checkbox"][aria-label="I consent to these outward-visible actions"]',
  ) as HTMLInputElement
}

beforeEach(() => {
  fetchPartitions.mockReset()
  fetchPartition.mockReset()
  previewPartition.mockReset()
  ratifyPartition.mockReset()
  useRoleMock.mockReset()
  window.localStorage.clear()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
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
  vi.useRealTimers()
})

describe('PartitionsView — list', () => {
  it('shows a loading indicator before the list resolves', () => {
    fetchPartitions.mockReturnValue(new Promise(() => {}))
    mockRole('approve', 2)
    mount()
    expect(container.querySelector('[role="status"]')).not.toBeNull()
  })

  it('renders an empty state that says what would fill it, not just "nothing"', async () => {
    fetchPartitions.mockResolvedValue([])
    mockRole('approve', 2)
    mount()
    await settle()

    const empty = container.querySelector('[data-testid="partitions-empty"]')
    expect(empty).not.toBeNull()
    // EmptyState's `body` is mandatory by design; assert the operator is told
    // how a partition gets here rather than being shown a blank panel.
    expect(empty!.textContent).toMatch(/hivepilot partition submit/i)
  })

  it('CRITICAL: hides the Review control when the caller ranks below approve', async () => {
    fetchPartitions.mockResolvedValue([SUMMARY])
    mockRole('run', 1)
    mount()
    await settle()

    expect(container.textContent).toContain('part-abc123')
    expect(
      container.querySelector('button[aria-label="Review partition part-abc123"]'),
    ).toBeNull()
    expect(container.textContent).toMatch(/read-only/i)
  })

  it('shows the Review control for an approve-rank token on a proposed partition', async () => {
    fetchPartitions.mockResolvedValue([SUMMARY])
    mockRole('approve', 2)
    mount()
    await settle()

    expect(
      container.querySelector('button[aria-label="Review partition part-abc123"]'),
    ).not.toBeNull()
  })

  it('never offers Review for a partition that is no longer proposed', async () => {
    fetchPartitions.mockResolvedValue([{ ...SUMMARY, status: 'ratified' }])
    mockRole('approve', 2)
    mount()
    await settle()

    expect(
      container.querySelector('button[aria-label="Review partition part-abc123"]'),
    ).toBeNull()
  })

  it('CRITICAL: a 403 on the list degrades gracefully via ApiForbiddenError', async () => {
    const { ApiForbiddenError } = await import('@/lib/api')
    fetchPartitions.mockRejectedValue(new ApiForbiddenError())
    mockRole('read', 0)
    mount()
    await settle()

    expect(container.querySelector('[role="alert"]')).toBeNull()
    expect(container.querySelector('[data-testid="partitions-forbidden"]')).not.toBeNull()
  })

  it('renders an absent source as an em-dash, never a plausible-looking value', async () => {
    fetchPartitions.mockResolvedValue([{ ...SUMMARY, source_kind: null, source_ref: null }])
    mockRole('approve', 2)
    mount()
    await settle()

    const row = container.querySelector('[data-testid="partition-row-part-abc123"]')!
    expect(row.textContent).toContain('—')
  })
})

describe('PartitionsView — ratification drawer', () => {
  beforeEach(() => {
    fetchPartitions.mockResolvedValue([SUMMARY])
    fetchPartition.mockResolvedValue(DETAIL)
    previewPartition.mockResolvedValue(preview())
    mockRole('approve', 2)
  })

  it('CRITICAL: prefills the editor with the proposed plan', async () => {
    mount()
    await openDrawer()

    const text = editor().value
    expect(JSON.parse(text)).toEqual(PLAN)
    expect(text).toContain('parse-guard')
  })

  it('CRITICAL: shows a client-side parse error BEFORE submit and blocks dispatch', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mount()
    await openDrawer()

    await act(async () => {
      setTextareaValue(editor(), '{ "tasks": [ ')
      await Promise.resolve()
    })

    const parseError = document.querySelector('[data-testid="partition-parse-error"]')
    expect(parseError).not.toBeNull()
    expect(dispatchButton().disabled).toBe(true)

    // A disabled button never fires onClick, but assert the POST regardless:
    // the guard that matters is "nothing was submitted", not "the button
    // looked disabled".
    await act(async () => {
      dispatchButton().dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    expect(ratifyPartition).not.toHaveBeenCalled()

    // The gate is never asked about a document that isn't JSON.
    previewPartition.mockClear()
    await act(async () => {
      vi.advanceTimersByTime(PREVIEW_DEBOUNCE_MS + 10)
      await Promise.resolve()
    })
    expect(previewPartition).not.toHaveBeenCalled()
  })

  it('CRITICAL: dispatch stays disabled when the GATE refuses, even though the JSON parses', async () => {
    previewPartition.mockResolvedValue(
      preview({
        ok: false,
        code: 'policy_denied',
        status_code: 403,
        detail: "task 'ship' wall_clock_seconds=1200 exceeds max_task_wall_clock_seconds=600",
      }),
    )
    mount()
    await openDrawer()

    expect(document.querySelector('[data-testid="partition-parse-error"]')).toBeNull()
    expect(dispatchButton().disabled).toBe(true)
    // The refusal is the gate's own message, verbatim — never re-worded here.
    expect(document.querySelector('[data-testid="partition-gate-refusal"]')!.textContent).toContain(
      'max_task_wall_clock_seconds=600',
    )
  })

  it('enables dispatch once the gate accepts the plan', async () => {
    mount()
    await openDrawer()

    expect(document.querySelector('[data-testid="partition-gate-accepted"]')).not.toBeNull()
    expect(dispatchButton().disabled).toBe(false)
  })
})

describe('PartitionsView — outward consent', () => {
  beforeEach(() => {
    fetchPartitions.mockResolvedValue([SUMMARY])
    fetchPartition.mockResolvedValue(DETAIL)
    mockRole('approve', 2)
  })

  it('CRITICAL: the consent checkbox is separate from ratification and defaults UNCHECKED', async () => {
    previewPartition.mockResolvedValue(preview({ ok: false, code: 'consent_required' }))
    mount()
    await openDrawer()

    const checkbox = consentCheckbox()
    expect(checkbox).not.toBeNull()
    expect(checkbox.checked).toBe(false)
    // First preview call carries `false` — an unticked box is what the gate
    // is asked about, not an optimistic `true`.
    expect(previewPartition).toHaveBeenCalledWith('part-abc123', expect.any(String), false)
  })

  it('CRITICAL: the warning names the actions the BACKEND computed, not a hardcoded list', async () => {
    previewPartition.mockResolvedValue(
      preview({ outward_actions: ['forge_release', 'notify'], ok: false, code: 'consent_required' }),
    )
    mount()
    await openDrawer()

    const warning = document.querySelector('[data-testid="partitions-outward-warning"]')!
    expect(warning.textContent).toContain('releases published')
    expect(warning.textContent).toContain('notifications sent out')
    // The git/PR wording must NOT appear: this plan's footprint doesn't
    // include it, so a hardcoded sentence would be caught here.
    expect(warning.textContent).not.toContain('branches will be pushed')
    expect(warning.textContent).toContain('an action visible outside this machine')
  })

  it('renders the exact spec warning for a git_push + forge_pr footprint', async () => {
    previewPartition.mockResolvedValue(
      preview({ outward_actions: ['forge_pr', 'git_push'], ok: false, code: 'consent_required' }),
    )
    mount()
    await openDrawer()

    // Sorted server-side, so the sentence reads in the backend's order.
    expect(document.querySelector('[data-testid="partitions-outward-warning"]')!.textContent).toBe(
      'PRs opened and branches will be pushed — an action visible outside this machine.',
    )
  })

  it('renders an outward token it does not know VERBATIM instead of dropping it', async () => {
    previewPartition.mockResolvedValue(
      preview({ outward_actions: ['telepathy'], ok: false, code: 'consent_required' }),
    )
    mount()
    await openDrawer()

    expect(document.querySelector('[data-testid="partitions-outward-warning"]')!.textContent).toContain(
      'telepathy',
    )
  })

  it('declares the v1 runtime gap for tokens consent does not actually suppress yet', async () => {
    previewPartition.mockResolvedValue(preview({ outward_actions: ['vault_write'] }))
    mount()
    await openDrawer()

    expect(document.querySelector('[data-testid="partitions-outward-gap"]')).not.toBeNull()
  })

  it('offers no checkbox at all when the computed footprint is empty', async () => {
    previewPartition.mockResolvedValue(preview({ outward_actions: [] }))
    mount()
    await openDrawer()

    expect(consentCheckbox()).toBeNull()
    expect(document.querySelector('[data-testid="partitions-outward-none"]')).not.toBeNull()
  })

  it('CRITICAL: ticking consent re-asks the GATE rather than deciding locally', async () => {
    previewPartition.mockResolvedValue(preview({ ok: false, code: 'consent_required' }))
    mount()
    await openDrawer()

    previewPartition.mockResolvedValue(preview({ ok: true }))
    await act(async () => {
      consentCheckbox().click()
      await Promise.resolve()
    })
    await settle()

    expect(previewPartition).toHaveBeenLastCalledWith('part-abc123', expect.any(String), true)
    expect(dispatchButton().disabled).toBe(false)
  })
})

describe('PartitionsView — typed controls', () => {
  beforeEach(() => {
    fetchPartitions.mockResolvedValue([SUMMARY])
    fetchPartition.mockResolvedValue(DETAIL)
    previewPartition.mockResolvedValue(preview())
    mockRole('approve', 2)
  })

  it('CRITICAL: dropping a task rewrites the raw document — the box stays authoritative', async () => {
    mount()
    await openDrawer()

    const drop = document.querySelector(
      'button[aria-label="Drop task ship from this plan"]',
    ) as HTMLButtonElement
    await act(async () => {
      drop.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    const plan = JSON.parse(editor().value) as typeof PLAN
    expect(plan.tasks.map((task) => task.id)).toEqual(['parse-guard'])
    // Everything else survives untouched — dropping a task must not silently
    // rewrite the rest of the operator's plan.
    expect(plan.policy).toEqual(PLAN.policy)
    expect(plan.source).toEqual(PLAN.source)
  })

  it('CRITICAL: editing a budget rewrites that task only, in the raw document', async () => {
    mount()
    await openDrawer()

    const field = document.querySelector(
      'input[aria-label="Cost ceiling in US dollars for task parse-guard"]',
    ) as HTMLInputElement
    await act(async () => {
      setInputValue(field, '0.25')
      blurInput(field)
      await Promise.resolve()
    })

    const plan = JSON.parse(editor().value) as typeof PLAN
    expect(plan.tasks[0].budget.cost_usd).toBe(0.25)
    expect(plan.tasks[0].budget.wall_clock_seconds).toBe(1500)
    expect(plan.tasks[1].budget).toEqual(PLAN.tasks[1].budget)
  })

  it('writes null for an emptied budget so the GATE reports it, never a substituted default', async () => {
    mount()
    await openDrawer()

    const field = document.querySelector(
      'input[aria-label="Wall-clock ceiling in seconds for task parse-guard"]',
    ) as HTMLInputElement
    await act(async () => {
      setInputValue(field, '')
      blurInput(field)
      await Promise.resolve()
    })

    const plan = JSON.parse(editor().value) as Record<string, any>
    expect(plan.tasks[0].budget.wall_clock_seconds).toBeNull()
  })

  it('labels wall-clock as an enforcement ceiling, never as an estimate', async () => {
    mount()
    await openDrawer()

    const task = document.querySelector('[data-testid="partition-task-parse-guard"]')!
    // 1500s renders through the shared formatter as "25m".
    expect(task.textContent).toContain('Killed after 25m')
    expect(task.textContent).not.toMatch(/estimat/i)
  })
})

describe('PartitionsView — effective parallelism', () => {
  beforeEach(() => {
    fetchPartitions.mockResolvedValue([SUMMARY])
    fetchPartition.mockResolvedValue(DETAIL)
    mockRole('approve', 2)
  })

  it('CRITICAL: shows the EFFECTIVE number alongside the requested one', async () => {
    previewPartition.mockResolvedValue(preview())
    mount()
    await openDrawer()

    const readout = document.querySelector('[data-testid="partitions-parallelism"]')!
    expect(readout.textContent).toContain('Effective parallelism')
    expect(readout.textContent).toContain('1')
    expect(readout.textContent).toContain('3 requested')
    // The backend's explanation, verbatim — this view never re-words it.
    expect(
      document.querySelector('[data-testid="partitions-parallelism-notes"]')!.textContent,
    ).toContain('claude_max_concurrency=1')
  })

  it('renders an em-dash when parallelism could not be computed, never a plausible 1', async () => {
    previewPartition.mockResolvedValue(preview({ parallelism: null }))
    mount()
    await openDrawer()

    const readout = document.querySelector('[data-testid="partitions-parallelism"]')!
    expect(readout.textContent).toContain('—')
    expect(readout.textContent).not.toContain('requested')
  })
})

describe('PartitionsView — dispatch', () => {
  beforeEach(() => {
    fetchPartitions.mockResolvedValue([SUMMARY])
    fetchPartition.mockResolvedValue(DETAIL)
    previewPartition.mockResolvedValue(preview())
    mockRole('approve', 2)
  })

  it('CRITICAL: confirms before dispatching, and a dismissed confirm submits nothing', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    mount()
    await openDrawer()

    await act(async () => {
      dispatchButton().dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    expect(window.confirm).toHaveBeenCalled()
    expect(ratifyPartition).not.toHaveBeenCalled()
  })

  it('submits the edited plan, the consent flag and the expected digest', async () => {
    ratifyPartition.mockResolvedValue({
      partition_id: 'part-abc123',
      status: 'ratified',
      ratified_digest: 'sha256:new',
      outward_actions: ['forge_pr', 'git_push'],
      outward_consent: true,
      task_ids: ['parse-guard', 'ship'],
      diff: '',
      warnings: [],
      idempotent: false,
      dispatching: true,
      parallelism: DETAIL.parallelism,
    })
    mount()
    await openDrawer()

    await act(async () => {
      consentCheckbox().click()
      await Promise.resolve()
    })
    await settle()
    await act(async () => {
      dispatchButton().dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    await settle()

    expect(ratifyPartition).toHaveBeenCalledTimes(1)
    const [id, body] = ratifyPartition.mock.calls[0] as [string, Record<string, unknown>]
    expect(id).toBe('part-abc123')
    expect(body.outward_consent).toBe(true)
    // The digest the SERVER handed us — this is what makes a stale tab a 409
    // instead of a second dispatch.
    expect(body.expected_digest).toBe('sha256:deadbeef')
    expect(JSON.parse(String(body.partition_json))).toEqual(PLAN)
  })

  it('CRITICAL: disables the control and shows "Processing…" while in flight', async () => {
    ratifyPartition.mockReturnValue(new Promise(() => {}))
    mount()
    await openDrawer()

    await act(async () => {
      dispatchButton().dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    expect(dispatchButton().disabled).toBe(true)
    expect(dispatchButton().textContent).toMatch(/processing/i)
    expect(editor().disabled).toBe(true)
  })

  it('CRITICAL: a 403 on ratify degrades gracefully via ApiForbiddenError', async () => {
    const { ApiForbiddenError } = await import('@/lib/api')
    ratifyPartition.mockRejectedValue(new ApiForbiddenError())
    mount()
    await openDrawer()

    await act(async () => {
      dispatchButton().dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    await settle()

    const alerts = Array.from(document.querySelectorAll('[role="alert"]'))
    expect(alerts.some((node) => /approve-rank token/i.test(node.textContent ?? ''))).toBe(true)
  })

  it('translates a 409 into the stale-plan message rather than re-deriving the rule', async () => {
    const { ApiError } = await import('@/lib/api')
    ratifyPartition.mockRejectedValue(new ApiError(409, 'partition has moved on since load'))
    mount()
    await openDrawer()

    await act(async () => {
      dispatchButton().dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    await settle()

    const alerts = Array.from(document.querySelectorAll('[role="alert"]'))
    expect(alerts.some((node) => /changed since you opened it/i.test(node.textContent ?? ''))).toBe(
      true,
    )
    // The server's own words survive alongside the translation.
    expect(alerts.some((node) => /moved on since load/i.test(node.textContent ?? ''))).toBe(true)
  })

  it('reports an idempotent second ratify as a no-op, never as a fresh dispatch', async () => {
    ratifyPartition.mockResolvedValue({
      partition_id: 'part-abc123',
      status: 'ratified',
      ratified_digest: 'sha256:new',
      outward_actions: [],
      outward_consent: false,
      task_ids: ['parse-guard', 'ship'],
      diff: '',
      warnings: [],
      idempotent: true,
      dispatching: false,
      parallelism: null,
    })
    mount()
    await openDrawer()

    await act(async () => {
      dispatchButton().dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    await settle()

    expect(document.body.textContent).toMatch(/already ratified/i)
    expect(document.body.textContent).not.toMatch(/queued/i)
  })
})

describe('PartitionsView — i18n', () => {
  it('en and fr declare exactly the same partitions.* keys', () => {
    const keysOf = (dict: Record<string, string>) =>
      Object.keys(dict)
        .filter((key) => key.startsWith('partitions.'))
        .sort()

    const enKeys = keysOf(en)
    expect(enKeys.length).toBeGreaterThan(0)
    expect(keysOf(fr as unknown as Record<string, string>)).toEqual(enKeys)
    expect(Object.keys(fr)).toContain('nav.partitions')
  })

  it('writes real French copy, not the English string echoed back', () => {
    const sample = [
      'partitions.description',
      'partitions.outwardWarning',
      'partitions.outwardConsentLabel',
      'partitions.dispatch',
      'partitions.wallClockLabel',
    ] as const
    for (const key of sample) {
      expect(fr[key], key).not.toBe(en[key])
      expect(fr[key].length, key).toBeGreaterThan(0)
    }
    expect(fr['partitions.parallelismLabel']).toBe('Parallélisme effectif')
  })

  it('renders the drawer in French when the language is fr', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    fetchPartitions.mockResolvedValue([SUMMARY])
    fetchPartition.mockResolvedValue(DETAIL)
    previewPartition.mockResolvedValue(preview({ ok: false, code: 'consent_required' }))
    mockRole('approve', 2)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <PartitionsView />
        </LanguageProvider>,
      )
      await Promise.resolve()
    })
    await settle()

    const review = container.querySelector(
      'button[aria-label="Relire la partition part-abc123"]',
    ) as HTMLButtonElement
    expect(review).not.toBeNull()
    await act(async () => {
      review.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    await settle()

    expect(document.body.textContent).toContain('Ratifier et lancer')
    expect(document.body.textContent).toContain('Parallélisme effectif')
    expect(
      document.querySelector('[data-testid="partitions-outward-warning"]')!.textContent,
    ).toContain('une action visible en dehors de cette machine')
  })
})

describe('PartitionsView — mobile safety', () => {
  beforeEach(() => {
    fetchPartitions.mockResolvedValue([SUMMARY])
    fetchPartition.mockResolvedValue(DETAIL)
    previewPartition.mockResolvedValue(preview())
    mockRole('approve', 2)
  })

  // Measured at 390px: an id like `part-abc123` and a long refusal message are
  // the two strings that overflowed. Everything that can hold an unbroken run
  // of characters must be allowed to break, and nothing may introduce its own
  // horizontal scroll container — the body must never scroll sideways.
  it('lets long unbroken strings wrap instead of widening the page', async () => {
    mount()
    await settle()

    const row = container.querySelector('[data-testid="partition-row-part-abc123"]')!
    expect(row.querySelector('.break-all, .break-words')).not.toBeNull()
    expect(container.querySelector('.overflow-x-auto')).toBeNull()
  })

  it('gives the primary controls a 44px tap target', async () => {
    mount()
    await openDrawer()

    expect(dispatchButton().className).toMatch(/touch-target/)
    expect(
      (
        document.querySelector(
          'button[aria-label="Drop task ship from this plan"]',
        ) as HTMLButtonElement
      ).className,
    ).toMatch(/touch-target/)
    expect(
      (
        document.querySelector(
          'input[aria-label="Cost ceiling in US dollars for task parse-guard"]',
        ) as HTMLInputElement
      ).className,
    ).toMatch(/touch-target/)
  })

  it('keeps the budget controls stacked below sm so they never sit side by side on a phone', async () => {
    mount()
    await openDrawer()

    const grid = document
      .querySelector('[data-testid="partition-task-parse-guard"]')!
      .querySelector('.grid')!
    expect(grid.className).toContain('grid-cols-1')
    expect(grid.className).toContain('sm:grid-cols-2')
  })
})
