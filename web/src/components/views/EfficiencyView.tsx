import { Layers, Terminal, Zap } from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Gauge } from '@/components/dashboard/Gauge'
import { MetricReadout } from '@/components/dashboard/MetricReadout'
import { Sparkline } from '@/components/dashboard/Sparkline'
import { useT } from '@/lib/i18n'
import { fetchEfficiency, type HeadroomEfficiency, type RtkEfficiency } from '@/lib/mirador-api'
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
    return (
      <p data-testid="efficiency-headroom-empty" className="text-sm text-muted-foreground">
        {t('efficiency.headroomNotAvailable')}
      </p>
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
        {rtk.saved_series.length > 0 ? (
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
              </div>
            )}
          </AsyncSection>
        </CardContent>
      </Card>
    </div>
  )
}
