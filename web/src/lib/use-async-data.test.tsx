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

  it('re-fetches when a dep changes, without discarding the value', async () => {
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

  it('stale-while-revalidate: keeps the prior data visible (no loading flip) while a dep-triggered refetch is in flight, and still shows loading on first mount', async () => {
    const first = deferred<string>()
    const fetcherA = vi.fn().mockReturnValue(first.promise)
    act(() => {
      root.render(<Probe fetcher={fetcherA} deps={['a']} />)
    })
    // First mount, nothing resolved yet — the full loading state, same as
    // ever.
    expect(readState()).toEqual({ status: 'loading' })

    await act(async () => {
      first.resolve('v1')
      await first.promise
    })
    expect(readState()).toEqual({ status: 'success', data: 'v1' })

    // A dep change triggers a refetch — because data already exists, this
    // must NOT collapse back to `{ status: 'loading' }` (that's the bug:
    // it causes the visible layout "jump" every poll). It should keep
    // rendering the last-known data and flag `isRefreshing` instead.
    const second = deferred<string>()
    const fetcherB = vi.fn().mockReturnValue(second.promise)
    act(() => {
      root.render(<Probe fetcher={fetcherB} deps={['b']} />)
    })
    expect(readState()).toEqual({ status: 'success', data: 'v1', isRefreshing: true })

    // Once the refetch resolves, the new data replaces the old and
    // `isRefreshing` clears.
    await act(async () => {
      second.resolve('v2')
      await second.promise
    })
    expect(readState()).toEqual({ status: 'success', data: 'v2' })
  })

  it('stale-while-revalidate: a refetch that errors still surfaces the error (no silent stale data)', async () => {
    const first = deferred<string>()
    const fetcherA = vi.fn().mockReturnValue(first.promise)
    act(() => {
      root.render(<Probe fetcher={fetcherA} deps={['a']} />)
    })
    await act(async () => {
      first.resolve('v1')
      await first.promise
    })
    expect(readState()).toEqual({ status: 'success', data: 'v1' })

    const second = deferred<string>()
    const fetcherB = vi.fn().mockReturnValue(second.promise)
    act(() => {
      root.render(<Probe fetcher={fetcherB} deps={['b']} />)
    })
    expect(readState()).toEqual({ status: 'success', data: 'v1', isRefreshing: true })

    await act(async () => {
      second.reject(new Error('refresh failed'))
      await second.promise.catch(() => undefined)
    })
    expect(readState().status).toBe('error')
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

  it('does not update state (or warn) when the fetch resolves after unmount', async () => {
    const gate = deferred<string>()
    const fetcher = vi.fn().mockReturnValue(gate.promise)
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    act(() => {
      root.render(<Probe fetcher={fetcher} deps={[]} />)
    })
    expect(readState().status).toBe('loading')
    expect(container.textContent).not.toBe('')

    // Unmount BEFORE the fetch resolves — this runs the hook's cleanup,
    // which flips its internal `cancelled` flag before `fetcher`'s promise
    // ever settles.
    act(() => {
      root.unmount()
    })
    expect(container.innerHTML).toBe('')

    // Now resolve the deferred promise. Two independent regression guards:
    // (1) no React warning is logged (the classic pre-React-18 signal for a
    // "setState on an unmounted component" bug — React 18+ no longer emits
    // this warning since the update is a guaranteed no-op regardless, but
    // asserting it stays silent still catches a regression in any renderer
    // that DOES warn), and (2) the container stays empty — if a stale
    // update somehow caused React to re-commit content after the resolve,
    // this would catch it directly at the DOM level rather than trusting
    // console output alone.
    await act(async () => {
      gate.resolve('too-late')
      await gate.promise
    })

    expect(consoleError).not.toHaveBeenCalled()
    expect(container.innerHTML).toBe('')

    // Recreate the root so `afterEach`'s `root.unmount()` has a live root to
    // unmount (this test already unmounted the original one).
    root = createRoot(container)
  })
})
