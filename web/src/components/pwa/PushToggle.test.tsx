import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'

const { fetchPushConfig } = vi.hoisted(() => ({ fetchPushConfig: vi.fn() }))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchPushConfig, subscribePush: vi.fn(), unsubscribePush: vi.fn() }
})

import { PushToggle } from './PushToggle'

let container: HTMLDivElement
let root: Root

async function mount() {
  await act(async () => {
    root.render(
      <LanguageProvider>
        <PushToggle />
      </LanguageProvider>,
    )
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('PushToggle', () => {
  beforeEach(() => {
    fetchPushConfig.mockReset()
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
  })

  it('hides when the server has no VAPID keys', async () => {
    fetchPushConfig.mockResolvedValue({ enabled: false, vapid_public_key: null })
    await mount()
    expect(container.querySelector('[data-testid="pwa-push"]')).toBeNull()
  })

  it('shows the bell when push is configured', async () => {
    Object.defineProperty(navigator, 'serviceWorker', {
      configurable: true,
      value: {
        ready: Promise.resolve({
          pushManager: { getSubscription: async () => null },
        }),
      },
    })
    Object.defineProperty(window, 'PushManager', {
      configurable: true,
      value: function PushManager() {},
    })
    fetchPushConfig.mockResolvedValue({
      enabled: true,
      vapid_public_key: 'BK' + 'A'.repeat(80),
    })
    await mount()
    expect(container.querySelector('[data-testid="pwa-push"]')).not.toBeNull()
  })
})
