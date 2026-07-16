/**
 * Typed API client for HivePilot's same-origin `/v1/*` API.
 *
 * Wraps `fetch`, injects the `Authorization: Bearer <token>` header from the
 * token stashed in `localStorage` by the token gate (see
 * `components/TokenGate.tsx`), and clears the token on 401/403 so the app
 * falls back to the gate on the next render. This is the client Sprint 3's
 * real views (Analytics / Cost / Health / Mem0) will reuse — keep it small
 * and dependency-free.
 */

export const TOKEN_STORAGE_KEY = 'hivepilot.webui.token'

/** Dispatched on `window` whenever `clearToken` runs, so any mounted
 * component (notably `TokenGate`, which only validates the token once on
 * mount) can react to a background 401 and fall back to the token gate
 * without a full page reload. */
export const TOKEN_CLEARED_EVENT = 'hivepilot:token-cleared'

/** Thrown by `apiFetch` on 401 (always) or 403 (default `on403: 'clear'`) —
 * the token itself is treated as invalid; the caller should re-show the
 * token gate. */
export class ApiAuthError extends Error {
  readonly name = 'ApiAuthError'
  readonly status: number

  constructor(status: number) {
    super(`Not authorized (HTTP ${status})`)
    this.status = status
  }
}

/** Thrown by `apiFetch` on a 403 when called with `{ on403: 'forbidden' }` —
 * the token is still valid, it just lacks the role this specific endpoint
 * requires (e.g. a `read` token calling the `admin`-only `/v1/memories`).
 * Unlike `ApiAuthError`, this does NOT clear the stored token. */
export class ApiForbiddenError extends Error {
  readonly name = 'ApiForbiddenError'
  readonly status = 403 as const

  constructor() {
    super('Forbidden (HTTP 403) — the current token lacks the role this endpoint requires')
  }
}

/** Thrown by `apiFetch` for any other non-2xx response. */
export class ApiError extends Error {
  readonly name = 'ApiError'
  readonly status: number

  constructor(status: number, detail?: string) {
    super(detail ? `Request failed (HTTP ${status}): ${detail}` : `Request failed (HTTP ${status})`)
    this.status = status
  }
}

export function getToken(): string | null {
  return window.localStorage.getItem(TOKEN_STORAGE_KEY)
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, token)
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_STORAGE_KEY)
  window.dispatchEvent(new Event(TOKEN_CLEARED_EVENT))
}

async function readDetail(response: Response): Promise<string | undefined> {
  try {
    const body: unknown = await response.clone().json()
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail?: unknown }).detail
      return typeof detail === 'string' ? detail : undefined
    }
  } catch {
    // Non-JSON body — no detail available, fall through.
  }
  return undefined
}

export interface ApiFetchOptions extends RequestInit {
  /**
   * How to treat an HTTP 403 response. Defaults to `'clear'` — the original
   * behavior: identical to a 401 (clear the stored token, throw
   * `ApiAuthError`), which is correct for every endpoint that requires the
   * same role as the token gate itself validates against.
   *
   * Pass `'forbidden'` for an endpoint that legitimately requires a HIGHER
   * role than the gate checks (e.g. `/v1/memories` requires `admin`, but the
   * gate only validates a `read`-role call). A 403 there throws
   * `ApiForbiddenError` instead and leaves the token untouched, so a valid
   * lower-role token keeps working for every other endpoint.
   */
  on403?: 'clear' | 'forbidden'
}

/**
 * Fetch `path` (a same-origin `/v1/*` path) with the stored bearer token
 * attached, parsing and returning the JSON response body.
 *
 * A 401 always means the token itself is invalid: it's cleared and an
 * `ApiAuthError` is thrown — the caller should route back to the token gate
 * (or simply let `TOKEN_CLEARED_EVENT` do it). A 403 does the same by
 * default (`on403: 'clear'`); pass `{ on403: 'forbidden' }` for an endpoint
 * whose role requirement is intentionally higher than the gate's own check
 * (see `ApiFetchOptions.on403`). Any other non-2xx status throws `ApiError`
 * without touching the token.
 */
export async function apiFetch<T>(path: string, init: ApiFetchOptions = {}): Promise<T> {
  const { on403 = 'clear', ...requestInit } = init
  const token = getToken()
  const headers = new Headers(requestInit.headers)
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  const response = await fetch(path, { ...requestInit, headers })

  if (response.status === 401) {
    clearToken()
    throw new ApiAuthError(401)
  }

  if (response.status === 403) {
    if (on403 === 'forbidden') {
      throw new ApiForbiddenError()
    }
    clearToken()
    throw new ApiAuthError(403)
  }

  if (!response.ok) {
    const detail = await readDetail(response)
    throw new ApiError(response.status, detail)
  }

  return (await response.json()) as T
}
