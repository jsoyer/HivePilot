import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useT } from '@/lib/i18n'
import {
  fetchMemoryBackends,
  type MemoryBackendStats,
  type MemoryBackendsResponse,
} from '@/lib/pollen-api'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

/**
 * The memory backends, side by side on the same counters.
 *
 * They are **not** interchangeable and this panel says so. Measured on real
 * steps, their recalls overlap by 2–4%: mem0 answers semantically from a
 * third-party store keyed by project:task:role; Obsidian answers from
 * role-scoped notes that never leave the host. Showing only one would invite
 * cutting a live capability.
 *
 * The KPI is **empty recalls**, not a result count. 115 of 150 production
 * searches returned exactly 5 — the top-k cap, which says nothing about
 * quality. How often a recall came back with nothing is the honest signal and
 * the only one both backends can be compared on.
 *
 * An idle backend renders as measured-and-idle, never as absent. Obsidian sat
 * at zero for months purely because nothing recorded it, and a zero meaning
 * "never measured" is indistinguishable from one meaning "useless".
 */

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="text-xl font-semibold tabular-nums">{value}</span>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </div>
  )
}

function BackendPanel({
  name,
  stats,
  egress,
}: {
  name: string
  stats: MemoryBackendStats
  egress: boolean
}) {
  const t = useT()
  const emptyShare =
    stats.searches > 0 ? Math.round((stats.empty_searches / stats.searches) * 100) : 0

  return (
    <Card data-testid={`memory-backend-${name}`} className="flex flex-col">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="capitalize">{name}</CardTitle>
          <Badge variant={egress ? 'destructive' : 'outline'}>
            {egress ? t('memoryBackends.egressYes') : t('memoryBackends.egressNo')}
          </Badge>
        </div>
        <CardDescription>{t(`memoryBackends.about.${name}` as never)}</CardDescription>
      </CardHeader>
      <CardContent className="mt-auto flex flex-col gap-4">
        <div className="grid grid-cols-2 gap-4">
          <Metric label={t('memoryBackends.recalls')} value={stats.searches.toLocaleString()} />
          <Metric
            label={t('memoryBackends.emptyRecalls')}
            value={`${stats.empty_searches.toLocaleString()} (${emptyShare}%)`}
            hint={t('memoryBackends.emptyHint')}
          />
          <Metric label={t('memoryBackends.stores')} value={stats.stores.toLocaleString()} />
          <Metric label={t('memoryBackends.actors')} value={String(stats.actors)} />
        </div>
        <span className="text-xs text-muted-foreground">
          {stats.last_activity
            ? `${t('memoryBackends.lastActivity')} ${stats.last_activity}`
            : t('memoryBackends.neverUsed')}
        </span>
      </CardContent>
    </Card>
  )
}

function Panels({ data }: { data: MemoryBackendsResponse }) {
  const t = useT()
  const names = Object.keys(data.backends).sort()

  return (
    <div className="flex flex-col gap-3">
      {/* Stated once, above both: they complement, they do not replace. */}
      <p className="text-sm text-muted-foreground">{t('memoryBackends.notInterchangeable')}</p>
      <div className="grid gap-3 lg:grid-cols-2">
        {names.map((name) => (
          <BackendPanel
            key={name}
            name={name}
            stats={data.backends[name]}
            egress={Boolean(data.egress[name])}
          />
        ))}
      </div>
    </div>
  )
}

export function MemoryBackendsView() {
  const data = useAsyncData(() => fetchMemoryBackends(30), [])

  return (
    <AsyncSection state={data} isEmpty={() => false}>
      {(loaded) => <Panels data={loaded} />}
    </AsyncSection>
  )
}
