import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ApiAuthError,
  apiFetch,
  clearToken,
  getToken,
  setToken,
  TOKEN_STORAGE_KEY,
} from './api'

describe('token storage', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('returns null when no token is stored', () => {
    expect(getToken()).toBeNull()
  })

  it('round-trips a token through setToken/getToken', () => {
    setToken('abc123')
    expect(getToken()).toBe('abc123')
    expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBe('abc123')
  })

  it('removes the token on clearToken', () => {
    setToken('abc123')
    clearToken()
    expect(getToken()).toBeNull()
  })
})

describe('apiFetch', () => {
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    window.localStorage.clear()
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  it('injects the Authorization header from the stored token', async () => {
    setToken('secret-token')
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
    globalThis.fetch = fetchMock as unknown as typeof fetch

    await apiFetch('/v1/plugins/health')

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/v1/plugins/health')
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toBe('Bearer secret-token')
  })

  it('does not send an Authorization header when no token is stored', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200 }),
    )
    globalThis.fetch = fetchMock as unknown as typeof fetch

    await apiFetch('/v1/plugins/health')

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const headers = new Headers(init.headers)
    expect(headers.has('Authorization')).toBe(false)
  })

  it('parses and returns the JSON body on success', async () => {
    setToken('secret-token')
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ plugins: [] }), { status: 200 }),
    ) as unknown as typeof fetch

    const data = await apiFetch<{ plugins: unknown[] }>('/v1/plugins/health')
    expect(data).toEqual({ plugins: [] })
  })

  it('clears the stored token and throws ApiAuthError on 401', async () => {
    setToken('bad-token')
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Missing token' }), { status: 401 }),
    ) as unknown as typeof fetch

    await expect(apiFetch('/v1/plugins/health')).rejects.toBeInstanceOf(ApiAuthError)
    expect(getToken()).toBeNull()
  })

  it('clears the stored token and throws ApiAuthError on 403', async () => {
    setToken('low-role-token')
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Forbidden' }), { status: 403 }),
    ) as unknown as typeof fetch

    await expect(apiFetch('/v1/plugins/health')).rejects.toBeInstanceOf(ApiAuthError)
    expect(getToken()).toBeNull()
  })

  it('throws a plain Error (not ApiAuthError) on other non-2xx statuses', async () => {
    setToken('secret-token')
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: 'boom' }), { status: 500 }),
    ) as unknown as typeof fetch

    await expect(apiFetch('/v1/plugins/health')).rejects.not.toBeInstanceOf(ApiAuthError)
    // A 500 is a server error, not an auth failure — the token stays valid.
    expect(getToken()).toBe('secret-token')
  })
})
