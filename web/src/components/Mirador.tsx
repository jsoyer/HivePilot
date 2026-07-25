import {
  Activity,
  Bot,
  CheckSquare,
  Cpu,
  Database,
  DollarSign,
  HeartPulse,
  LayoutDashboard,
  LayoutGrid,
  Menu,
  PlayCircle,
  Search,
  Users,
  Workflow,
  Zap,
} from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent } from '@/components/ui/tabs'
import { LanguageProvider, useT } from '@/lib/i18n'
import { fetchPanels } from '@/lib/mirador-api'
import { RoleProvider } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { CommandPalette } from './CommandPalette'
import { buildNavGroups, type NavItem } from './nav/nav-config'
import { LanguageToggle } from './nav/LanguageToggle'
import { SidebarNav } from './nav/SidebarNav'
import { StatusPills } from './nav/StatusPills'
import { ThemeToggle } from './nav/ThemeToggle'
import { AgentsView } from './views/AgentsView'
import { AnalyticsView } from './views/AnalyticsView'
import { ApprovalsView } from './views/ApprovalsView'
import { AutopilotView } from './views/AutopilotView'
import { CostView } from './views/CostView'
import { EfficiencyView } from './views/EfficiencyView'
import { GraphView } from './views/GraphView'
import { HealthView } from './views/HealthView'
import { HomeView } from './views/HomeView'
import { MemoryView } from './views/MemoryView'
import { ModelsView } from './views/ModelsView'
import { PanelView } from './views/PanelView'
import { RunBoardView } from './views/RunBoardView'

// FR/EN i18n (P1a): `labelKey` is a `TranslationKey` (see `@/lib/i18n`), NOT
// display text — resolved to the current language via `t()` where
// `navItems` is built below, in `MiradorShell` (which has `useT()` in
// scope, unlike this module-level constant).
// Mirador Home command-center sprint: Home is the FIRST built-in tab and
// the default landing view (`activeView` below). Its `Panel` here (`() =>
// <HomeView onNavigate={...} />`) is a thin wrapper only used by the
// generic `BUILTIN_TABS.map` render loop below — the wrapper is defined
// inline per-render (see `MiradorShell`) so it can close over the real
// `setActiveView`, keeping `HomeView` itself a plain, directly-testable
// component that takes `onNavigate` as an explicit prop rather than reading
// shell state from context.
const BUILTIN_TABS = [
  { value: 'home', labelKey: 'nav.home', Panel: HomeView, Icon: LayoutDashboard },
  { value: 'analytics', labelKey: 'nav.analytics', Panel: AnalyticsView, Icon: Activity },
  { value: 'cost', labelKey: 'nav.cost', Panel: CostView, Icon: DollarSign },
  // Mirador Spend section sprint: Models (per-model cost/tokens/success
  // rate, GET /v1/models) and Efficiency (Headroom + rtk token-savings
  // signals, GET /v1/efficiency) — grouped with Cost under "Spend" in
  // nav-config.ts's NAV_GROUP_ORDER.
  { value: 'models', labelKey: 'nav.models', Panel: ModelsView, Icon: Cpu },
  { value: 'efficiency', labelKey: 'nav.efficiency', Panel: EfficiencyView, Icon: Zap },
  { value: 'health', labelKey: 'nav.health', Panel: HealthView, Icon: HeartPulse },
  // Mirador "Agents" view sprint: per-role activity roster + lessons +
  // verdicts (GET /v1/agents, /v1/lessons, /v1/verdicts) — read-only for any
  // token, grouped with Health/Graph under "System" in nav-config.ts's
  // NAV_GROUP_ORDER (an observability surface over the fleet's roles).
  { value: 'agents', labelKey: 'nav.agents', Panel: AgentsView, Icon: Users },
  // Mirador Memory unification sprint: the formerly-separate Mem0 (search)
  // and Réalité (quality) built-ins merged into ONE `memory` item, plus a
  // new Growth tab (`/v1/memory/growth`) — see `MemoryView`'s own
  // docstring for the internal Quality/Growth/Search tab layout. Read-only
  // for any token; individual `/v1/memory/*` endpoints gate themselves.
  { value: 'memory', labelKey: 'nav.memory', Panel: MemoryView, Icon: Database },
  // Mirador actionable dashboard PRD, Sprint 2: read-only for any token,
  // Approve/Deny controls inside gate themselves on useRole().can('approve')
  // — see ApprovalsView.
  { value: 'approvals', labelKey: 'nav.approvals', Panel: ApprovalsView, Icon: CheckSquare },
  // Mirador Operate section PRD: Run Board (Kanban of runs, GET /v1/runs +
  // GET /v1/runs/{id} drill-down) — read-only for any token, the New Run
  // form and Stop controls inside gate themselves on useRole().can('run')
  // — see RunBoardView. Supersedes the old flat-table RunsView.
  { value: 'runs', labelKey: 'nav.runs', Panel: RunBoardView, Icon: PlayCircle },
  // Mirador Autopilot view sprint: GET /v1/autopilot (guarded objective
  // queue state — real-or-honest-empty, tenant-locked) + POST /v1/autopilot/
  // pause|resume — read-only for any token, the Pause/Resume control inside
  // gates itself on useRole().can('run') — see AutopilotView.
  { value: 'autopilot', labelKey: 'nav.autopilot', Panel: AutopilotView, Icon: Bot },
  // Mirador Graph View PRD, Sprint 3: read-only for any token; a graph
  // source's own min_role (data-dependent, GET /v1/graph/{source}) gates
  // itself inside GraphView, exactly like PanelView's per-panel min_role.
  { value: 'graph', labelKey: 'nav.graph', Panel: GraphView, Icon: Workflow },
] as const

/** A dynamic panel tab's `value` — prefixed so it can never collide with a
 * built-in tab's static `value` above. */
function panelTabValue(name: string): string {
  return `panel-${name}`
}

/**
 * The Mirador app shell — dark, grouped-sidebar insight dashboard (P0b:
 * sidebar nav + enriched header, upgrading the original flat top tab bar).
 * Eight built-in items (Home / Analytics / Cost / Health / Memory /
 * Approvals / Runs / Graph, wired to real HivePilot API data — `/v1/models`,
 * `/v1/efficiency`, `/v1/analytics/*`, `/v1/plugins/health`, `/v1/memories`,
 * `/v1/memory/*`, `/v1/approvals`, `/v1/runs`, `/v1/graph/*`, see `./views/*`
 * and `@/lib/mirador-api`) — Memory itself merges the FORMER separate Mem0
 * (search) and Réalité (quality) built-ins into one item with internal
 * Quality/Growth/Search tabs (see `MemoryView`'s own docstring) — grouped by
 * `./nav/nav-config`'s `buildNavGroups`, plus one DYNAMIC item per
 * plugin-contributed `panel` (Sprint 3 web surface, `GET /v1/panels`) —
 * ungrouped panels fall into a trailing "Panels" group automatically (see
 * `buildNavGroups`'s fallback). Each plugin panel lazy-fetches its own data
 * (`GET /v1/panels/{name}`) via `PanelView`, which handles its own
 * loading/error/empty/403 states — a panel that fails to load (or 403s for
 * the caller's role) never breaks the rest of the shell.
 *
 * The nav restructure (flat tabs -> grouped sidebar) is a UI change only —
 * `Tabs`'s uncontrolled `value` state (`defaultValue="analytics"`) is
 * exactly what it always was; `SidebarNav` renders the same
 * `TabsList`/`TabsTrigger` primitives, just grouped and styled as an
 * aside/drawer instead of a horizontal strip. See `SidebarNav`'s docstring
 * for why that's a single `TabsList`, not one per group.
 *
 * FR/EN i18n (P1a): the exported `Mirador` is just a `LanguageProvider`
 * wrap around the actual shell (`MiradorShell`) — `useT()` needs a provider
 * ABOVE it in the tree, so it can't be called from the same component that
 * defines the provider.
 *
 * ⌘K command palette (P1b): the `Tabs` root below is now CONTROLLED
 * (`value`/`onValueChange` instead of `defaultValue`) — `activeView` is
 * lifted up here so `CommandPalette`'s nav commands (rendered as a header
 * sibling, outside the `Tabs` tree) can set the SAME state `SidebarNav`'s
 * `TabsTrigger`s set, without needing access to Base UI's internal Tabs
 * context. This is a UI-state-plumbing change only — the sidebar's own
 * click-to-switch behavior is unchanged, it now just flows through
 * `onValueChange` instead of Base UI's uncontrolled default.
 *
 * Home command-center sprint: `activeView`'s initial value is now `'home'`
 * (was `'analytics'`) — Home is the default landing view, first in both
 * `BUILTIN_TABS` and `nav-config.ts`'s `NAV_GROUP_ORDER`.
 */
function MiradorShell() {
  const t = useT()
  const panelsState = useAsyncData(() => fetchPanels(), [])
  const pluginPanels = panelsState.status === 'success' ? panelsState.data.panels : []
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  // Mirador Home command-center sprint: Home is the default landing view
  // (was 'analytics').
  const [activeView, setActiveView] = useState('home')
  const [paletteOpen, setPaletteOpen] = useState(false)

  const navItems: NavItem[] = [
    ...BUILTIN_TABS.map((tab) => ({ value: tab.value, label: t(tab.labelKey), Icon: tab.Icon })),
    ...pluginPanels.map((panel) => ({
      value: panelTabValue(panel.name),
      label: panel.title,
      // Dynamic plugin panels have no fixed icon of their own (unlike the
      // built-ins above) — a generic grid glyph distinguishes them as
      // "extra" without implying a category `LayoutGrid` doesn't own.
      Icon: LayoutGrid,
    })),
  ]
  const navGroups = buildNavGroups(navItems).map((group) => ({ ...group, label: t(group.label) }))

  return (
    // Mirador actionable dashboard PRD, Sprint 1: RoleProvider fetches the
    // caller's own RBAC role (GET /v1/whoami) once on mount and exposes it
    // app-wide via useRole() — see @/lib/role-context. Provider wrap only;
    // no other logic changes here.
    <RoleProvider>
      {/* IA/Cyber identity: `bg-grid` paints the faint tech-grid + soft
       * radial glow across the whole shell (see `src/index.css`) — a
       * background-image only, so it never affects layout/scroll behavior.
       * The header stays a "glass panel" (semi-transparent + backdrop-blur,
       * unchanged from before this sprint) floating over that texture. */}
      <div className="bg-grid flex min-h-screen flex-col text-foreground">
        <header className="sticky top-0 z-20 flex flex-wrap items-center gap-3 border-b border-border bg-background/80 px-3 py-3 backdrop-blur-md sm:px-6">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            className="lg:hidden"
            data-testid="mobile-nav-trigger"
            aria-label={t('common.openNavigation')}
            onClick={() => setMobileNavOpen(true)}
          >
            <Menu className="size-4" />
          </Button>
          <div className="flex min-w-0 flex-col">
            <h1 className="truncate text-xl font-semibold">Mirador</h1>
            <span className="truncate text-xs text-muted-foreground">{t('header.subtitle')}</span>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-3">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="gap-2 text-muted-foreground"
              onClick={() => setPaletteOpen(true)}
              aria-label={t('header.search')}
            >
              <Search className="size-4" />
              <span className="hidden sm:inline">{t('header.search')}</span>
              <kbd className="hidden rounded border border-border bg-muted px-1 text-[10px] font-medium sm:inline">
                ⌘K
              </kbd>
            </Button>
            <StatusPills />
            <LanguageToggle />
            <ThemeToggle />
          </div>
        </header>
        <CommandPalette
          open={paletteOpen}
          onOpenChange={setPaletteOpen}
          navGroups={navGroups}
          onNavigate={setActiveView}
        />
        <Tabs
          value={activeView}
          onValueChange={(value) => setActiveView(String(value))}
          orientation="vertical"
          className="min-h-0 flex-1 items-stretch"
        >
          <SidebarNav
            groups={navGroups}
            mobileOpen={mobileNavOpen}
            onCloseMobile={() => setMobileNavOpen(false)}
          />
          <main className="min-w-0 flex-1 overflow-x-hidden p-3 sm:p-6">
            {BUILTIN_TABS.map(({ value, Panel }) => (
              <TabsContent key={value} value={value}>
                {/* HomeView is the one built-in tab that takes a prop
                 * (`onNavigate`, wired to the SAME `setActiveView` the
                 * sidebar/palette use, so its hero KPI deep links behave
                 * identically to clicking a nav item) — every other
                 * built-in Panel is prop-less. */}
                {value === 'home' ? <HomeView onNavigate={setActiveView} /> : <Panel />}
              </TabsContent>
            ))}
            {pluginPanels.map((panel) => (
              <TabsContent key={panel.name} value={panelTabValue(panel.name)}>
                <PanelView name={panel.name} title={panel.title} minRole={panel.min_role} />
              </TabsContent>
            ))}
          </main>
        </Tabs>
      </div>
    </RoleProvider>
  )
}

export function Mirador() {
  return (
    <LanguageProvider>
      <MiradorShell />
    </LanguageProvider>
  )
}
