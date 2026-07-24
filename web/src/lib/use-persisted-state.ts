import { useCallback, useState, type Dispatch, type SetStateAction } from 'react'

/**
 * `useState` backed by `window.localStorage`, namespaced under a caller-
 * supplied key (see `TOKEN_STORAGE_KEY` in `./api` for the `hivepilot.webui.*`
 * naming convention this project already uses). Used by the sidebar's
 * collapse toggle and the theme toggle (P0b) so both survive a page reload
 * without introducing a state-management dependency.
 *
 * Reads are synchronous (lazy `useState` initializer) so there is no
 * flash-of-default-then-persisted-value on mount. A malformed/missing stored
 * value falls back to `defaultValue` rather than throwing — this hook must
 * never crash the shell over a corrupted localStorage entry.
 */
export function usePersistedState<T>(
  key: string,
  defaultValue: T,
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key)
      if (raw === null) return defaultValue
      return JSON.parse(raw) as T
    } catch {
      return defaultValue
    }
  })

  const setPersisted = useCallback<Dispatch<SetStateAction<T>>>(
    (next) => {
      setValue((prev) => {
        const resolved = typeof next === 'function' ? (next as (prev: T) => T)(prev) : next
        try {
          window.localStorage.setItem(key, JSON.stringify(resolved))
        } catch {
          // Storage unavailable/full — the in-memory state still updates,
          // it just won't survive a reload. Never crash the shell over it.
        }
        return resolved
      })
    },
    [key],
  )

  return [value, setPersisted]
}
