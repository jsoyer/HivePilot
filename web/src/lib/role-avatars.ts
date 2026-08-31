import type { AvatarDefinition } from '@bible-strong/avatar-core'
import baseAvatar from '@/assets/avatars/base.avatar.json'

/**
 * Per-role procedural avatars for the Pollen roster (HP-20).
 *
 * We ship ONE base procedural avatar (`base.avatar.json`, the "Strobi" example
 * from the Bible Strong Avatar Lab — see `assets/avatars/ATTRIBUTION.md`) and
 * derive a visually distinct avatar per role by overriding only the body
 * colour. The base already carries the full expression + animation set
 * (`idle`, `thinking`, `working`, `happy`, `sad`, `suspicious`, …) that
 * `roleStateAnimation` maps run state onto, so every role reuses the same rich
 * rig with its own palette. Bespoke per-persona avatars can be authored later
 * in the Lab and dropped in without touching consumers.
 */

/** The eight first-class agent roles, keyed by their `roles.yaml` name. */
export const ROLE_AVATAR_COLORS: Readonly<Record<string, string>> = {
  ceo: '#a855f7', // Aliénor — strategy (violet)
  chief_of_staff: '#14b8a6', // Jules — coordination (teal)
  cto: '#3b82f6', // Blaise — architecture (blue)
  developer: '#22c55e', // Gustave — implementation (green)
  reviewer: '#f59e0b', // Victor — review (amber)
  ciso: '#ef4444', // Hugo — security (red)
  qa: '#ec4899', // Marie — quality (pink)
  documentation: '#64748b', // Théo — docs (slate)
}

/** Run/interaction state a role avatar can reflect. */
export type RoleAvatarState = 'idle' | 'running' | 'thinking' | 'success' | 'failed' | 'blocked'

/**
 * Map a role's live state onto one of the base avatar's animation keys.
 * Every value here MUST exist in `base.avatar.json`'s `animations`.
 */
const STATE_ANIMATION: Readonly<Record<RoleAvatarState, string>> = {
  idle: 'idle',
  running: 'working',
  thinking: 'thinking',
  success: 'happy',
  failed: 'sad',
  blocked: 'suspicious',
}

export function roleStateAnimation(state: RoleAvatarState = 'idle'): string {
  return STATE_ANIMATION[state] ?? 'idle'
}

export function hasRoleAvatar(role: string): boolean {
  return role in ROLE_AVATAR_COLORS
}

// Definitions are cached per role: the renderer validates a definition once
// per immutable object reference, so a stable object per role avoids
// re-validation on every render.
const definitionCache = new Map<string, AvatarDefinition>()

/**
 * Build (and cache) the recoloured avatar definition for `role`, or return
 * `null` when the role has no mapped avatar (caller falls back to initials).
 */
export function roleAvatarDefinition(role: string): AvatarDefinition | null {
  const color = ROLE_AVATAR_COLORS[role]
  if (!color) return null
  const cached = definitionCache.get(role)
  if (cached) return cached
  const base = baseAvatar as unknown as AvatarDefinition
  const definition = {
    ...structuredClone(base),
    name: role,
    colors: { ...(base.colors ?? {}), body: color },
  } as AvatarDefinition
  definitionCache.set(role, definition)
  return definition
}
