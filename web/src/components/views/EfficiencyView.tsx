import { Layers, Terminal, Zap } from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Gauge } from '@/components/dashboard/Gauge'
import { MetricReadout } from '@/components/dashboard/MetricReadout'
import { Sparkline } from '@/components/dashboard/Sparkline'

// Below this, a sparkline shows a shape the data does not support.
const MIN_TREND_POINTS = 2
import { useT } from '@/lib/i18n'
import { fetchEfficiency, type CacheEfficiency, type HeadroomEfficiency,
  type ProxyEfficiency, type RtkEfficiency } from '@/lib/pollen-api'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

/** Matches `HomeView`'s own efficiency fetch window — no per-view selector
 * called out in the sprint for this view. */
const DAYS = 30

function formatTokens(n: number): string {
  return Math.round(n).toLocaleString('en-US')
}

/**
 * Headroom panel — `EfficiencySummary.headroom` is a REAL, zero-safe dict
 * (never `null`, see `headroom_metrics.efficiency_summary`'s docstring),
 * but an all-zero one (`total_compressions === 0`) means nothing has
 * actually been recorded — rendered as an honest "not reporting" state
 * rather than a Gauge frozen at 0%, which would look like a measured
 * result rather than an absence of data.
 *
 * `avg_ratio`/`p95_ratio` are `chars_after / chars_before` (see
 * `plugins/headroom.py`) — a SMALLER ratio means MORE was cut, so the
 * Gauge here plots the derived reduction rate (`1 - avg_ratio`), not the
 * raw ratio. There is no real cache/target concept in this data (Headroom
 * is a context-compression tool, not a cache), so the Gauge intentionally
 * omits a `target` marker rather than inventing a threshold the API
 * doesn't provide.
 */
function HeadroomPanel({ headroom }: { headroom: HeadroomEfficiency }) {
  const t = useT()

  if (headroom.total_compressions === 0) {
    // Zero compressions has two meanings, and the operator needs to know
    // which. Skips recorded => headroom RAN and declined; nothing at all =>
    // it never ran. Saying only "no compressions yet" for both is what sent
    // someone hunting a broken integration that was working correctly.
    const skipped = headroom.total_skipped ?? 0
    const reasons = Object.entries(headroom.skip_reasons ?? {})
    return (
      <div data-testid="efficiency-headroom-empty" className="flex flex-col gap-1">
        <p className="text-sm text-muted-foreground">
          {skipped > 0
            ? t('efficiency.headroomRanAndDeclined', { count: skipped })
            : t('efficiency.headroomNeverRan')}
        </p>
        {reasons.length > 0 && (
          <ul className="metric-mono flex flex-col gap-0.5 text-xs text-muted-foreground">
            {reasons.map(([reason, count]) => (
              <li key={reason}>
                {reason} · {count}
              </li>
            ))}
          </ul>
        )}
      </div>
    )
  }

  const reductionRate = Math.max(0, Math.min(1, 1 - headroom.avg_ratio))
  const keptAtP95 = Math.round(headroom.p95_ratio * 100)

  return (
    <div data-testid="efficiency-headroom-section" className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <MetricReadout
          icon={<Zap className="size-4" />}
          label={t('efficiency.tokensSaved')}
          value={formatTokens(headroom.est_tokens_saved)}
          tone="good"
        />
        <MetricReadout
          icon={<Layers className="size-4" />}
          label={t('efficiency.compressions')}
          value={headroom.total_compressions.toLocaleString('en-US')}
        />
        <MetricReadout
          label={t('efficiency.charsSaved')}
          value={headroom.chars_saved.toLocaleString('en-US')}
        />
      </div>
      <div className="flex flex-wrap items-center gap-6">
        <Gauge value={reductionRate} label={t('efficiency.avgCompressionRate')} tone="good" />
        <div className="text-sm text-muted-foreground">
          <p className="font-medium text-foreground">{t('efficiency.p95Ratio')}</p>
          <p>
            {keptAtP95}% {t('efficiency.p95RatioSub')}
          </p>
        </div>
      </div>
    </div>
  )
}

/**
 * Compression-proxy panel.
 *
 * The proxy is the third savings source and the only one on the critical
 * path of every agent call — it can also fall back to a direct call when
 * unreachable (`proxy_route`), which keeps the agents working and silently
 * stops compressing them. This panel is the only surface that would show
 * that had happened.
 *
 * `null` means unconfigured, unreachable or unparseable — rendered as an
 * honest "not answering" state, never a fabricated 0%. The distinction
 * matters: a proxy nobody can reach is a degraded system, while a proxy
 * reporting zero saved is a working one with nothing to compress.
 */
/**
 * Prompt-cache panel — the only source on this view measured from our OWN
 * telemetry rather than a tool's self-report.
 *
 * The headline rate is a shop window and deliberately not the point. On the
 * reference deployment it read a healthy 85% while one step, `ceo intake`,
 * created ~43k tokens of cache per run and read back ~16k, ten times over,
 * at 1.25x base input for a creation against 0.1x for a read. An aggregate
 * cannot show that: a busy healthy step drowns a quiet pathological one, and
 * the number that looks fine is exactly why nobody looks further.
 *
 * So the offender list is what this panel exists for. `amortisation` is the
 * MEDIAN per run, not the total — summing hid the real case entirely,
 * because one lucky run that read 326 696 tokens lifted the sum over the
 * floor while nine runs out of ten paid double.
 */
function CachePanel({ cache }: { cache: CacheEfficiency | null | undefined }) {
  const t = useT()

  // `undefined` as well as `null`: a server older than this field simply
  // omits it, and crashing here would take the whole view down over a
  // payload that is merely out of date.
  if (!cache || cache.hit_rate === null) {
    return (
      <p data-testid="efficiency-cache-empty" className="text-sm text-muted-foreground">
        {t('efficiency.cacheNotMeasured')}
      </p>
    )
  }

  return (
    <div data-testid="efficiency-cache-section" className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <MetricReadout
          label={t('efficiency.cacheHitRate')}
          value={`${Math.round(cache.hit_rate * 100)}%`}
          sub={t('efficiency.cacheSteps', { count: cache.steps })}
        />
        <MetricReadout
          label={t('efficiency.cacheRead')}
          value={formatTokens(cache.cache_read)}
        />
        <MetricReadout
          label={t('efficiency.cacheCreated')}
          value={formatTokens(cache.cache_creation)}
        />
      </div>

      {cache.unamortised.length > 0 && (
        <div data-testid="efficiency-cache-offenders" className="flex flex-col gap-2">
          <h4 className="text-xs font-semibold">{t('efficiency.cacheUnamortisedTitle')}</h4>
          <p className="text-xs text-muted-foreground">
            {t('efficiency.cacheUnamortisedDescription')}
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-xs tabular-nums">
              <thead>
                <tr className="text-left text-muted-foreground">
                  <th className="py-1 pr-4 font-medium">{t('efficiency.cacheColStep')}</th>
                  <th className="py-1 pr-4 font-medium">{t('efficiency.cacheColRuns')}</th>
                  <th className="py-1 pr-4 font-medium">{t('efficiency.cacheColCreated')}</th>
                  <th className="py-1 pr-4 font-medium">{t('efficiency.cacheColRead')}</th>
                  <th className="py-1 pr-4 font-medium">{t('efficiency.cacheColTurns')}</th>
                  <th className="py-1 font-medium">{t('efficiency.cacheColAmortisation')}</th>
                </tr>
              </thead>
              <tbody>
                {cache.unamortised.map((s) => (
                  <tr key={s.step} className="border-t border-border/50">
                    <td className="py-1 pr-4">{s.step}</td>
                    <td className="py-1 pr-4">{s.runs}</td>
                    <td className="py-1 pr-4">{formatTokens(s.cache_creation)}</td>
                    <td className="py-1 pr-4">{formatTokens(s.cache_read)}</td>
                    <td className="py-1 pr-4">{s.turns ?? '—'}</td>
                    <td className="py-1">{s.amortisation.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Without this the table just looks empty after the turn-capture
          split lands, and an empty table reads as "nothing to fix" — which
          is the same misreading the split was written to stop. Every row
          recorded before the column existed sits here. */}
      {(cache.unclassified ?? 0) > 0 && (
        <p data-testid="efficiency-cache-unclassified" className="text-xs text-muted-foreground">
          {t('efficiency.cacheUnclassified', { count: cache.unclassified ?? 0 })}
        </p>
      )}
    </div>
  )
}

function ProxyPanel({ proxy }: { proxy: ProxyEfficiency | null | undefined }) {
  const t = useT()

  // `undefined` as well as `null`: a server older than this field simply
  // omits it, and `proxy === null` would fall through to `proxy.summary` and
  // crash the whole panel on a payload that is merely out of date.
  if (!proxy) {
    return (
      <p data-testid="efficiency-proxy-empty" className="text-sm text-muted-foreground">
        {t('efficiency.proxyNotAvailable')}
      </p>
    )
  }

  const summary = proxy.summary ?? {}
  const compression = summary.compression ?? {}
  const cost = summary.cost ?? {}

  return (
    <div data-testid="efficiency-proxy-section" className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricReadout
          label={t('efficiency.proxyRequests')}
          value={(summary.api_requests ?? 0).toLocaleString('en-US')}
          sub={t('efficiency.proxyMode', { mode: summary.mode ?? '—' })}
        />
        <MetricReadout
          label={t('efficiency.proxyCompressed')}
          value={(compression.requests_compressed ?? 0).toLocaleString('en-US')}
        />
        <MetricReadout
          label={t('efficiency.proxyTokensRemoved')}
          value={(compression.total_tokens_removed ?? 0).toLocaleString('en-US')}
          sub={`${(compression.avg_compression_pct ?? 0).toFixed(1)}%`}
        />
        <MetricReadout
          label={t('efficiency.proxySaved')}
          value={`$${(cost.total_saved_usd ?? 0).toFixed(4)}`}
          sub={t('efficiency.proxyAgainst', {
            total: `$${(cost.without_headroom_usd ?? 0).toFixed(2)}`,
          })}
        />
      </div>
    </div>
  )
}

/**
 * rtk panel — `EfficiencySummary.rtk` is `null` whenever the `rtk` binary
 * is absent/erroring/unparseable (see `efficiency_service.rtk_summary`'s
 * docstring) — rendered as an honest "not available on this host" state,
 * never a fabricated 0%. `top_commands` is always `null` on the wire (no
 * per-command breakdown in `rtk gain -f json`) and is never rendered.
 */
function RtkPanel({ rtk }: { rtk: RtkEfficiency | null }) {
  const t = useT()

  if (rtk === null) {
    return (
      <p data-testid="efficiency-rtk-empty" className="text-sm text-muted-foreground">
        {t('efficiency.rtkNotAvailable')}
      </p>
    )
  }

  return (
    <div data-testid="efficiency-rtk-section" className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <MetricReadout
          icon={<Terminal className="size-4" />}
          label={t('efficiency.rtkGain')}
          value={`${rtk.gain_pct}%`}
          tone="good"
        />
        <MetricReadout label={t('efficiency.rtkTokensSaved')} value={formatTokens(rtk.tokens_saved)} />
        <MetricReadout label={t('efficiency.rtkCommands')} value={rtk.total_commands.toLocaleString('en-US')} />
      </div>
      <div className="flex flex-col gap-2">
        <h4 className="text-sm font-medium">{t('efficiency.rtkSavedSeriesTitle')}</h4>
        {/* A trend needs at least two points. One sample drew a flat stub that
            looked like a real (and flat) trend — worse than saying nothing. */}
        {rtk.saved_series.length >= MIN_TREND_POINTS ? (
          <Sparkline points={rtk.saved_series.map((point) => point.saved_tokens)} tone="good" />
        ) : (
          <p className="text-sm text-muted-foreground">{t('efficiency.noSavedSeries')}</p>
        )}
      </div>
    </div>
  )
}

/**
 * Efficiency tab (Mirador Spend section) — `GET /v1/efficiency`: Headroom
 * context-compression savings (real, zero-safe, tenant-scoped) and the
 * `rtk` CLI's best-effort GLOBAL command-savings telemetry (not
 * tenant-scoped, `null` whenever the binary/data isn't available). Both
 * halves fetch as ONE call (`EfficiencySummary`) but render independently —
 * `rtk === null` never blanks the headroom panel and vice versa.
 */
export function EfficiencyView() {
  const t = useT()
  const efficiency = useAsyncData(() => fetchEfficiency(DAYS), [DAYS])

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>{t('efficiency.title')}</CardTitle>
          <CardDescription>{t('efficiency.description')}</CardDescription>
        </CardHeader>
        <CardContent>
          <AsyncSection state={efficiency} isEmpty={() => false}>
            {(data) => (
              <div className="flex flex-col gap-6">
                <div>
                  <h3 className="mb-2 text-sm font-semibold">{t('efficiency.headroomTitle')}</h3>
                  <p className="mb-3 text-xs text-muted-foreground">{t('efficiency.headroomDescription')}</p>
                  <HeadroomPanel headroom={data.headroom} />
                </div>
                <div>
                  <h3 className="mb-2 text-sm font-semibold">{t('efficiency.rtkTitle')}</h3>
                  <p className="mb-3 text-xs text-muted-foreground">{t('efficiency.rtkDescription')}</p>
                  <RtkPanel rtk={data.rtk} />
                </div>
                <div>
                  <h3 className="mb-2 text-sm font-semibold">{t('efficiency.proxyTitle')}</h3>
                  <p className="mb-3 text-xs text-muted-foreground">{t('efficiency.proxyDescription')}</p>
                  <ProxyPanel proxy={data.proxy} />
                </div>
                <div>
                  <h3 className="mb-2 text-sm font-semibold">{t('efficiency.cacheTitle')}</h3>
                  <p className="mb-3 text-xs text-muted-foreground">{t('efficiency.cacheDescription')}</p>
                  <CachePanel cache={data.cache} />
                </div>
              </div>
            )}
          </AsyncSection>
        </CardContent>
      </Card>
    </div>
  )
}
