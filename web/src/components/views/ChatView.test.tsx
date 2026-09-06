import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { ConciergeDecision } from '@/lib/pollen-api'

const { askConcierge, speakReply, postApproval, useRoleMock } = vi.hoisted(() => ({
  askConcierge: vi.fn(),
  speakReply: vi.fn(),
  postApproval: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, askConcierge, postApproval }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

vi.mock('@/lib/voice-reply', () => ({ speakReply }))

import { ChatView } from './ChatView'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  askConcierge.mockReset()
  speakReply.mockReset()
  postApproval.mockReset().mockResolvedValue({ result: { success: true } })
  useRoleMock.mockReturnValue({
    role: 'admin',
    can: (needed: string) => ['read', 'run', 'approve', 'admin'].includes(needed),
  })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
})

function render() {
  act(() =>
    root.render(
      <LanguageProvider>
        <ChatView />
      </LanguageProvider>,
    ),
  )
}

function type(text: string) {
  const input = container.querySelector('[data-testid="chat-input"]') as HTMLTextAreaElement
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype,
    'value',
  )!.set!
  act(() => {
    setter.call(input, text)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

async function send() {
  const button = container.querySelector('[data-testid="chat-send"]') as HTMLButtonElement
  await act(async () => {
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  })
  await act(async () => {})
}

const answer = (text: string): ConciergeDecision => ({
  kind: 'answer',
  answer_text: text,
  role_key: null,
  target: null,
  order: null,
  action: null,
  params: null,
  destructive: false,
  dispatches: [],
})

describe('ChatView', () => {
  it('shows the empty prompt before any message', () => {
    render()
    expect(container.textContent).toContain('concierge')
  })

  it('renders the user message and the concierge answer', async () => {
    askConcierge.mockResolvedValue(answer('Run 8 succeeded.'))
    render()
    type('how did the last run go?')
    await send()

    const user = container.querySelector('[data-testid="chat-message-user"]')
    const bot = container.querySelector('[data-testid="chat-message-concierge"]')
    expect(user?.textContent).toContain('how did the last run go?')
    expect(bot?.textContent).toContain('Run 8 succeeded.')
    expect(askConcierge).toHaveBeenCalledOnce()
  })

  it('surfaces a route decision as a proposal card, not an executed action', async () => {
    askConcierge.mockResolvedValue({
      kind: 'route',
      answer_text: null,
      role_key: 'developer',
      target: 'example-api',
      order: 'add a healthcheck',
      action: null,
      params: null,
      destructive: true,
      dispatches: [],
    })
    render()
    type('ask the dev to add a healthcheck')
    await send()

    const proposal = container.querySelector('[data-testid="chat-proposal"]')
    expect(proposal).not.toBeNull()
    expect(proposal?.textContent).toContain('developer')
    expect(proposal?.textContent).toContain('example-api')
  })

  it('speaks the concierge answer when a call is in progress', async () => {
    window.localStorage.setItem('hivepilot.webui.voice-call', 'true')
    askConcierge.mockResolvedValue(answer('Run 8 succeeded.'))
    render()
    type('status?')
    await send()
    expect(speakReply).toHaveBeenCalledWith('Run 8 succeeded.', 'en-US')
  })

  it('shows an error bubble when the concierge is unreachable', async () => {
    askConcierge.mockRejectedValue(new Error('boom'))
    render()
    type('hello')
    await send()

    expect(container.querySelector('[data-testid="chat-message-error"]')).not.toBeNull()
  })

  it('renders an inline approval card and posts approve for a concierge approve action', async () => {
    askConcierge.mockResolvedValue({
      kind: 'action',
      answer_text: 'Approve run 42?',
      role_key: null,
      target: null,
      order: null,
      action: 'approve',
      params: { run_id: 42 },
      destructive: true,
      dispatches: [],
    })
    render()
    type('approve run 42')
    await send()
    expect(container.querySelector('[data-testid="chat-approval-card"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="chat-proposal"]')).toBeNull()
    await act(async () => {
      ;(container.querySelector('[data-testid="chat-approval-approve"]') as HTMLButtonElement).click()
      await Promise.resolve()
    })
    expect(postApproval).toHaveBeenCalledWith(42, { approve: true, reason: undefined })
    expect(container.querySelector('[data-testid="chat-approval-done"]')?.textContent).toMatch(/42/)
  })
})
