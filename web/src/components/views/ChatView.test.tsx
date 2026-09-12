import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { ConciergeDecision } from '@/lib/pollen-api'

const { askConcierge, speakReply, postApproval, createRun, useRoleMock } = vi.hoisted(() => ({
  askConcierge: vi.fn(),
  speakReply: vi.fn(),
  postApproval: vi.fn(),
  createRun: vi.fn(),
  useRoleMock: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, askConcierge, postApproval, createRun }
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
  createRun.mockReset().mockResolvedValue({ run_id: 9, status: 'running' })
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

  it('does not put confirm buttons on an ANSWER', async () => {
    askConcierge.mockResolvedValue(answer('Run 8 succeeded.'))
    render()
    type('how did the last run go?')
    await send()
    expect(container.querySelector('[data-testid="inbox-intent-card"]')).toBeNull()
    expect(container.querySelector('[data-testid="inbox-intent-confirm"]')).toBeNull()
    expect(container.querySelector('[data-testid="inbox-intent-cancel"]')).toBeNull()
    expect(createRun).not.toHaveBeenCalled()
    expect(postApproval).not.toHaveBeenCalled()
  })

  it('surfaces a ROUTE as a confirm card and does not execute until Confirm', async () => {
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

    const card = container.querySelector('[data-testid="inbox-intent-card"]')
    expect(card).not.toBeNull()
    expect(card?.textContent).toContain('developer')
    expect(card?.textContent).toContain('example-api')
    expect(card?.textContent).toMatch(/intent: ROUTE/)
    expect(container.querySelector('[data-testid="inbox-intent-confirm"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="inbox-intent-cancel"]')).not.toBeNull()
    expect(createRun).not.toHaveBeenCalled()
    expect(postApproval).not.toHaveBeenCalled()
  })

  it('cancels a ROUTE without calling any execute API', async () => {
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
    await act(async () => {
      ;(container.querySelector('[data-testid="inbox-intent-cancel"]') as HTMLButtonElement).click()
      await Promise.resolve()
    })
    expect(createRun).not.toHaveBeenCalled()
    expect(postApproval).not.toHaveBeenCalled()
    expect(container.querySelector('[data-testid="inbox-intent-done"]')?.textContent).toMatch(/Cancelled/)
    expect(container.querySelector('[data-testid="inbox-intent-confirm"]')).toBeNull()
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

  it('renders an ACTION confirm card and posts approve only after Confirmer', async () => {
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
    const card = container.querySelector('[data-testid="inbox-intent-card"]')
    expect(card).not.toBeNull()
    expect(card?.getAttribute('data-intent')).toBe('ACTION')
    expect(card?.textContent).toMatch(/intent: ACTION/)
    expect(postApproval).not.toHaveBeenCalled()
    await act(async () => {
      ;(container.querySelector('[data-testid="inbox-intent-confirm"]') as HTMLButtonElement).click()
      await Promise.resolve()
    })
    expect(postApproval).toHaveBeenCalledWith(42, { approve: true, reason: undefined })
    expect(container.querySelector('[data-testid="inbox-intent-done"]')?.textContent).toMatch(/42/)
  })

  it('triggers createRun only after Confirmer on an ACTION run', async () => {
    askConcierge.mockResolvedValue({
      kind: 'action',
      answer_text: null,
      role_key: null,
      target: 'noxxy',
      order: null,
      action: 'run',
      params: { task: 'docs', extra_prompt: 'add a healthcheck' },
      destructive: true,
      dispatches: [],
    })
    render()
    type('run docs on noxxy')
    await send()
    expect(createRun).not.toHaveBeenCalled()
    await act(async () => {
      ;(container.querySelector('[data-testid="inbox-intent-confirm"]') as HTMLButtonElement).click()
      await Promise.resolve()
    })
    expect(createRun).toHaveBeenCalledWith({
      task: 'docs',
      project: 'noxxy',
      extra_prompt: 'add a healthcheck',
      auto_git: true,
    })
  })

  it('does not execute a pipeline ACTION until Confirmer, and Confirmer still does not invent a pipeline API', async () => {
    askConcierge.mockResolvedValue({
      kind: 'action',
      answer_text: null,
      role_key: null,
      target: 'noxys',
      order: null,
      action: 'run_pipeline',
      params: { pipeline: 'noxys', branch: 'staging' },
      destructive: true,
      dispatches: [],
    })
    render()
    type('lance le pipeline noxys sur staging')
    await send()
    const card = container.querySelector('[data-testid="inbox-intent-card"]')
    expect(card?.textContent).toMatch(/pipeline noxys/)
    expect(card?.textContent).toMatch(/staging/)
    expect(createRun).not.toHaveBeenCalled()
    await act(async () => {
      ;(container.querySelector('[data-testid="inbox-intent-confirm"]') as HTMLButtonElement).click()
      await Promise.resolve()
    })
    expect(createRun).not.toHaveBeenCalled()
    expect(postApproval).not.toHaveBeenCalled()
    expect(container.querySelector('[data-testid="inbox-intent-done"]')).not.toBeNull()
  })
})
