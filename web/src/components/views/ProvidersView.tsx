import { ServerCog, TriangleAlert } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { MetricReadout } from '@/components/dashboard/MetricReadout'
import { formatAge, formatTimestamp } from '@/lib/format-time'
import { useT } from '@/lib/i18n'
import {
  fetchAnalyticsCost,
  fetchProviderFallbacks,
  type ProviderFallback,
} from '@/lib/pollen-api'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

/** Fixed 30-day spend window (matches ModelsView); fallbacks use a 24h window
 * since they are a "what's failing right now" signal. */
const SPEND_DAYS = 30
const FALLBACK_HOURS = 24

const NO_FALLBACKS: ProviderFallback[] = []

function formatTokens(n: number): string {
  return n.toLocaleString('en-US')
}

function formatCost(n: number): string {
  return `$${n.toFixed(3)}`
}

interface ProviderRow {
  provider: string
  cost_usd: number
  input_tokens: number
  output_tokens: number
  fallback: ProviderFallback | null
}

/**
 * Providers panel (HP-73) — the visible companion to HP-70's model fallback.
 * Per provider it shows REAL recorded spend + tokens (`/v1/analytics/cost`
 * `by_provider`) and, when present, a fallback badge from
 * `/v1/providers/fallbacks` (how many times it fell over in the last 24h, when
 * last, and why). Nothing here is estimated beyond the existing cost envelope:
 * a provider with no recorded activity simply doesn't appear, and quota
 * %/runway (which needs a real provider API) is deliberately not shown.
 */
export function ProvidersView() {
  const t = useT()
  const costState = useAsyncData(() => fetchAnalyticsCost(SPEND_DAYS), [])
  const fallbackState = useAsyncData(() => fetchProviderFallbacks(FALLBACK_HOURS), [])
  const fallbacks = fallbackState.status === 'success' ? fallbackState.data.providers : NO_FALLBACKS

  function fallbackReason(reason: string | null): string {
    if (reason === 'quota') return t('providers.reason.quota')
    if (reason === 'unavailable') return t('providers.reason.unavailable')
    return reason ?? ''
  }

  return (
    <Card data-testid="providers-view">
      <CardHeader>
        <CardTitle>{t('nav.providers')}</CardTitle>
        <CardDescription>{t('providers.description')}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <AsyncSection
          state={costState}
          emptyMessage={t('providers.noData')}
          isEmpty={(data) => data.by_provider.length === 0 && fallbacks.length === 0}
        >
          {(cost) => {
            const byName = new Map<string, ProviderRow>()
            for (const p of cost.by_provider) {
              byName.set(p.provider, {
                provider: p.provider,
                cost_usd: p.cost_usd,
                input_tokens: p.input_tokens,
                output_tokens: p.output_tokens,
                fallback: null,
              })
            }
            // A provider that fell back but recorded no spend in-window still
            // matters — surface it as a fallback-only row rather than hiding it.
            for (const fb of fallbacks) {
              const row = byName.get(fb.provider)
              if (row) row.fallback = fb
              else
                byName.set(fb.provider, {
                  provider: fb.provider,
                  cost_usd: 0,
                  input_tokens: 0,
                  output_tokens: 0,
                  fallback: fb,
                })
            }
            const rows = [...byName.values()].sort((a, b) => b.cost_usd - a.cost_usd)
            const fallbackCount = fallbacks.reduce((sum, f) => sum + f.count, 0)

            return (
              <>
                <div className="grid gap-3 sm:grid-cols-3">
                  <MetricReadout
                    label={t('providers.overallSpend')}
                    value={formatCost(cost.overall.cost_usd)}
                  />
                  <MetricReadout
                    label={t('providers.providerCount')}
                    value={String(cost.by_provider.length)}
                  />
                  <MetricReadout
                    label={t('providers.fallbackCount', { hours: FALLBACK_HOURS })}
                    value={String(fallbackCount)}
                  />
                </div>

                <Table data-testid="providers-table">
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t('providers.provider')}</TableHead>
                      <TableHead className="text-right">{t('providers.cost')}</TableHead>
                      <TableHead className="text-right">{t('providers.tokens')}</TableHead>
                      <TableHead>{t('providers.status')}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {rows.map((row) => (
                      <TableRow key={row.provider} data-testid={`providers-row-${row.provider}`}>
                        <TableCell className="font-medium">
                          <span className="flex items-center gap-1.5">
                            <ServerCog className="size-4 text-muted-foreground" />
                            {row.provider}
                          </span>
                        </TableCell>
                        <TableCell className="metric-mono text-right">
                          {formatCost(row.cost_usd)}
                        </TableCell>
                        <TableCell className="metric-mono text-right text-muted-foreground">
                          {formatTokens(row.input_tokens + row.output_tokens)}
                        </TableCell>
                        <TableCell>
                          {row.fallback ? (
                            <Badge
                              variant="outline"
                              data-testid={`providers-fallback-${row.provider}`}
                              className="gap-1 border-[var(--color-warn)]/40 text-[var(--color-warn)]"
                              title={
                                row.fallback.last_at
                                  ? formatTimestamp(row.fallback.last_at)
                                  : undefined
                              }
                            >
                              <TriangleAlert className="size-3" />
                              {t('providers.fallbackBadge', {
                                count: row.fallback.count,
                                reason: fallbackReason(row.fallback.last_reason),
                              })}
                              {row.fallback.last_at
                                ? ` · ${t('home.ageAgo', { age: formatAge(row.fallback.last_at) })}`
                                : ''}
                            </Badge>
                          ) : (
                            <span className="text-xs text-muted-foreground">
                              {t('providers.healthy')}
                            </span>
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>

                <p className="text-xs text-muted-foreground">{t('providers.note')}</p>
              </>
            )
          }}
        </AsyncSection>
      </CardContent>
    </Card>
  )
}
