import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import { AlertsView } from './AlertsView'

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

describe('AlertsView', () => {
  it('renders the stub door, not an alerts list', () => {
    act(() => {
      root.render(
        <LanguageProvider>
          <AlertsView />
        </LanguageProvider>,
      )
    })
    expect(container.querySelector('[data-testid="alerts-page"]')).not.toBeNull()
    expect(container.textContent).toContain('Alerts')
    expect(container.textContent).toMatch(/follow-up/i)
    expect(container.querySelectorAll('li')).toHaveLength(0)
  })
})
