import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, askConcierge: vi.fn() }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return {
    ...actual,
    useRole: () => ({
      role: 'admin',
      can: (needed: string) => ['read', 'run', 'approve', 'admin'].includes(needed),
    }),
  }
})

import { InboxView } from './InboxView'

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

describe('InboxView', () => {
  it('renders Inbox chrome around the existing concierge chat, without ACTION/KPI chrome', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <InboxView />
        </LanguageProvider>,
      )
    })
    const page = container.querySelector('[data-testid="inbox-page"]')
    expect(page).not.toBeNull()
    expect(page?.textContent).toContain('Inbox')
    expect(page?.textContent).toMatch(/⌘K/)
    expect(container.querySelector('[data-testid="chat-input"]')).not.toBeNull()
    expect(container.textContent).not.toMatch(/need you/i)
    expect(container.textContent).not.toMatch(/What's new/i)
  })
})
