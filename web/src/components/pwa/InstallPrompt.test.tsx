import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'
import { InstallPrompt } from './InstallPrompt'

let container: HTMLDivElement
let root: Root

describe('InstallPrompt', () => {
  beforeEach(() => {
    window.localStorage.clear()
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
  })

  it('stays hidden until beforeinstallprompt fires', async () => {
    await act(async () => {
      root.render(
        <LanguageProvider>
          <InstallPrompt />
        </LanguageProvider>,
      )
      await Promise.resolve()
    })
    expect(container.querySelector('[data-testid="pwa-install"]')).toBeNull()

    await act(async () => {
      const event = new Event('beforeinstallprompt', { cancelable: true })
      Object.assign(event, {
        prompt: async () => undefined,
        userChoice: Promise.resolve({ outcome: 'dismissed' }),
      })
      event.preventDefault()
      window.dispatchEvent(event)
      await Promise.resolve()
    })
    expect(container.querySelector('[data-testid="pwa-install"]')).not.toBeNull()
  })
})
