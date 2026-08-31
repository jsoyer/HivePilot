import type { LucideIcon } from 'lucide-react'

/**
 * Grouped sidebar navigation (Pollen dashboard upgrade, P0b).
 * Mirrors the operator's mockup section labels — VUE D'ENSEMBLE / AGENTS /
 * SYSTÈME / MÉMOIRE — mapped onto Pollen's actual built-in tabs (see
 * `Pollen.tsx`'s `BUILTIN_TABS`).
 *
 * FR/EN i18n (P1a): `label` here is a `TranslationKey` (see `@/lib/i18n`),
 * NOT display text — `buildNavGroups` stays language-agnostic, and the
 * caller (`Pollen.tsx`, which has `useT()` in scope) resolves each group's
 * `label` to display text right before rendering. This keeps `SidebarNav`
 * itself free of any i18n dependency.
 *
 * Mirador Operate section sprint (Run Board + run detail, demote the
 * node-graph): the operator's core complaint driving this sprint was "the
 * pipeline graphs are useless" — `GraphView` (plugins/pipeline/skills
 * topology) is kept (never deleted, still fully reachable), but demoted out
 * of a prominent top-level slot. Two changes from the group table below:
 *  1. The former "Agents" group (Approvals/Runs) is renamed "Operate" and
 *     moved to the SECOND position, right after Home — Runs (now a Kanban
 *     Run Board, not a flat table) is the primary Operate experience.
 *  2. "System" (Health/Graph) — Graph's home — moves to the LAST position,
 *     after Memory, so it's still one click away but no longer front-and-
 *     center. Health stays paired with it (unchanged pairing, just demoted
 *     as a unit).
 */
export const NAV_GROUP_ORDER: { label: string; values: readonly string[] }[] = [
  // Mirador Home command-center sprint: Home is the new default landing
  // view, called out in its own leading group (not folded into "Overview")
  // so it always renders first, above every other section.
  { label: 'nav.atAGlance', values: ['home'] },
  // Mirador Operate section sprint: Runs (Kanban Run Board — the operator's
  // actionable "what's happening right now" view) + Approvals, right after
  // Home. Replaces the "Agents" group at this same top-adjacent slot -- the
  // rename better describes what the group is FOR (operating runs), not
  // just which role triggers them.
  // Mirador Autopilot view sprint: Autopilot (GET/POST /v1/autopilot — the
  // guarded objective queue's control surface) joins the same group — it's
  // an "operate" concern (pause/resume, watch what's queued/dispatched),
  // not a Spend/Overview/System one.
  // Propose -> ratify -> dispatch PRD, Sprint 4: Partitions (GET
  // /v1/partitions + the ratification gate) is an Operate concern — it is
  // where an operator decides whether N agents start — so it sits next to
  // Approvals rather than in Overview or System.
  { label: 'nav.operate', values: ['spaces', 'runs', 'approvals', 'partitions', 'autopilot'] },
  // Mirador Spend section sprint: Cost moves out of "Overview" into its own
  // "Spend" group alongside the two new views (Models/Efficiency) — the
  // operator's complaint this sprint answers ("la conso marche pas, rien
  // sur les modèles, rien de headroom/rtk") is specifically about spend
  // visibility, so it gets a dedicated, discoverable section rather than
  // being folded into general analytics.
  { label: 'nav.spend', values: ['cost', 'models', 'efficiency'] },
  { label: 'nav.overview', values: ['analytics'] },
  // Mirador Memory unification sprint: the formerly-separate Mem0 (search)
  // and memory-quality built-ins merged into ONE `memory` item backed by
  // `MemoryView`'s internal Quality/Growth/Search tabs (see `Pollen.tsx`'s
  // `BUILTIN_TABS`) — a single-entry-with-tabs destination reads cleaner
  // than three separate top-level nav items for what is really one
  // subject (memory).
  { label: 'nav.memory', values: ['memory'] },
  // Mirador Operate section sprint: demoted to LAST — Graph (plugins/
  // pipeline/skills topology) is still fully reachable (sidebar/drawer/⌘K),
  // just no longer a prominent top-level destination now that the Run Board
  // is the primary "what's happening" view. Health stays paired with it.
  // Mirador "Agents" view sprint: Agents (GET /v1/agents+/lessons+/verdicts
  // — per-role activity/cost/lessons/verdicts) joins this group too — it's
  // an observability surface over the fleet's roles, same category as
  // Health (plugin health) and Graph (topology), not a Spend/Operate
  // concern. Placed before Graph (still fully reachable, just kept last per
  // its own demotion above).
  // `conversations` sits beside `agents`: same subject seen two ways -- the
  // roster of who ran, and the thread of what they actually said.
  { label: 'nav.system', values: ['health', 'plugins', 'cache', 'agents', 'conversations', 'graph'] },
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
