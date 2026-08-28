import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import type { ConciergeDecision } from '@/lib/pollen-api'

const { askConcierge } = vi.hoisted(() => ({ askConcierge: vi.fn() }))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, askConcierge }
})

import { ChatView } from './ChatView'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  askConcierge.mockReset()
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
  // Astryx TextArea forwards to a real <textarea> (HP-23 POC).
  const input = container.querySelector('textarea') as HTMLTextAreaElement
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype,
    'value',
  )!.set!
  act(() => {
    setter.call(input, text)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

function sendButton(): HTMLButtonElement {
  const buttons = Array.from(container.querySelectorAll('button')) as HTMLButtonElement[]
  return buttons.find((b) => (b.textContent ?? '').includes('Send')) ?? buttons[buttons.length - 1]
}

async function send() {
  await act(async () => {
    sendButton().dispatchEvent(new MouseEvent('click', { bubbles: true }))
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

    expect(container.textContent).toContain('how did the last run go?')
    expect(container.textContent).toContain('Run 8 succeeded.')
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

  it('shows an error bubble when the concierge is unreachable', async () => {
    askConcierge.mockRejectedValue(new Error('boom'))
    render()
    type('hello')
    await send()

    expect(container.querySelector('[data-testid="chat-message-error"]')).not.toBeNull()
  })
})
