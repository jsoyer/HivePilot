import '@bible-strong/avatar-react/styles.css'
import { Avatar } from '@bible-strong/avatar-react'
import { useMemo } from 'react'
import {
  roleAvatarDefinition,
  roleStateAnimation,
  type RoleAvatarState,
} from '@/lib/role-avatars'

/**
 * A role's procedural avatar (HP-20). Renders the animated Bible Strong avatar
 * for one of the eight agent roles, recoloured per role, and reflecting run
 * state through the `state` prop (`idle` → idle loop, `running` → working,
 * `success` → happy, …).
 *
 * Unknown/unmapped roles fall back to a coloured initial, so this is a safe
 * drop-in replacement anywhere a small role badge is shown.
 */
export function RoleAvatar({
  role,
  label,
  size = 24,
  state = 'idle',
}: {
  role: string
  label?: string
  size?: number
  state?: RoleAvatarState
}) {
  const definition = useMemo(() => roleAvatarDefinition(role), [role])
  const testId = `agent-avatar-${role}`

  if (!definition) {
    const initial = [...(label ?? role).trim()][0]?.toUpperCase() ?? '?'
    return (
      <span
        aria-hidden="true"
        data-testid={testId}
        style={{ width: size, height: size, fontSize: Math.round(size * 0.45) }}
        className="inline-flex shrink-0 items-center justify-center rounded-full bg-muted font-semibold text-muted-foreground"
      >
        {initial}
      </span>
    )
  }

  return (
    <span
      data-testid={testId}
      style={{ width: size, height: size }}
      className="inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full"
    >
      <Avatar
        definition={definition}
        animation={roleStateAnimation(state)}
        size={size}
        ariaLabel={label ?? role}
      />
    </span>
  )
}
