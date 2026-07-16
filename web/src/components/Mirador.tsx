import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

const TABS = [
  { value: 'analytics', label: 'Analytics' },
  { value: 'cost', label: 'Cost' },
  { value: 'health', label: 'Health' },
  { value: 'mem0', label: 'Mem0' },
] as const

function PlaceholderPanel({ label }: { label: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {label}
          <Badge variant="outline">Sprint 3</Badge>
        </CardTitle>
        <CardDescription>Wired in Sprint 3.</CardDescription>
      </CardHeader>
      <CardContent className="text-sm text-muted-foreground">
        Real {label.toLowerCase()} data lands here once this view is wired up.
      </CardContent>
    </Card>
  )
}

/**
 * The Mirador app shell — dark, tabbed insight dashboard (Analytics / Cost /
 * Health / Mem0). Placeholder panels only in this sprint; Sprint 3 wires
 * each tab to real `/v1/analytics/*`, `/v1/plugins/health`, and mem0 data
 * via `@/lib/api`'s `apiFetch`.
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
        {TABS.map((tab) => (
          <TabsContent key={tab.value} value={tab.value} className="mt-4">
            <PlaceholderPanel label={tab.label} />
          </TabsContent>
        ))}
      </Tabs>
    </div>
  )
}
