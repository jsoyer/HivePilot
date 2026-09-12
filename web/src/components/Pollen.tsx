import {
  Activity,
  Bell,
  Bot,
  CheckSquare,
  Cpu,
  Database,
  DollarSign,
  Blocks,
  HeartPulse,
  Inbox,
  LayoutDashboard,
  LayoutGrid,
  Menu,
  PlayCircle,
  Search,
  Split,
  Users,
  UserRoundCog,
  Workflow,
  Zap,
  Gauge,
  MessagesSquare,
  Boxes,
  Waypoints,
  ServerCog,
  Plug,
  Sparkles,
  Cable,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent } from '@/components/ui/tabs'
import { ApiForbiddenError } from '@/lib/api'
import { LanguageProvider, useT } from '@/lib/i18n'
import { fetchPanels, fetchPluginsHealth, fetchRuns } from '@/lib/pollen-api'
import { RoleProvider } from '@/lib/role-context'
import { countShellIssues } from '@/lib/shell-issues'
import { useAsyncData } from '@/lib/use-async-data'
import { CommandPalette } from './CommandPalette'
import { buildNavGroups, pickNavItems, PLUS_NAV, PRIMARY_NAV, type NavItem } from './nav/nav-config'
import { IssuesChip } from './nav/IssuesChip'
import { OverflowMenu } from './nav/OverflowMenu'
import { SidebarNav } from './nav/SidebarNav'
import { ConversationsView } from './views/ConversationsView'
import { EspacesView } from './views/EspacesView'
import { AgentStudioView } from './views/AgentStudioView'
import { AgentsView } from './views/AgentsView'
import { AlertsView } from './views/AlertsView'
import { AnalyticsView } from './views/AnalyticsView'
import { ApprovalsView } from './views/ApprovalsView'
import { AutopilotView } from './views/AutopilotView'
import { CostView } from './views/CostView'
import { EfficiencyView } from './views/EfficiencyView'
import { GraphView } from './views/GraphView'
import { HealthView } from './views/HealthView'
import { HomeView } from './views/HomeView'
import { InboxView } from './views/InboxView'
import { MemoryView } from './views/MemoryView'
import { ModelsView } from './views/ModelsView'
import { CacheView } from './views/CacheView'
import { OrchestratorView } from './views/OrchestratorView'
import { ProvidersView } from './views/ProvidersView'
import { PluginsView } from './views/PluginsView'
import { McpView } from './views/McpView'
import { IntegrationsView } from './views/IntegrationsView'
import { PanelView } from './views/PanelView'
import { PartitionsView } from './views/PartitionsView'
import { RunBoardView } from './views/RunBoardView'
import { SkillsWorkshopView } from './views/SkillsWorkshopView'

const BUILTIN_TABS = [
  { value: 'home', labelKey: 'nav.home', Panel: HomeView, Icon: LayoutDashboard },
  { value: 'analytics', labelKey: 'nav.analytics', Panel: AnalyticsView, Icon: Activity },
  { value: 'cost', labelKey: 'nav.cost', Panel: CostView, Icon: DollarSign },
  { value: 'models', labelKey: 'nav.models', Panel: ModelsView, Icon: Cpu },
  { value: 'providers', labelKey: 'nav.providers', Panel: ProvidersView, Icon: ServerCog },
  { value: 'efficiency', labelKey: 'nav.efficiency', Panel: EfficiencyView, Icon: Zap },
  { value: 'health', labelKey: 'nav.health', Panel: HealthView, Icon: HeartPulse },
  { value: 'plugins', labelKey: 'nav.plugins', Panel: PluginsView, Icon: Blocks },
  { value: 'mcp', labelKey: 'nav.mcp', Panel: McpView, Icon: Plug },
  { value: 'integrations', labelKey: 'nav.integrations', Panel: IntegrationsView, Icon: Cable },
  { value: 'cache', labelKey: 'nav.cache', Panel: CacheView, Icon: Gauge },
  { value: 'agents', labelKey: 'nav.agents', Panel: AgentsView, Icon: Users },
  { value: 'studio', labelKey: 'nav.studio', Panel: AgentStudioView, Icon: UserRoundCog },
  { value: 'conversations', labelKey: 'nav.conversations', Panel: ConversationsView, Icon: MessagesSquare },
  { value: 'inbox', labelKey: 'nav.inbox', Panel: InboxView, Icon: Inbox },
  { value: 'memory', labelKey: 'nav.memory', Panel: MemoryView, Icon: Database },
  { value: 'approvals', labelKey: 'nav.approvals', Panel: ApprovalsView, Icon: CheckSquare },
  { value: 'runs', labelKey: 'nav.runs', Panel: RunBoardView, Icon: PlayCircle },
  { value: 'spaces', labelKey: 'nav.spaces', Panel: EspacesView, Icon: Boxes },
  { value: 'orchestrator', labelKey: 'nav.orchestrator', Panel: OrchestratorView, Icon: Waypoints },
  { value: 'autopilot', labelKey: 'nav.autopilot', Panel: AutopilotView, Icon: Bot },
  { value: 'partitions', labelKey: 'nav.partitions', Panel: PartitionsView, Icon: Split },
  { value: 'workshop', labelKey: 'nav.workshop', Panel: SkillsWorkshopView, Icon: Sparkles },
  { value: 'graph', labelKey: 'nav.graph', Panel: GraphView, Icon: Workflow },
  { value: 'alerts', labelKey: 'nav.alerts', Panel: AlertsView, Icon: Bell },
] as const

function panelTabValue(name: string): string {
  return `panel-${name}`
}

function PollenShell() {
  const t = useT()
  const panelsState = useAsyncData(() => fetchPanels(), [])
  const pluginPanels = panelsState.status === 'success' ? panelsState.data.panels : []
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [activeView, setActiveView] = useState('inbox')
  const [paletteOpen, setPaletteOpen] = useState(false)

  const health = useAsyncData(() => fetchPluginsHealth(), [])
  const runs = useAsyncData(async () => {
    try {
      return await fetchRuns(50)
    } catch (error) {
      if (error instanceof ApiForbiddenError) {
        return []
      }
      throw error
    }
  }, [])
  const issuesReady = health.status !== 'loading' && runs.status !== 'loading'
  const issueCount = countShellIssues(
    health.status === 'success' ? health.data.plugins : [],
    runs.status === 'success' ? runs.data : [],
  )

  const navItems: NavItem[] = [
    ...BUILTIN_TABS.map((tab) => ({ value: tab.value, label: t(tab.labelKey), Icon: tab.Icon })),
    ...pluginPanels.map((panel) => ({
      value: panelTabValue(panel.name),
      label: panel.title,
      Icon: LayoutGrid,
    })),
  ]
  const navGroups = buildNavGroups(navItems).map((group) => ({ ...group, label: t(group.label) }))
  const primaryItems = useMemo(() => pickNavItems(navItems, PRIMARY_NAV, t), [navItems, t])
  const plusItems = useMemo(() => pickNavItems(navItems, PLUS_NAV, t), [navItems, t])

  return (
    <RoleProvider>
      <div className="flex min-h-screen flex-col bg-background text-foreground">
        <header className="sticky top-0 z-20 flex flex-wrap items-center gap-3 border-b border-border bg-background py-3 pr-[max(0.75rem,env(safe-area-inset-right))] pb-3 pl-[max(0.75rem,env(safe-area-inset-left))] pt-[max(0.75rem,env(safe-area-inset-top))] sm:px-6">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            className="touch-target md:hidden"
            data-testid="mobile-nav-trigger"
            aria-label={t('common.openNavigation')}
            onClick={() => setMobileNavOpen(true)}
          >
            <Menu className="size-4" />
          </Button>
          <div className="flex min-w-0 items-center gap-2.5">
            <span
              data-slot="brand-mark"
              aria-hidden="true"
              className="size-7 shrink-0 rounded-full bg-primary"
            />
            <h1 className="truncate text-base font-semibold">Pollen</h1>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="touch-target h-8 gap-2 rounded-full text-muted-foreground"
              onClick={() => setPaletteOpen(true)}
              aria-label={t('header.search')}
            >
              <Search className="size-4" />
              <span className="hidden sm:inline">{t('header.search')}</span>
              <kbd className="hidden rounded border border-border bg-muted px-1 text-[10px] font-medium sm:inline">
                ⌘K
              </kbd>
            </Button>
            <IssuesChip count={issueCount} ready={issuesReady} onClick={() => setActiveView('alerts')} />
            <OverflowMenu />
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
            primary={primaryItems}
            plusItems={plusItems}
            plusLabel={t('nav.plus')}
            plusHint={t('nav.plusHint')}
            alertsCount={issueCount}
            mobileOpen={mobileNavOpen}
            onCloseMobile={() => setMobileNavOpen(false)}
          />
          <main className="min-w-0 flex-1 overflow-x-hidden p-4 sm:p-8">
            {BUILTIN_TABS.map(({ value, Panel }) => (
              <TabsContent key={value} value={value}>
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

export function Pollen() {
  return (
    <LanguageProvider>
      <PollenShell />
    </LanguageProvider>
  )
}
