import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiForbiddenError } from '@/lib/api'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type { MemoriesResponse } from '@/lib/mirador-api'

const { fetchMemories } = vi.hoisted(() => ({ fetchMemories: vi.fn() }))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, fetchMemories }
})

import { Mem0View } from './Mem0View'

let container: HTMLDivElement
let root: Root

function mount() {
  act(() => {
    root.render(<Mem0View />)
  })
}

function setNativeValue(input: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
  nativeSetter.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

async function search(query: string) {
  const input = container.querySelector('input[aria-label="Search memories"]') as HTMLInputElement
  const form = container.querySelector('form') as HTMLFormElement
  await act(async () => {
    setNativeValue(input, query)
  })
  await act(async () => {
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    await Promise.resolve()
  })
}

beforeEach(() => {
  fetchMemories.mockReset()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
  vi.restoreAllMocks()
})

describe('Mem0View', () => {
  it('renders a search box and does not fetch until a search is submitted', () => {
    mount()
    expect(container.querySelector('input[aria-label="Search memories"]')).not.toBeNull()
    expect(fetchMemories).not.toHaveBeenCalled()
    expect(container.textContent).toMatch(/search/i)
  })

  it('shows a loading indicator while a search is in flight', async () => {
    fetchMemories.mockReturnValue(new Promise(() => {}))
    mount()
    await search('deploy failures')
    expect(container.querySelector('[role="status"]')).not.toBeNull()
  })

  it('fetches with the entered query and limit 20, then renders the typed memory table', async () => {
    fetchMemories.mockResolvedValue({
      configured: true,
      memories: [
        {
          memory: 'The deploy pipeline retries on rate_limit.',
          metadata: { category: 'runbook', project: 'hivepilot', task: 'deploy', ts: '2026-07-10T12:00:00Z' },
        },
      ],
    } satisfies MemoriesResponse)

    mount()
    await search('deploy')

    expect(fetchMemories).toHaveBeenCalledWith('deploy', 20)
    expect(container.textContent).toContain('The deploy pipeline retries on rate_limit.')
    expect(container.textContent).toContain('runbook')
    expect(container.textContent).toContain('hivepilot')
    expect(container.textContent).toContain('deploy')
  })

  it('shows an empty state when the search returns no memories', async () => {
    fetchMemories.mockResolvedValue({ configured: true, memories: [] } satisfies MemoriesResponse)

    mount()
    await search('nonexistent-topic')

    expect(container.textContent).toMatch(/no memories found/i)
  })

  it('shows the backend detail when mem0 is not configured', async () => {
    fetchMemories.mockResolvedValue({
      configured: false,
      memories: [],
      detail: 'mem0 not configured (mem0_enabled is off, mem0ai isn\'t installed, or the mem0 client could not be built)',
    } satisfies MemoriesResponse)

    mount()
    await search('anything')

    expect(container.textContent).toMatch(/not configured/i)
  })

  it('CRITICAL: shows a graceful "requires an admin token" message on a 403 — not a crash or generic error', async () => {
    fetchMemories.mockRejectedValue(new ApiForbiddenError())

    mount()
    await search('deploy')

    expect(container.querySelector('[role="alert"]')).toBeNull()
    const forbidden = container.querySelector('[data-testid="mem0-forbidden"]')
    expect(forbidden).not.toBeNull()
    expect(forbidden?.textContent).toMatch(/admin/i)
    expect(container.textContent).not.toMatch(/something went wrong/i)
    // No unhandled rejection / crash: the view is still mounted and interactive.
    expect(container.querySelector('input[aria-label="Search memories"]')).not.toBeNull()
  })

  it('shows a generic error card (not the admin message) for a non-403 failure', async () => {
    fetchMemories.mockRejectedValue(new Error('network down'))

    mount()
    await search('deploy')

    const alert = container.querySelector('[role="alert"]')
    expect(alert?.textContent).toContain('network down')
    expect(alert?.textContent).not.toMatch(/admin/i)
  })

  it('renders French title, search hint, and table headers when the language is fr (P1a)', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    fetchMemories.mockResolvedValue({
      configured: true,
      memories: [
        {
          memory: 'Le pipeline de déploiement relance en cas de rate_limit.',
          metadata: { category: 'runbook', project: 'hivepilot', task: 'deploy', ts: '2026-07-10T12:00:00Z' },
        },
      ],
    } satisfies MemoriesResponse)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <Mem0View />
        </LanguageProvider>,
      )
    })
    expect(container.textContent).toContain('Recherche de mémoire Mem0')
    expect(container.textContent).toContain('Saisissez une recherche ci-dessus pour consulter les mémoires.')

    const input = container.querySelector('input[aria-label="Rechercher des mémoires"]') as HTMLInputElement
    const form = container.querySelector('form') as HTMLFormElement
    await act(async () => {
      setNativeValue(input, 'deploy')
    })
    await act(async () => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Catégorie')
    expect(container.textContent).toContain('Horodatage')
  })
})
