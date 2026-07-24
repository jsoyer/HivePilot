import type { LucideIcon } from 'lucide-react'

/**
 * Grouped sidebar navigation (Mirador → "Vigie" dashboard upgrade, P0b).
 * Mirrors the operator's mockup section labels — VUE D'ENSEMBLE / AGENTS /
 * SYSTÈME / MÉMOIRE — mapped onto Mirador's actual built-in tabs (see
 * `Mirador.tsx`'s `BUILTIN_TABS`). "Agents" holds the two agent-action tabs
 * (Approvals/Runs) — not called out by name in the sprint's suggested
 * grouping, but required by "don't drop any tab", and it's exactly the 4th
 * named group the mockup describes.
 *
 * FR/EN i18n (P1a): `label` here is a `TranslationKey` (see `@/lib/i18n`),
 * NOT display text — `buildNavGroups` stays language-agnostic, and the
 * caller (`Mirador.tsx`, which has `useT()` in scope) resolves each group's
 * `label` to display text right before rendering. This keeps `SidebarNav`
 * itself free of any i18n dependency.
 */
export const NAV_GROUP_ORDER: { label: string; values: readonly string[] }[] = [
  { label: 'nav.overview', values: ['analytics', 'cost'] },
  { label: 'nav.agents', values: ['approvals', 'runs'] },
  { label: 'nav.system', values: ['health', 'graph'] },
  { label: 'nav.memory', values: ['mem0'] },
]

export interface NavItem {
  value: string
  label: string
  Icon: LucideIcon
}

export interface NavGroup {
  label: string
  items: NavItem[]
}

/** Translation key for the fallback group holding any item not covered by
 * `NAV_GROUP_ORDER` — dynamic plugin-panel tabs land here by construction
 * (they're never in the static table), and it also protects a future
 * built-in tab added without updating `NAV_GROUP_ORDER` from silently
 * disappearing from the sidebar. */
export const FALLBACK_GROUP_LABEL = 'nav.panels'

/**
 * Groups a flat list of nav items per `NAV_GROUP_ORDER`, preserving each
 * group's declared value order. Any item whose `value` isn't listed in
 * `NAV_GROUP_ORDER` is appended to a trailing `FALLBACK_GROUP_LABEL` group
 * instead of being dropped — this is what keeps "every existing tab must
 * still be reachable" true even for tabs this static table doesn't know
 * about yet (dynamic plugin panels, or a new built-in tab).
 */
export function buildNavGroups(items: NavItem[]): NavGroup[] {
  const itemByValue = new Map(items.map((item) => [item.value, item]))
  const used = new Set<string>()
  const groups: NavGroup[] = []

  for (const { label, values } of NAV_GROUP_ORDER) {
    const groupItems: NavItem[] = []
    for (const value of values) {
      const item = itemByValue.get(value)
      if (item) {
        groupItems.push(item)
        used.add(value)
      }
    }
    if (groupItems.length > 0) {
      groups.push({ label, items: groupItems })
    }
  }

  const leftover = items.filter((item) => !used.has(item.value))
  if (leftover.length > 0) {
    groups.push({ label: FALLBACK_GROUP_LABEL, items: leftover })
  }

  return groups
}
