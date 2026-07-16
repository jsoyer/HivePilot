import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { fetchPluginsHealth, type PluginHealthStatus } from '@/lib/mirador-api'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

const STATUS_VARIANT: Record<PluginHealthStatus, 'secondary' | 'outline' | 'destructive'> = {
  ok: 'secondary',
  degraded: 'outline',
  error: 'destructive',
}

/** Health tab — `GET /v1/plugins/health`, one badge per plugin. */
export function HealthView() {
  const health = useAsyncData(() => fetchPluginsHealth(), [])

  return (
    <Card>
      <CardHeader>
        <CardTitle>Plugin health</CardTitle>
        <CardDescription>Process-global plugin status, same as `hivepilot plugins health`</CardDescription>
      </CardHeader>
      <CardContent>
        <AsyncSection
          state={health}
          isEmpty={(data) => data.plugins.length === 0}
          emptyMessage="No plugins registered."
        >
          {(data) => (
            <ul className="flex flex-col gap-2">
              {data.plugins.map((plugin) => (
                <li
                  key={plugin.name}
                  className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-2"
                >
                  <span className="font-medium">{plugin.name}</span>
                  <Badge variant={STATUS_VARIANT[plugin.status]}>{plugin.status}</Badge>
                  {plugin.detail && (
                    <span className="text-sm text-muted-foreground">{plugin.detail}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </AsyncSection>
      </CardContent>
    </Card>
  )
}
