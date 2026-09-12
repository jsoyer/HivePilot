import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import { IssuesChip } from './IssuesChip'

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

function click(el: Element) {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

describe('IssuesChip', () => {
  it('renders nothing until ready (no All-clear flash)', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <IssuesChip count={0} ready={false} />
        </LanguageProvider>,
      )
    })
    expect(container.querySelector('[data-testid="issues-chip"]')).toBeNull()
  })

  it('shows All clear when the count is 0', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <IssuesChip count={0} ready />
        </LanguageProvider>,
      )
    })
    expect(container.textContent).toBe('All clear')
  })

  it('shows N issues when the count is positive', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <IssuesChip count={3} ready />
        </LanguageProvider>,
      )
    })
    expect(container.textContent).toBe('3 issues')
  })

  it('calls onClick when pressed', () => {
    const onClick = vi.fn()
    act(() => {
      root.render(
        <LanguageProvider>
          <IssuesChip count={2} ready onClick={onClick} />
        </LanguageProvider>,
      )
    })
    act(() => {
      click(container.querySelector('[data-testid="issues-chip"]') as HTMLElement)
    })
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})
