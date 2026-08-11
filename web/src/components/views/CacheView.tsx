import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useT } from '@/lib/i18n'
import { fetchCacheReport, type CacheReport } from '@/lib/pollen-api'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

/**
 * Prompt-cache economics.
 *
 * **A median and a count, never a fleet ratio.** Cache creation is billed at
 * 1.25x base input and a read at 0.1x, so a prefix read fewer than once cost
 * more than sending it uncached. A fleet-wide hit rate is dominated by
 * whichever session read the most: measured here, 85% coexisted with 1.7M
 * tokens of creation never read back, and every aggregate looked fine.
 *
 * Fed by the agent CLI's own OTLP metrics rather than our per-step
 * bookkeeping, which only sees the calls we route ourselves.
 */

function Stat({
  label,
  value,
  hint,
  tone = 'neutral',
}: {
  label: string
  value: string
  hint?: string
  tone?: 'neutral' | 'bad'
}) {
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-border p-4">
      <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span
        className={`text-2xl font-semibold tabular-nums ${tone === 'bad' ? 'text-destructive' : ''}`}
      >
        {value}
      </span>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </div>
  )
}

function Report({ data }: { data: CacheReport }) {
  const t = useT()

  if (data.sessions === 0) {
    // Ingest is opt-in. "No data" is not "healthy" and must not render as it.
    return <p className="text-sm text-muted-foreground">{t('cache.noTelemetry')}</p>
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          label={t('cache.median')}
          value={`${data.median_amortisation.toFixed(2)}×`}
          hint={t('cache.breakEven')}
          tone={data.median_amortisation < 1 ? 'bad' : 'neutral'}
        />
        <Stat
          label={t('cache.belowOne')}
          value={`${data.below_one} / ${data.sessions}`}
          hint={t('cache.belowOneHint')}
          tone={data.below_one > 0 ? 'bad' : 'neutral'}
        />
        <Stat
          label={t('cache.wasted')}
          value={data.wasted_tokens.toLocaleString()}
          hint={t('cache.wastedHint')}
          tone={data.wasted_tokens > 0 ? 'bad' : 'neutral'}
        />
        <Stat label={t('cache.sessions')} value={String(data.sessions)} />
      </div>

      {data.worst && (
        <div className="flex flex-col gap-2 rounded-lg border border-border bg-muted/30 p-4">
          {/* A count says to look; the worst case says where. */}
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-semibold uppercase text-muted-foreground">
              {t('cache.worst')}
            </span>
            {data.worst.model && <Badge variant="outline">{data.worst.model}</Badge>}
            <Badge variant={data.worst.amortisation < 1 ? 'destructive' : 'outline'}>
              {data.worst.amortisation.toFixed(2)}×
            </Badge>
          </div>
          <code className="overflow-x-auto text-xs">{data.worst.session_id}</code>
          <span className="text-sm text-muted-foreground">
            {t('cache.worstDetail')
              .replace('{created}', data.worst.created.toLocaleString())
              .replace('{read}', data.worst.read.toLocaleString())}
          </span>
        </div>
      )}
    </div>
  )
}

export function CacheView() {
  const t = useT()
  const report = useAsyncData(() => fetchCacheReport(30), [])

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('cache.title')}</CardTitle>
        <CardDescription>{t('cache.description')}</CardDescription>
      </CardHeader>
      <CardContent>
        <AsyncSection state={report} isEmpty={() => false}>
          {(data) => <Report data={data} />}
        </AsyncSection>
      </CardContent>
    </Card>
  )
}
