import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiAuthError, clearToken, getToken, setToken } from '@/lib/api'
import { TokenGate } from './TokenGate'

const { apiFetchMock } = vi.hoisted(() => ({ apiFetchMock: vi.fn() }))

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, apiFetch: apiFetchMock }
})

let container: HTMLDivElement
let root: Root

function mount() {
  act(() => {
    root.render(
      <TokenGate>
        <div data-testid="protected-content">Protected content</div>
      </TokenGate>,
    )
  })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

beforeEach(() => {
  window.localStorage.clear()
  apiFetchMock.mockReset()
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

describe('TokenGate', () => {
  it('shows the token form immediately when no token is stored', async () => {
    mount()
    // No stored token — no validation call, no "checking" flash.
    expect(apiFetchMock).not.toHaveBeenCalled()
    expect(container.querySelector('input[aria-label="HivePilot read token"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="protected-content"]')).toBeNull()
  })

  it('shows a checking state, then children, when the stored token validates', async () => {
    setToken('good-token')
    const gate = deferred<unknown>()
    apiFetchMock.mockReturnValue(gate.promise)

    mount()

    expect(container.textContent).toContain('Checking')
    expect(container.querySelector('[data-testid="protected-content"]')).toBeNull()

    await act(async () => {
      gate.resolve({ plugins: [] })
      await gate.promise
    })

    expect(container.querySelector('[data-testid="protected-content"]')).not.toBeNull()
    expect(apiFetchMock).toHaveBeenCalledWith('/v1/plugins/health')
  })

  it('falls back to the token form when the stored token fails validation', async () => {
    setToken('stale-token')
    apiFetchMock.mockRejectedValue(new ApiAuthError(401))

    await act(async () => {
      mount()
    })

    expect(container.querySelector('input[aria-label="HivePilot read token"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="protected-content"]')).toBeNull()
  })

  it('submits the entered token, stores it, and reveals children on success', async () => {
    apiFetchMock.mockResolvedValue({ plugins: [] })

    await act(async () => {
      mount()
    })

    const input = container.querySelector('input[aria-label="HivePilot read token"]') as HTMLInputElement
    const form = container.querySelector('form') as HTMLFormElement

    await act(async () => {
      input.dispatchEvent(new Event('focus', { bubbles: true }))
      const nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        'value',
      )!.set!
      nativeSetter.call(input, 'my-read-token')
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })

    await act(async () => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })

    expect(getToken()).toBe('my-read-token')
    expect(container.querySelector('[data-testid="protected-content"]')).not.toBeNull()
  })

  it('shows an error and keeps the form when submitting an invalid token', async () => {
    apiFetchMock.mockRejectedValue(new ApiAuthError(401))

    await act(async () => {
      mount()
    })

    const input = container.querySelector('input[aria-label="HivePilot read token"]') as HTMLInputElement
    const form = container.querySelector('form') as HTMLFormElement
    const nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      'value',
    )!.set!

    await act(async () => {
      nativeSetter.call(input, 'bad-token')
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })

    expect(container.querySelector('[role="alert"]')?.textContent).toMatch(/invalid/i)
    expect(container.querySelector('[data-testid="protected-content"]')).toBeNull()
    // Clearing the token on 401/403 is apiFetch's job (covered by
    // api.test.ts) — apiFetch is mocked out here, so it's a no-op; the gate
    // itself only reacts to the thrown ApiAuthError.
  })

  it('falls back to the token form when a background call (e.g. from Mirador) clears the token', async () => {
    setToken('good-token')
    apiFetchMock.mockResolvedValue({ plugins: [] })

    await act(async () => {
      mount()
    })

    expect(container.querySelector('[data-testid="protected-content"]')).not.toBeNull()

    // Simulate a background 401 elsewhere in the app: apiFetch (the real,
    // un-mocked implementation) clears the token and fires
    // TOKEN_CLEARED_EVENT. The gate itself doesn't need to know which view
    // triggered it — it just reacts to the event.
    await act(async () => {
      clearToken()
    })

    expect(container.querySelector('input[aria-label="HivePilot read token"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="protected-content"]')).toBeNull()
  })
})
