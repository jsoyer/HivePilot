import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { AnalyticsView } from './views/AnalyticsView'
import { CostView } from './views/CostView'
import { HealthView } from './views/HealthView'
import { Mem0View } from './views/Mem0View'

const TABS = [
  { value: 'analytics', label: 'Analytics', Panel: AnalyticsView },
  { value: 'cost', label: 'Cost', Panel: CostView },
  { value: 'health', label: 'Health', Panel: HealthView },
  { value: 'mem0', label: 'Mem0', Panel: Mem0View },
] as const

/**
 * The Mirador app shell — dark, tabbed insight dashboard (Analytics / Cost /
 * Health / Mem0). Each tab is wired to real HivePilot API data (Sprint 3):
 * `/v1/analytics/*` for Analytics/Cost, `/v1/plugins/health` for Health, and
 * `/v1/memories` for Mem0 — see `./views/*` and `@/lib/mirador-api`.
 */
export function Mirador() {
  return (
    <div className="min-h-screen bg-background p-6 text-foreground">
      <header className="mb-6 flex items-center gap-3">
        <h1 className="text-xl font-semibold">Mirador</h1>
        <Badge variant="secondary">HivePilot insight dashboard</Badge>
      </header>
      <Tabs defaultValue="analytics">
        <TabsList>
          {TABS.map((tab) => (
            <TabsTrigger key={tab.value} value={tab.value}>
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
        {TABS.map(({ value, Panel }) => (
          <TabsContent key={value} value={value} className="mt-4">
            <Panel />
          </TabsContent>
        ))}
      </Tabs>
    </div>
  )
}
