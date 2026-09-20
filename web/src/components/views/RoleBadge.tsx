import { ROLE_AVATAR_COLORS } from '@/lib/role-avatars'

function hexWithAlpha(hex: string, alpha: string): string {
  return `${hex}${alpha}`
}

/** Role chip using HP-20 kit colours (`ROLE_AVATAR_COLORS`), not sky accent. */
export function RoleBadge({
  role,
  label,
}: {
  role: string
  label?: string
}) {
  const color = ROLE_AVATAR_COLORS[role] ?? '#a1a1aa'
  return (
    <span
      data-testid={`role-badge-${role}`}
      className="inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium"
      style={{
        color,
        backgroundColor: hexWithAlpha(color, '1f'),
        borderColor: hexWithAlpha(color, '40'),
      }}
    >
      <span aria-hidden="true" className="size-1.5 rounded-full" style={{ backgroundColor: color }} />
      {label ?? role}
    </span>
  )
}

export function displayActorName(label: string): string {
  const cut = label.indexOf(' (')
  return cut > 0 ? label.slice(0, cut) : label
}
