import {
  Activity,
  CheckSquare,
  Database,
  DollarSign,
  HeartPulse,
  LayoutGrid,
  Menu,
  PlayCircle,
  Workflow,
} from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent } from '@/components/ui/tabs'
import { fetchPanels } from '@/lib/mirador-api'
import { RoleProvider } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { buildNavGroups, type NavItem } from './nav/nav-config'
import { SidebarNav } from './nav/SidebarNav'
import { StatusPills } from './nav/StatusPills'
import { ThemeToggle } from './nav/ThemeToggle'
import { AnalyticsView } from './views/AnalyticsView'
import { ApprovalsView } from './views/ApprovalsView'
import { CostView } from './views/CostView'
import { GraphView } from './views/GraphView'
import { HealthView } from './views/HealthView'
import { Mem0View } from './views/Mem0View'
import { PanelView } from './views/PanelView'
import { RunsView } from './views/RunsView'

const BUILTIN_TABS = [
  { value: 'analytics', label: 'Analytics', Panel: AnalyticsView, Icon: Activity },
  { value: 'cost', label: 'Cost', Panel: CostView, Icon: DollarSign },
  { value: 'health', label: 'Health', Panel: HealthView, Icon: HeartPulse },
  { value: 'mem0', label: 'Mem0', Panel: Mem0View, Icon: Database },
  // Mirador actionable dashboard PRD, Sprint 2: read-only for any token,
  // Approve/Deny controls inside gate themselves on useRole().can('approve')
  // — see ApprovalsView.
  { value: 'approvals', label: 'Approvals', Panel: ApprovalsView, Icon: CheckSquare },
  // Mirador actionable dashboard PRD, Sprint 3: read-only for any token,
  // the New Run form inside gates itself on useRole().can('run') — see
  // RunsView.
  { value: 'runs', label: 'Runs', Panel: RunsView, Icon: PlayCircle },
  // Mirador Graph View PRD, Sprint 3: read-only for any token; a graph
  // source's own min_role (data-dependent, GET /v1/graph/{source}) gates
  // itself inside GraphView, exactly like PanelView's per-panel min_role.
  { value: 'graph', label: 'Graph', Panel: GraphView, Icon: Workflow },
] as const

/** A dynamic panel tab's `value` — prefixed so it can never collide with a
 * built-in tab's static `value` above. */
function panelTabValue(name: string): string {
  return `panel-${name}`
}

/**
 * The Mirador app shell — dark, grouped-sidebar insight dashboard (P0b:
 * sidebar nav + enriched header, upgrading the original flat top tab bar).
 * Seven built-in items (Analytics / Cost / Health / Mem0 / Approvals / Runs
 * / Graph, wired to real HivePilot API data — `/v1/analytics/*`,
 * `/v1/plugins/health`, `/v1/memories`, `/v1/approvals`, `/v1/runs`,
 * `/v1/graph/*`, see `./views/*` and `@/lib/mirador-api`), grouped by
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
 */
export function Mirador() {
  const panelsState = useAsyncData(() => fetchPanels(), [])
  const pluginPanels = panelsState.status === 'success' ? panelsState.data.panels : []
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  const navItems: NavItem[] = [
    ...BUILTIN_TABS.map((tab) => ({ value: tab.value, label: tab.label, Icon: tab.Icon })),
    ...pluginPanels.map((panel) => ({
      value: panelTabValue(panel.name),
      label: panel.title,
      // Dynamic plugin panels have no fixed icon of their own (unlike the
      // built-ins above) — a generic grid glyph distinguishes them as
      // "extra" without implying a category `LayoutGrid` doesn't own.
      Icon: LayoutGrid,
    })),
  ]
  const navGroups = buildNavGroups(navItems)

  return (
    // Mirador actionable dashboard PRD, Sprint 1: RoleProvider fetches the
    // caller's own RBAC role (GET /v1/whoami) once on mount and exposes it
    // app-wide via useRole() — see @/lib/role-context. Provider wrap only;
    // no other logic changes here.
    <RoleProvider>
      <div className="flex min-h-screen flex-col bg-background text-foreground">
        <header className="sticky top-0 z-20 flex flex-wrap items-center gap-3 border-b border-border bg-background/95 px-3 py-3 backdrop-blur sm:px-6">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            className="lg:hidden"
            data-testid="mobile-nav-trigger"
            aria-label="Open navigation"
            onClick={() => setMobileNavOpen(true)}
          >
            <Menu className="size-4" />
          </Button>
          <div className="flex min-w-0 flex-col">
            <h1 className="truncate text-xl font-semibold">Mirador</h1>
            <span className="truncate text-xs text-muted-foreground">
              HivePilot insight dashboard
            </span>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-3">
            <StatusPills />
            <ThemeToggle />
          </div>
        </header>
        <Tabs defaultValue="analytics" orientation="vertical" className="min-h-0 flex-1 items-stretch">
          <SidebarNav
            groups={navGroups}
            mobileOpen={mobileNavOpen}
            onCloseMobile={() => setMobileNavOpen(false)}
          />
          <main className="min-w-0 flex-1 overflow-x-hidden p-3 sm:p-6">
            {BUILTIN_TABS.map(({ value, Panel }) => (
              <TabsContent key={value} value={value}>
                <Panel />
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
