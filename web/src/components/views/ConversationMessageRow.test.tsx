import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import { INTERACTION_MESSAGES } from '@/test/fixtures/interactions'
import { ConversationFil, ConversationMessageRow } from './ConversationMessageRow'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
})

describe('ConversationFil', () => {
  it('renders the actor→target fil in order with role attribution', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <ConversationFil messages={INTERACTION_MESSAGES} />
        </LanguageProvider>,
      )
    })

    const turns = container.querySelectorAll('[data-testid="conversation-fil"] > details')
    expect(Array.from(turns).map((el) => el.getAttribute('data-testid'))).toEqual([
      'message-101',
      'message-102',
      'message-103',
    ])
    expect(container.textContent).toContain('Aliénor (CEO)')
    expect(container.textContent).toContain('Gustave (Developer)')
    expect(container.textContent).toContain('Victor (Reviewer)')
    expect(container.textContent).toContain('ceo')
    expect(container.textContent).toContain('developer')
    expect(container.textContent).toContain('reviewer')
    expect(container.querySelector('[data-testid="conversation-fil"]')).not.toBeNull()
  })

  it('collapses later outputs and keeps the first turn open', () => {
    act(() => {
      root.render(<ConversationFil messages={INTERACTION_MESSAGES} />)
    })

    const first = container.querySelector('[data-testid="message-101"]') as HTMLDetailsElement
    const second = container.querySelector('[data-testid="message-102"]') as HTMLDetailsElement
    expect(first.open).toBe(true)
    expect(second.open).toBe(false)
    expect(container.querySelector('[data-testid="message-output-102"]')?.textContent).toContain(
      'Implemented mdstat',
    )

    act(() => {
      second.querySelector('summary')?.click()
    })
    expect(second.open).toBe(true)
  })

  it('says honestly when the run recorded no agent output', () => {
    act(() => {
      root.render(<ConversationFil messages={[]} />)
    })

    expect(container.textContent).toMatch(/no agent output/i)
    expect(container.querySelector('[data-testid="conversation-fil"]')).toBeNull()
  })
})

describe('ConversationMessageRow', () => {
  it('attributes the speaker via metadata.role, not by parsing the actor label', () => {
    act(() => {
      root.render(
        <ConversationMessageRow
          message={{
            ...INTERACTION_MESSAGES[0],
            actor: 'Hugo (CISO)',
            role: 'ciso',
          }}
        />,
      )
    })

    expect(container.textContent).toContain('Hugo (CISO)')
    expect(container.textContent).toContain('ciso')
    expect(container.textContent).not.toContain('CEO')
  })

  it('CRITICAL: renders untrusted output as plain text, never as injected markup', () => {
    const malicious = '<img src=x onerror=alert(1)>'
    act(() => {
      root.render(
        <ConversationMessageRow
          defaultOpen
          message={{ ...INTERACTION_MESSAGES[1], body: malicious }}
        />,
      )
    })

    expect(container.textContent).toContain(malicious)
    expect(container.querySelector('img')).toBeNull()
  })
})
