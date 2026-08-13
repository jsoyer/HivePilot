import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { ConversationRunsResponse, ConversationThread } from '@/lib/pollen-api'

const { fetchConversationRuns, fetchConversationThread, replyToRole } = vi.hoisted(() => ({
  fetchConversationRuns: vi.fn(),
  fetchConversationThread: vi.fn(),
  replyToRole: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchConversationRuns, fetchConversationThread, replyToRole }
})

import { ConversationsView } from './ConversationsView'

let container: HTMLDivElement
let root: Root

const RUNS: ConversationRunsResponse = {
  runs: [
    {
      run_id: 538,
      project: 'forage',
      started_at: '2026-08-13T20:05:34',
      message_count: 2,
      roles: ['developer', 'release_manager'],
    },
    {
      run_id: 521,
      project: 'ab-a',
      started_at: '2026-08-12T18:10:00',
      message_count: 10,
      roles: ['ceo', 'developer', 'reviewer'],
    },
  ],
}

const THREAD: ConversationThread = {
  run_id: 538,
  roles: ['developer', 'reviewer'],
  messages: [
    {
      interaction_id: 1,
      actor: 'Gustave (Developer)',
      role: 'developer',
      action: 'completed stage',
      body: 'Implemented mdstat with 38 tests.',
      at: '2026-08-13T20:06:00',
    },
    {
      interaction_id: 2,
      actor: 'Victor (Reviewer)',
      role: 'reviewer',
      action: 'completed stage',
      body: 'status: REQUEST_CHANGES\nThe grant path never checks isAdmin.',
      at: '2026-08-13T20:09:00',
    },
  ],
}

/** Set a controlled field's value the way React will notice.
 *
 * React installs its own value tracker on the DOM node, so assigning
 * `el.value` directly and dispatching `input` fires the event with the tracker
 * unchanged — React concludes nothing changed and never calls `onChange`. The
 * native setter updates the tracker too. */
function typeInto(el: HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const proto =
    el instanceof HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLSelectElement.prototype
  Object.getOwnPropertyDescriptor(proto, 'value')?.set?.call(el, value)
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }))
}

async function mount() {
  fetchConversationRuns.mockResolvedValue(RUNS)
  fetchConversationThread.mockResolvedValue(THREAD)
  await act(async () => {
    root.render(
      <LanguageProvider>
        <ConversationsView />
      </LanguageProvider>,
    )
    await Promise.resolve()
  })
}

describe('ConversationsView', () => {
  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    fetchConversationRuns.mockReset()
    fetchConversationThread.mockReset()
    replyToRole.mockReset()
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
    window.localStorage.clear()
  })

  it('lists the runs that carry messages', async () => {
    await mount()

    expect(container.textContent).toContain('538')
    expect(container.textContent).toContain('521')
    expect(container.textContent).toContain('forage')
  })

  it('shows each speaker by name and role', async () => {
    // The data has been in `interactions` all along; nothing ever showed it as
    // a conversation.
    await mount()

    expect(container.textContent).toContain('Gustave (Developer)')
    expect(container.textContent).toContain('Victor (Reviewer)')
  })

  it('shows what was actually said, not just that a stage completed', async () => {
    await mount()

    expect(container.textContent).toContain('never checks isAdmin')
  })

  it('opens the newest run without a click', async () => {
    // An empty right-hand pane on load reads as "there is nothing here".
    await mount()

    expect(fetchConversationThread).toHaveBeenCalledWith(538)
  })

  it('sends a reply to the role, not to the run', async () => {
    // Replying to a finished run would change nothing. The corrections file
    // for a role feeds that role's NEXT run.
    replyToRole.mockResolvedValue({ role: 'reviewer', written_to: '/x/reviewer.md' })
    await mount()

    const select = container.querySelector('select') as HTMLSelectElement
    const textarea = container.querySelector('textarea') as HTMLTextAreaElement
    expect(select).toBeTruthy()
    expect(textarea).toBeTruthy()

    await act(async () => {
      typeInto(select, 'reviewer')
      typeInto(textarea, 'Check isAdmin on every grant path.')
    })
    const button = Array.from(container.querySelectorAll('button')).find((b) =>
      /envoyer|send/i.test(b.textContent || ''),
    ) as HTMLButtonElement
    await act(async () => {
      button.click()
      await Promise.resolve()
    })

    expect(replyToRole).toHaveBeenCalledWith('reviewer', 'Check isAdmin on every grant path.')
  })

  it('refuses to send an empty reply', async () => {
    await mount()

    const button = Array.from(container.querySelectorAll('button')).find((b) =>
      /envoyer|send/i.test(b.textContent || ''),
    ) as HTMLButtonElement

    expect(button.disabled).toBe(true)
    expect(replyToRole).not.toHaveBeenCalled()
  })

  it('says plainly that a reply reaches the next run, not this one', async () => {
    // Otherwise it reads as a chat with a running agent, which it is not.
    //
    // Asserted against real prose, and against the KEY NAME being absent:
    // `t()` falls back to the key itself when a translation is missing, and
    // `conversations.replyReachesNextRun` contains the word "Next" — so the
    // obvious version of this test passed while the string was untranslated.
    await mount()

    expect(container.textContent).not.toContain('conversations.replyReachesNextRun')
    expect(container.textContent).toContain('does not reach the agents above')
  })
})
