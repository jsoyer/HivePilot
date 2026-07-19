import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Role } from './mirador-api'

const { whoamiMock } = vi.hoisted(() => ({ whoamiMock: vi.fn() }))

vi.mock('./mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./mirador-api')>()
  return { ...actual, whoami: whoamiMock }
})

import { RoleProvider, type RoleContextValue, useRole } from './role-context'

const ROLES: Role[] = ['read', 'run', 'approve', 'admin']

let container: HTMLDivElement
let root: Root
let captured: RoleContextValue | null

function Consumer() {
  captured = useRole()
  return null
}

function mount() {
  act(() => {
    root.render(
      <RoleProvider>
        <Consumer />
      </RoleProvider>,
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
  whoamiMock.mockReset()
  captured = null
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

describe('RoleProvider / useRole', () => {
  it('starts with role null / rank -Infinity before whoami resolves', () => {
    const pending = deferred<{ role: Role; tenant: string }>()
    whoamiMock.mockReturnValue(pending.promise)

    mount()

    expect(captured?.role).toBeNull()
    expect(captured?.rank).toBe(Number.NEGATIVE_INFINITY)
  })

  it('calls whoami() exactly once on mount', () => {
    const pending = deferred<{ role: Role; tenant: string }>()
    whoamiMock.mockReturnValue(pending.promise)

    mount()

    expect(whoamiMock).toHaveBeenCalledTimes(1)
  })

  it('populates role/rank once whoami resolves', async () => {
    whoamiMock.mockResolvedValue({ role: 'approve', tenant: 'acme' })

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(captured?.role).toBe('approve')
    expect(captured?.rank).toBe(2)
  })

  it('re-renders do not trigger a second whoami() call', async () => {
    whoamiMock.mockResolvedValue({ role: 'read', tenant: 'default' })

    await act(async () => {
      mount()
      await Promise.resolve()
    })
    await act(async () => {
      mount() // re-render the same provider instance
      await Promise.resolve()
    })

    expect(whoamiMock).toHaveBeenCalledTimes(1)
  })

  it('stays role null (fail-closed) when whoami rejects (e.g. 401/403)', async () => {
    whoamiMock.mockRejectedValue(new Error('unauthorized'))

    await act(async () => {
      mount()
      await Promise.resolve()
    })

    expect(captured?.role).toBeNull()
    expect(captured?.rank).toBe(Number.NEGATIVE_INFINITY)
  })

  describe('can() — role x required matrix (fail-closed)', () => {
    it.each(ROLES.flatMap((role) => ROLES.map((required) => [role, required] as const)))(
      'role=%s can(%s)',
      async (role, required) => {
        whoamiMock.mockResolvedValue({ role, tenant: 'default' })
        await act(async () => {
          mount()
          await Promise.resolve()
        })
        const expected = ROLES.indexOf(role) >= ROLES.indexOf(required)
        expect(captured?.can(required)).toBe(expected)
      },
    )

    it('returns false for every required role while role is still null (not yet resolved)', () => {
      const pending = deferred<{ role: Role; tenant: string }>()
      whoamiMock.mockReturnValue(pending.promise)

      mount()

      for (const required of ROLES) {
        expect(captured?.can(required)).toBe(false)
      }
    })

    it('returns false for every required role when whoami rejects', async () => {
      whoamiMock.mockRejectedValue(new Error('unauthorized'))

      await act(async () => {
        mount()
        await Promise.resolve()
      })

      for (const required of ROLES) {
        expect(captured?.can(required)).toBe(false)
      }
    })

    it('returns false for every required role for an unrecognized role string (defense-in-depth)', async () => {
      whoamiMock.mockResolvedValue({ role: 'superuser' as unknown as Role, tenant: 'default' })

      await act(async () => {
        mount()
        await Promise.resolve()
      })

      for (const required of ROLES) {
        expect(captured?.can(required)).toBe(false)
      }
    })
  })

  it('useRole() outside a RoleProvider defaults to fail-closed (role null, can() always false)', () => {
    act(() => {
      root.render(<Consumer />)
    })

    expect(captured?.role).toBeNull()
    expect(captured?.rank).toBe(Number.NEGATIVE_INFINITY)
    for (const required of ROLES) {
      expect(captured?.can(required)).toBe(false)
    }
  })
})
