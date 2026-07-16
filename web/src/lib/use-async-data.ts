import { useEffect, useState } from 'react'

export type AsyncState<T> =
  | { status: 'loading' }
  | { status: 'error'; error: unknown }
  | { status: 'success'; data: T }

/**
 * Minimal shared data-fetching hook for Mirador's tab views. Calls `fetcher`
 * on mount and whenever any value in `deps` changes, tracking a tri-state
 * (loading/error/success) result.
 *
 * Deliberately dumb: it does NOT decide what counts as "empty" — an empty
 * `success` payload (e.g. `{ hotspots: [] }`) is still `status: 'success'`.
 * Each view renders its own empty-state from the resolved data; this hook
 * only owns the fetch lifecycle.
 *
 * Guards against stale updates: if `deps` change (or the component unmounts)
 * before an in-flight fetch resolves, that resolution is discarded so an
 * old, slower request can never clobber a newer one's result.
 */
export function useAsyncData<T>(fetcher: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [state, setState] = useState<AsyncState<T>>({ status: 'loading' })

  // `deps` is an intentionally caller-controlled dependency array (mirrors
  // useEffect's own contract, one level up) — this hook re-runs `fetcher`
  // whenever any entry changes, by design. `fetcher` itself is deliberately
  // excluded: callers pass a fresh closure every render, and this hook's
  // whole contract is "re-fetch on `deps` change, not on every render".
  // oxlint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    let cancelled = false
    setState({ status: 'loading' })
    fetcher()
      .then((data) => {
        if (!cancelled) setState({ status: 'success', data })
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ status: 'error', error })
      })
    return () => {
      cancelled = true
    }
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return state
}
