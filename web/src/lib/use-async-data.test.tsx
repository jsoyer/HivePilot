import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAsyncData } from './use-async-data'

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
  vi.restoreAllMocks()
})

function Probe({ fetcher, deps }: { fetcher: () => Promise<string>; deps: unknown[] }) {
  const state = useAsyncData(fetcher, deps)
  return <div data-testid="probe">{JSON.stringify(state)}</div>
}

function readState(): { status: string; data?: unknown; error?: unknown } {
  const el = container.querySelector('[data-testid="probe"]') as HTMLElement
  return JSON.parse(el.textContent ?? '{}')
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

describe('useAsyncData', () => {
  it('starts in the loading state', () => {
    const pending = new Promise<string>(() => {})
    act(() => {
      root.render(<Probe fetcher={() => pending} deps={[]} />)
    })
    expect(readState().status).toBe('loading')
  })

  it('transitions to success with the resolved data', async () => {
    const gate = deferred<string>()
    act(() => {
      root.render(<Probe fetcher={() => gate.promise} deps={[]} />)
    })

    await act(async () => {
      gate.resolve('hello')
      await gate.promise
    })

    expect(readState()).toEqual({ status: 'success', data: 'hello' })
  })

  it('transitions to error with the rejection reason', async () => {
    const gate = deferred<string>()
    act(() => {
      root.render(<Probe fetcher={() => gate.promise} deps={[]} />)
    })

    await act(async () => {
      gate.reject(new Error('boom'))
      await gate.promise.catch(() => undefined)
    })

    expect(readState().status).toBe('error')
  })

  it('re-fetches when a dep changes and resets to loading', async () => {
    const fetcher = vi.fn().mockResolvedValue('v1')
    act(() => {
      root.render(<Probe fetcher={fetcher} deps={['a']} />)
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(readState()).toEqual({ status: 'success', data: 'v1' })
    expect(fetcher).toHaveBeenCalledTimes(1)

    const fetcher2 = vi.fn().mockResolvedValue('v2')
    act(() => {
      root.render(<Probe fetcher={fetcher2} deps={['b']} />)
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(readState()).toEqual({ status: 'success', data: 'v2' })
    expect(fetcher2).toHaveBeenCalledTimes(1)
  })

  it('ignores a stale resolution after deps change (no update-after-unmount/stale race)', async () => {
    const first = deferred<string>()
    const fetcherA = vi.fn().mockReturnValue(first.promise)
    act(() => {
      root.render(<Probe fetcher={fetcherA} deps={['a']} />)
    })

    const fetcherB = vi.fn().mockResolvedValue('fresh')
    act(() => {
      root.render(<Probe fetcher={fetcherB} deps={['b']} />)
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(readState()).toEqual({ status: 'success', data: 'fresh' })

    // The stale first fetch resolves after the deps change — it must not
    // clobber the fresh state.
    await act(async () => {
      first.resolve('stale')
      await first.promise
    })
    expect(readState()).toEqual({ status: 'success', data: 'fresh' })
  })
})
