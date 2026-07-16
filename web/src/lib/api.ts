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

/** Thrown by `apiFetch` on 401/403 — the caller should re-show the token gate. */
export class ApiAuthError extends Error {
  readonly name = 'ApiAuthError'
  readonly status: number

  constructor(status: number) {
    super(`Not authorized (HTTP ${status})`)
    this.status = status
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

/**
 * Fetch `path` (a same-origin `/v1/*` path) with the stored bearer token
 * attached, parsing and returning the JSON response body.
 *
 * On 401/403 the stored token is cleared (it's no longer valid) and an
 * `ApiAuthError` is thrown; the caller should route back to the token gate.
 * Any other non-2xx status throws `ApiError` without touching the token.
 */
export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers = new Headers(init.headers)
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  const response = await fetch(path, { ...init, headers })

  if (response.status === 401 || response.status === 403) {
    clearToken()
    throw new ApiAuthError(response.status)
  }

  if (!response.ok) {
    const detail = await readDetail(response)
    throw new ApiError(response.status, detail)
  }

  return (await response.json()) as T
}
