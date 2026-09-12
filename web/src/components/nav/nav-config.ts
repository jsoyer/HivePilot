import type { LucideIcon } from 'lucide-react'

/**
 * Grouped navigation for the ⌘K palette (and leftover reachability).
 * The visible sidebar is NOT this table — see `PRIMARY_NAV` / `PLUS_NAV`.
 *
 * FR/EN i18n: `label` here is a `TranslationKey`, not display text.
 * `buildNavGroups` stays language-agnostic; the caller resolves labels.
 */
export const NAV_GROUP_ORDER: { label: string; values: readonly string[] }[] = [
  { label: 'nav.atAGlance', values: ['home'] },
  {
    label: 'nav.operate',
    values: ['inbox', 'spaces', 'orchestrator', 'runs', 'approvals', 'alerts', 'partitions', 'workshop', 'autopilot'],
  },
  { label: 'nav.spend', values: ['cost', 'models', 'providers', 'efficiency'] },
  { label: 'nav.overview', values: ['analytics'] },
  { label: 'nav.memory', values: ['memory'] },
  {
    label: 'nav.system',
    values: ['health', 'plugins', 'mcp', 'integrations', 'cache', 'agents', 'studio', 'conversations', 'graph'],
  },
]

/**
 * The four sidebar doors (redesign mock A). Internal tab values stay stable
 * (`inbox` is the former `chat` landing) so existing views keep working.
 */
export const PRIMARY_NAV: readonly { value: string; labelKey: string }[] = [
  { value: 'inbox', labelKey: 'nav.inbox' },
  { value: 'approvals', labelKey: 'nav.approvals' },
  { value: 'runs', labelKey: 'nav.runs' },
  { value: 'alerts', labelKey: 'nav.alerts' },
]

/**
 * Plus tray at the bottom of the sidebar. Spend → Cost, System → Health;
 * the rest of each group stays in ⌘K.
 */
export const PLUS_NAV: readonly { value: string; labelKey: string }[] = [
  { value: 'spaces', labelKey: 'nav.rooms' },
  { value: 'orchestrator', labelKey: 'nav.orchestrator' },
  { value: 'cost', labelKey: 'nav.spend' },
  { value: 'memory', labelKey: 'nav.memory' },
  { value: 'health', labelKey: 'nav.system' },
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

/** Translation key for leftover items (plugin panels, or a new built-in). */
export const FALLBACK_GROUP_LABEL = 'nav.panels'

export function pickNavItems(
  items: NavItem[],
  specs: readonly { value: string; labelKey: string }[],
  labelFor: (labelKey: string) => string,
): NavItem[] {
  const itemByValue = new Map(items.map((item) => [item.value, item]))
  const picked: NavItem[] = []
  for (const spec of specs) {
    const item = itemByValue.get(spec.value)
    if (item) {
      picked.push({ ...item, label: labelFor(spec.labelKey) })
    }
  }
  return picked
}

/**
 * Groups a flat list of nav items per `NAV_GROUP_ORDER`, preserving each
 * group's declared value order. Leftovers (plugin panels, unknown tabs)
 * land in a trailing `FALLBACK_GROUP_LABEL` group instead of disappearing.
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
