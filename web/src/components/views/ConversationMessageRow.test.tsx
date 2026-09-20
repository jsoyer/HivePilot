import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import { ROLE_AVATAR_COLORS } from '@/lib/role-avatars'
import { INTERACTION_MESSAGES } from '@/test/fixtures/interactions'
import { ConversationFil, ConversationMessageRow, inferTargetRole } from './ConversationMessageRow'

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
  it('renders the actor→target fil in order with kit role colours', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <ConversationFil messages={INTERACTION_MESSAGES} />
        </LanguageProvider>,
      )
    })

    const turns = container.querySelectorAll('[data-testid="conversation-fil"] > [data-testid^="message-"]')
    expect(Array.from(turns).map((el) => el.getAttribute('data-testid'))).toEqual([
      'message-101',
      'message-102',
      'message-103',
    ])
    expect(container.textContent).toContain('Aliénor')
    expect(container.textContent).toContain('Gustave')
    expect(container.textContent).toContain('Victor')
    expect(container.querySelector('[data-testid="role-badge-ceo"]')?.getAttribute('style')).toContain(
      '168, 85, 247',
    )
    expect(container.querySelector('[data-testid="role-badge-developer"]')?.getAttribute('style')).toContain(
      '34, 197, 94',
    )
    expect(ROLE_AVATAR_COLORS.ceo).toBe('#a855f7')
    expect(ROLE_AVATAR_COLORS.developer).toBe('#22c55e')
  })

  it('keeps every output collapsed until expanded', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <ConversationFil messages={INTERACTION_MESSAGES} />
        </LanguageProvider>,
      )
    })

    expect(container.querySelector('[data-testid="message-output-101"]')).toBeNull()
    expect(container.querySelector('[data-testid="message-output-102"]')).toBeNull()

    act(() => {
      container.querySelector('[data-testid="message-summary-102"]')?.dispatchEvent(
        new MouseEvent('click', { bubbles: true }),
      )
    })
    expect(container.querySelector('[data-testid="message-output-102"]')?.textContent).toContain(
      'Implemented mdstat',
    )
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

    expect(container.textContent).toContain('Hugo')
    expect(container.querySelector('[data-testid="role-badge-ciso"]')?.textContent).toContain('Hugo')
    expect(container.textContent).not.toContain('CEO')
  })

  it('CRITICAL: renders untrusted output as plain text, never as injected markup', () => {
    const malicious = '<img src=x onerror=alert(1)>'
    act(() => {
      root.render(
        <ConversationMessageRow message={{ ...INTERACTION_MESSAGES[1], body: malicious }} />,
      )
    })
    act(() => {
      container.querySelector('[data-testid="message-summary-102"]')?.dispatchEvent(
        new MouseEvent('click', { bubbles: true }),
      )
    })

    expect(container.textContent).toContain(malicious)
    expect(container.querySelector('img')).toBeNull()
  })
})

describe('inferTargetRole', () => {
  it('takes the next turn\'s role when that speaker is this target', () => {
    expect(inferTargetRole(INTERACTION_MESSAGES, 0)).toBe('developer')
    expect(inferTargetRole(INTERACTION_MESSAGES, 1)).toBe('reviewer')
    expect(inferTargetRole(INTERACTION_MESSAGES, 2)).toBeNull()
  })
})
