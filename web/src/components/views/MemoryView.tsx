import { Database, Layers, TrendingUp, Users } from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { DistributionBar } from '@/components/dashboard/DistributionBar'
import { MetricReadout } from '@/components/dashboard/MetricReadout'
import { Sparkline } from '@/components/dashboard/Sparkline'
import { ApiForbiddenError } from '@/lib/api'
import { useT } from '@/lib/i18n'
import { fetchMemoryGrowth } from '@/lib/mirador-api'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'
import { Mem0View } from './Mem0View'
import { RealityView } from './RealityView'

const DAYS = 30

/**
 * Growth tab body — `GET /v1/memory/growth`: total memory count
 * (`MetricReadout`), a namespace breakdown and a by-actor breakdown (both
 * `DistributionBar`, same primitive, same convention as `RealityView`'s
 * gaps-by-namespace section), and a growth-over-time trend (`Sparkline`
 * from `growth_series`).
 *
 * **Honesty over completeness** (mirrors `RealityView`'s and `HomeView`'s
 * convention): a genuinely-empty response (`total === 0` and every
 * breakdown/series empty — the opt-in memory instrumentation has never
 * fired) renders ONE plain empty-state message via `AsyncSection`'s own
 * `isEmpty`, instead of a `MetricReadout` frozen at `0`. `authorship` is
 * ALWAYS `null` on the wire (no real human-vs-agent split is tracked — see
 * `mirador-api.ts`'s module note above `MemoryGrowth`) — rendered as an
 * explicit "not available" line, never fabricated from `by_actor` (a REAL,
 * different breakdown: distinct actor identities, not a human/agent
 * category).
 *
 * **403 handling**: `fetchMemoryGrowth` opts into `on403: 'forbidden'`
 * (same as every other `/v1/memory/*` fetcher) — a 403 is peeled off
 * BEFORE `AsyncSection` and rendered as the same "requires a
 * higher-privilege token" banner `RealityView` uses for its own sections
 * (reusing its `reality.requiresToken*` keys — identical semantics, so no
 * second copy of the same three strings).
 *
 * **Security**: `namespace`/`actor` (from `memories_by_namespace`/
 * `by_actor`) are caller-influenced free text, same untrusted trust class
 * as everywhere else under `/v1/memory/*` (see `RealityView`'s module
 * note) — `DistributionBar` renders both via plain JSX text
 * interpolation only, never a raw-HTML injection prop.
 */
function GrowthTab() {
  const t = useT()
  const growth = useAsyncData(() => fetchMemoryGrowth(DAYS), [DAYS])

  if (growth.status === 'error' && growth.error instanceof ApiForbiddenError) {
    return (
      <div
        data-testid="memory-growth-forbidden"
        className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
      >
        {t('reality.requiresTokenLead')}{' '}
        <span className="font-medium text-foreground">{t('reality.requiresTokenTail')}</span>{' '}
        {t('reality.requiresTokenNote')}
      </div>
    )
  }

  return (
    <AsyncSection
      state={growth}
      isEmpty={(data) =>
        data.total === 0 &&
        data.memories_by_namespace.length === 0 &&
        data.growth_series.length === 0 &&
        data.by_actor.length === 0
      }
      emptyMessage={t('memory.growthEmptyState')}
    >
      {(data) => (
        <div className="flex flex-col gap-6">
          <MetricReadout
            icon={<Database className="size-4" />}
            label={t('memory.totalMemories')}
            value={data.total.toLocaleString('en-US')}
          />

          <div>
            <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold">
              <Layers className="size-4" />
              {t('memory.byNamespaceTitle')}
            </h3>
            <DistributionBar
              segments={data.memories_by_namespace.map((ns) => ({
                key: ns.namespace,
                label: ns.namespace,
                value: ns.count,
              }))}
            />
          </div>

          <div>
            <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold">
              <TrendingUp className="size-4" />
              {t('memory.growthOverTimeTitle')}
            </h3>
            {data.growth_series.length > 0 ? (
              <Sparkline
                points={data.growth_series.map((point) => point.created)}
                tone="good"
                ariaLabel={t('memory.growthOverTimeTitle')}
              />
            ) : (
              <p className="text-sm text-muted-foreground">{t('memory.noGrowthSeries')}</p>
            )}
          </div>

          <div>
            <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold">
              <Users className="size-4" />
              {t('memory.byActorTitle')}
            </h3>
            <DistributionBar
              segments={data.by_actor.map((actor) => ({
                key: actor.actor,
                label: actor.actor,
                value: actor.count,
              }))}
            />
          </div>

          <p data-testid="memory-authorship-note" className="text-xs text-muted-foreground">
            {t('memory.authorshipNotAvailable')}
          </p>
        </div>
      )}
    </AsyncSection>
  )
}

/**
 * Unified Memory view — merges the formerly-separate "Réalité" (memory
 * quality) and "Mem0" (search) built-in tabs into ONE, plus a new Growth
 * tab (`/v1/memory/growth`), grouped under a single "Memory" nav entry
 * (see `nav-config.ts`'s `NAV_GROUP_ORDER`). Default tab is Quality.
 *
 * **Quality** and **Search** render the EXISTING `RealityView`/`Mem0View`
 * components UNCHANGED, as full tab panels — their own loading/error/
 * empty/403 handling, and every one of their existing tests, keep working
 * exactly as before; only where they're mounted in the shell changed (they
 * used to be two sibling top-level `Mirador.tsx` tabs, now they're two
 * inner tabs of this one).
 *
 * This inner `Tabs` root is a SEPARATE, independent instance from the
 * outer shell `Tabs` in `Mirador.tsx` (nested Base UI `Tabs.Root`s don't
 * share state) — Base UI only mounts the ACTIVE panel's `Tabs.Panel` (the
 * same "one `[role=\"tabpanel\"]` at a time" behavior the outer shell
 * already relies on), so `RealityView`/`Mem0View`/`GrowthTab` each only
 * fetch their own data once their tab is actually selected, never all
 * three eagerly on mount.
 */
export function MemoryView() {
  const t = useT()

  return (
    <Tabs defaultValue="quality" className="gap-4">
      <TabsList>
        <TabsTrigger value="quality">{t('memory.tabQuality')}</TabsTrigger>
        <TabsTrigger value="growth">{t('memory.tabGrowth')}</TabsTrigger>
        <TabsTrigger value="search">{t('memory.tabSearch')}</TabsTrigger>
      </TabsList>
      <TabsContent value="quality">
        <RealityView />
      </TabsContent>
      <TabsContent value="growth">
        <Card>
          <CardHeader>
            <CardTitle>{t('memory.growthTitle')}</CardTitle>
            <CardDescription>{t('memory.growthDescription')}</CardDescription>
          </CardHeader>
          <CardContent>
            <GrowthTab />
          </CardContent>
        </Card>
      </TabsContent>
      <TabsContent value="search">
        <Mem0View />
      </TabsContent>
    </Tabs>
  )
}
