/**
 * Fail-closed RBAC role context for the Mirador web UI (Mirador actionable
 * dashboard PRD, Sprint 1). `RoleProvider` resolves the calling token's own
 * role once — via `GET /v1/whoami` (`@/lib/mirador-api`) — and exposes it
 * app-wide through `useRole()`, so later sprints (S2-S5: approve/deny,
 * run triggers, plugin toggles, ...) can show/hide action controls based on
 * the caller's actual privilege instead of guessing from what data loaded.
 *
 * `RoleProvider` is mounted inside `Mirador.tsx`, which itself only renders
 * once `TokenGate` has already validated the stored token (see `App.tsx` /
 * `TokenGate.tsx`) — so the `whoami()` fetch below naturally happens
 * "once, right after the token validates", without `RoleProvider` needing
 * to know anything about the gate itself. `apiFetch` (used internally by
 * `whoami()`) already clears an invalidated token and fires
 * `TOKEN_CLEARED_EVENT` on 401/403 exactly like every other endpoint —
 * `TokenGate` reacts to that the same way it always has.
 *
 * FAIL-CLOSED CONTRACT (the whole point of this PRD): an unknown, null, or
 * not-yet-resolved role must never grant anything. `roleRank` maps any
 * value that isn't one of the four recognized roles — including `null`
 * (no token yet / fetch still in flight) and any unrecognized string (a
 * defensive guard against a malformed/unexpected backend response, even
 * though `WhoAmI.role` is typed as `Role`) — to `-Infinity`, so `can()` is
 * `false` for every required role until a real, recognized role is set.
 */
import { createContext, type ReactNode, useContext, useEffect, useMemo, useState } from 'react'
import { type Role, whoami } from './mirador-api'

export type { Role }

/** Same ordering as the backend's `ROLE_RANKS`
 * (`hivepilot/services/token_service.py`): read < run < approve < admin. */
const ROLE_RANKS: Record<Role, number> = {
  read: 0,
  run: 1,
  approve: 2,
  admin: 3,
}

/** Fail-closed rank lookup: anything not a recognized `Role` (null,
 * undefined, or an unexpected string) ranks below every real role. */
function roleRank(role: Role | null | undefined): number {
  if (role == null) return Number.NEGATIVE_INFINITY
  const rank = ROLE_RANKS[role]
  return rank === undefined ? Number.NEGATIVE_INFINITY : rank
}

export interface RoleContextValue {
  /** The resolved role, or `null` before `whoami()` resolves (or if it
   * fails/the caller has no valid token yet). */
  role: Role | null
  /** `roleRank(role)` — `-Infinity` whenever `role` is `null`/unrecognized. */
  rank: number
  /** `true` iff the caller's rank is >= `required`'s rank. Always `false`
   * when `role` is `null`/unrecognized, for every `required` value. */
  can(required: Role): boolean
}

const RoleContext = createContext<RoleContextValue>({
  role: null,
  rank: Number.NEGATIVE_INFINITY,
  can: () => false,
})

interface RoleProviderProps {
  children: ReactNode
}

export function RoleProvider({ children }: RoleProviderProps) {
  const [role, setRole] = useState<Role | null>(null)

  useEffect(() => {
    let cancelled = false
    whoami()
      .then((data) => {
        if (!cancelled) setRole(data.role)
      })
      .catch(() => {
        // Fail-closed: any whoami failure (network error, 401/403 — the
        // latter already clears the token via apiFetch) leaves role at its
        // default `null`, never a guessed/permissive value.
        if (!cancelled) setRole(null)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const value = useMemo<RoleContextValue>(() => {
    const rank = roleRank(role)
    return {
      role,
      rank,
      can: (required: Role) => rank >= roleRank(required),
    }
  }, [role])

  return <RoleContext.Provider value={value}>{children}</RoleContext.Provider>
}

export function useRole(): RoleContextValue {
  return useContext(RoleContext)
}
