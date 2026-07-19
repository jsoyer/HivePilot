import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { fetchPanels } from '@/lib/mirador-api'
import { RoleProvider } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { AnalyticsView } from './views/AnalyticsView'
import { ApprovalsView } from './views/ApprovalsView'
import { CostView } from './views/CostView'
import { GraphView } from './views/GraphView'
import { HealthView } from './views/HealthView'
import { Mem0View } from './views/Mem0View'
import { PanelView } from './views/PanelView'
import { RunsView } from './views/RunsView'

const BUILTIN_TABS = [
  { value: 'analytics', label: 'Analytics', Panel: AnalyticsView },
  { value: 'cost', label: 'Cost', Panel: CostView },
  { value: 'health', label: 'Health', Panel: HealthView },
  { value: 'mem0', label: 'Mem0', Panel: Mem0View },
  // Mirador actionable dashboard PRD, Sprint 2: read-only for any token,
  // Approve/Deny controls inside gate themselves on useRole().can('approve')
  // — see ApprovalsView.
  { value: 'approvals', label: 'Approvals', Panel: ApprovalsView },
  // Mirador actionable dashboard PRD, Sprint 3: read-only for any token,
  // the New Run form inside gates itself on useRole().can('run') — see
  // RunsView.
  { value: 'runs', label: 'Runs', Panel: RunsView },
  // Mirador Graph View PRD, Sprint 3: read-only for any token; a graph
  // source's own min_role (data-dependent, GET /v1/graph/{source}) gates
  // itself inside GraphView, exactly like PanelView's per-panel min_role.
  { value: 'graph', label: 'Graph', Panel: GraphView },
] as const

/** A dynamic panel tab's `value` — prefixed so it can never collide with a
 * built-in tab's static `value` above. */
function panelTabValue(name: string): string {
  return `panel-${name}`
}

/**
 * The Mirador app shell — dark, tabbed insight dashboard. Four built-in tabs
 * (Analytics / Cost / Health / Mem0, wired to real HivePilot API data —
 * `/v1/analytics/*`, `/v1/plugins/health`, `/v1/memories`, see `./views/*`
 * and `@/lib/mirador-api`), plus one DYNAMIC tab per plugin-contributed
 * `panel` (Sprint 3 web surface, `GET /v1/panels`) appended after them.
 * Each plugin panel tab lazy-fetches its own data (`GET /v1/panels/{name}`)
 * via `PanelView`, which handles its own loading/error/empty/403 states —
 * a panel that fails to load (or 403s for the caller's role) never breaks
 * the rest of the shell.
 */
export function Mirador() {
  const panelsState = useAsyncData(() => fetchPanels(), [])
  const pluginPanels = panelsState.status === 'success' ? panelsState.data.panels : []

  return (
    // Mirador actionable dashboard PRD, Sprint 1: RoleProvider fetches the
    // caller's own RBAC role (GET /v1/whoami) once on mount and exposes it
    // app-wide via useRole() — see @/lib/role-context. Provider wrap only;
    // no other logic changes here.
    <RoleProvider>
      <div className="min-h-screen bg-background p-6 text-foreground">
        <header className="mb-6 flex items-center gap-3">
          <h1 className="text-xl font-semibold">Mirador</h1>
          <Badge variant="secondary">HivePilot insight dashboard</Badge>
        </header>
        <Tabs defaultValue="analytics">
          <TabsList>
            {BUILTIN_TABS.map((tab) => (
              <TabsTrigger key={tab.value} value={tab.value}>
                {tab.label}
              </TabsTrigger>
            ))}
            {pluginPanels.map((panel) => (
              <TabsTrigger key={panel.name} value={panelTabValue(panel.name)}>
                {panel.title}
              </TabsTrigger>
            ))}
          </TabsList>
          {BUILTIN_TABS.map(({ value, Panel }) => (
            <TabsContent key={value} value={value} className="mt-4">
              <Panel />
            </TabsContent>
          ))}
          {pluginPanels.map((panel) => (
            <TabsContent key={panel.name} value={panelTabValue(panel.name)} className="mt-4">
              <PanelView name={panel.name} title={panel.title} minRole={panel.min_role} />
            </TabsContent>
          ))}
        </Tabs>
      </div>
    </RoleProvider>
  )
}
