import { Workflow } from 'lucide-react'
import { type FormEvent, useEffect, useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { EmptyState } from '@/components/dashboard/EmptyState'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import {
  fetchGraph,
  fetchGraphNode,
  fetchGraphSources,
  parseGraphRunSelector,
  type GraphData,
  type GraphDetail,
  type GraphNode,
  type GraphSourceSummary,
} from '@/lib/pollen-api'
import { useAsyncData } from '@/lib/use-async-data'
import { GraphCanvas, type GraphColorBy } from './GraphCanvas'
import { PanelRenderer } from './PanelRenderer'

// Reuses `HomeView`'s `POLL_INTERVAL_MS` + `setInterval`/`refreshKey` pattern
// (not `useAsyncData`'s own polling — it has none): a run that's still
// `live` (per `parseGraphRunSelector`) refetches on this cadence WHILE the
// "Live" toggle is on, via `useAsyncData`'s existing stale-while-revalidate
// path (`isRefreshing`, no skeleton flash). A finished run never polls —
// "a completed run is a static record, not a live flow" (see
// `GraphCanvas.tsx`'s particle-motion comment for the same principle
// applied to the canvas itself).
const RUN_POLL_INTERVAL_MS = 5000

/**
 * Graph tab — `GET /v1/graph/sources` + `GET /v1/graph/{source}` +
 * `GET /v1/graph/{source}/node/{node_id}` (Mirador Graph View PRD, Sprint 3
 * web surface). A pannable/zoomable card-node canvas (`GraphCanvas`), a
 * source selector, kind-filter chips with counts, and a right detail pane
 * rendered through the EXISTING `PanelRenderer` — mirrors `PanelView`'s
 * `ApiForbiddenError` handling and `Mem0View`'s "don't fetch until we have
 * something to fetch" `useAsyncData<T | null>` pattern (no source selected
 * yet, or no node selected yet, both resolve to `null` rather than firing a
 * premature/garbage request).
 */
export function GraphView() {
  const t = useT()
  const sourcesState = useAsyncData(() => fetchGraphSources(), [])
  const sources = sourcesState.status === 'success' ? sourcesState.data.sources : []

  const [selectedSourceName, setSelectedSourceName] = useState<string | null>(null)
  const [paramInputs, setParamInputs] = useState<Record<string, string>>({})
  const [appliedParams, setAppliedParams] = useState<Record<string, string>>({})
  const [hiddenKinds, setHiddenKinds] = useState<Set<string>>(new Set())
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  // "Service Map" visual identity: a color-by toggle (status vs. kind vs.
  // role) — purely a client-side rendering choice over the SAME
  // already-fetched graph, so it never triggers a re-fetch (unlike
  // selectedSourceName/appliedParams above).
  const [colorBy, setColorBy] = useState<GraphColorBy>('status')
  // Run selector — `null` means "let the source pick" (the `pipeline`
  // source defaults to the latest run); an explicit id is threaded through
  // as an UNDECLARED `run_id` query param (the backend's `GraphContext.
  // params` is `dict(request.query_params)` unfiltered — see `fetchGraph`'s
  // docstring), never added to `GraphSourceSpec.params` (that would force a
  // mandatory freeform text box on every source).
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null)
  // "Live" toggle — only ever surfaced (see JSX below) when the currently
  // selected run's own `meta.live` says it's still running; a finished run
  // never shows this control at all.
  const [liveEnabled, setLiveEnabled] = useState(true)
  // Bumped by the live-poll interval AND by the manual "Reload" button —
  // both are "refetch the same params again", just triggered differently.
  const [refreshKey, setRefreshKey] = useState(0)

  // Content first: default to the first source that needs NO parameters, so
  // opening this tab always renders a graph. The previous default was simply
  // `sources[0]` — alphabetically the `pipeline` source, which requires a
  // `pipeline` param — so the view opened EMPTY behind "enter `pipeline`
  // above and click Load", asking an operator to guess a name before seeing
  // anything at all. Falls back to the first source when every registered
  // one takes a parameter. Never overrides a choice the caller already made.
  useEffect(() => {
    if (selectedSourceName === null && sources.length > 0) {
      const parameterless = sources.find((source) => source.params.length === 0)
      setSelectedSourceName((parameterless ?? sources[0]).name)
    }
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [sources])

  const selectedSource: GraphSourceSummary | undefined = sources.find((s) => s.name === selectedSourceName)

  const graphState = useAsyncData<GraphData | null>(
    () =>
      selectedSourceName === null
        ? Promise.resolve(null)
        : fetchGraph(selectedSourceName, {
            ...appliedParams,
            ...(selectedRunId !== null ? { run_id: String(selectedRunId) } : {}),
          }),
    [selectedSourceName, JSON.stringify(appliedParams), selectedRunId, refreshKey],
  )
  const graphData = graphState.status === 'success' ? graphState.data : null

  // Feature-detected, not source-name-matched: any source may populate
  // `meta.runs` (see `parseGraphRunSelector`'s docstring) — only the
  // built-in `pipeline` source does today, but a third-party `GraphSource`
  // plugin could too, and GraphView must not hardcode "pipeline" anywhere.
  const runSelector = useMemo(
    () => (graphData ? parseGraphRunSelector(graphData.meta) : null),
    [graphData],
  )

  // Poll only while the user has "Live" on AND the currently-displayed run
  // is itself still live — a finished run is a static record (see
  // `RUN_POLL_INTERVAL_MS` comment above), so this effect is a no-op for it
  // regardless of the toggle's state.
  useEffect(() => {
    if (!liveEnabled || !runSelector?.live) return
    const interval = window.setInterval(() => {
      setRefreshKey((key) => key + 1)
    }, RUN_POLL_INTERVAL_MS)
    return () => window.clearInterval(interval)
  }, [liveEnabled, runSelector?.live])

  const detailState = useAsyncData<GraphDetail | null>(
    () =>
      selectedSourceName === null || selectedNodeId === null
        ? Promise.resolve(null)
        : fetchGraphNode(selectedSourceName, selectedNodeId),
    [selectedSourceName, selectedNodeId],
  )

  // `run_graph_fetch` (`hivepilot/graph.py`) never raises: a source that
  // throws, or returns malformed data, degrades to a SINGLE synthetic
  // node — `id="error"`, `kind="error"`, `status="error"`, no edges, and a
  // label that is ONLY the exception TYPE name (e.g. "ValueError"), never
  // the message (no-secret-leak discipline). Rendering that as a graph node
  // on the canvas reads as a scary crash; detect it here (by the exact
  // id/kind/status/no-edges signature the backend always constructs, not by
  // string-matching the label) and render a friendly message INSTEAD of
  // handing it to `GraphCanvas`.
  const errorNode: GraphNode | null = useMemo(() => {
    if (!graphData) return null
    if (graphData.nodes.length !== 1 || graphData.edges.length > 0) return null
    const node = graphData.nodes[0]
    return node.id === 'error' && node.kind === 'error' && node.status === 'error' ? node : null
  }, [graphData])

  // Missing-required-param is distinguished from a genuine backend error
  // using information the frontend already trusts (`GraphSourceSummary.
  // params` + the params the user actually submitted) rather than
  // string-matching the exception label — the backend deliberately never
  // sends the real exception message, so the label alone ("ValueError")
  // can't tell "no pipeline given" apart from "unknown pipeline: foo".
  const missingParams = useMemo(() => {
    if (!selectedSource) return []
    return selectedSource.params.filter((param) => !(appliedParams[param] ?? '').trim())
  }, [selectedSource, appliedParams])

  const kindCounts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const node of graphData?.nodes ?? []) {
      counts.set(node.kind, (counts.get(node.kind) ?? 0) + 1)
    }
    return counts
  }, [graphData])

  const visibleNodeIds = useMemo(() => {
    const ids = new Set<string>()
    for (const node of graphData?.nodes ?? []) {
      if (!hiddenKinds.has(node.kind)) ids.add(node.id)
    }
    return ids
  }, [graphData, hiddenKinds])

  const visibleNodes: GraphNode[] = (graphData?.nodes ?? []).filter((n) => visibleNodeIds.has(n.id))
  const visibleEdges = (graphData?.edges ?? []).filter(
    (e) => visibleNodeIds.has(e.source) && visibleNodeIds.has(e.target),
  )

  function handleSourceChange(name: string) {
    setSelectedSourceName(name)
    setParamInputs({})
    setAppliedParams({})
    setHiddenKinds(new Set())
    setSelectedNodeId(null)
    setSelectedRunId(null)
    setLiveEnabled(true)
  }

  function handleReload() {
    setRefreshKey((key) => key + 1)
  }

  function handleParamsSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setAppliedParams({ ...paramInputs })
  }

  function toggleKind(kind: string) {
    setHiddenKinds((prev) => {
      const next = new Set(prev)
      if (next.has(kind)) next.delete(kind)
      else next.add(kind)
      return next
    })
  }

  function handleNodeClick(nodeId: string) {
    setSelectedNodeId(nodeId)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('graph.title')}</CardTitle>
        <CardDescription>{t('graph.description')}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {sourcesState.status === 'loading' && (
          <div role="status" className="animate-pulse text-sm text-muted-foreground">
            {t('graph.loadingSources')}
          </div>
        )}

        {sourcesState.status === 'error' && (
          <div
            role="alert"
            className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
          >
            {describeApiError(sourcesState.error)}
          </div>
        )}

        {sourcesState.status === 'success' && sources.length === 0 && (
          <p className="text-sm text-muted-foreground">{t('graph.noSources')}</p>
        )}

        {sourcesState.status === 'success' && sources.length > 0 && (
          <>
            <div className="flex flex-wrap items-end gap-3">
              <div className="flex flex-col gap-1">
                <label htmlFor="graph-source-select" className="text-sm font-medium">
                  {t('graph.source')}
                </label>
                <Select
                  id="graph-source-select"
                  className="min-w-40"
                  value={selectedSourceName ?? ''}
                  onChange={(event) => handleSourceChange(event.target.value)}
                >
                  {sources.map((source) => (
                    <option key={source.name} value={source.name}>
                      {source.title}
                    </option>
                  ))}
                </Select>
              </div>

              {/* A source's required params are offered as a PICK-LIST
                * whenever the backend can enumerate them (`param_options`,
                * e.g. every pipeline declared in `pipelines.yaml`). Only a
                * param it cannot enumerate falls back to a text box — the
                * same "free text is a last resort, never the default"
                * rule the New Run drawer follows. Selecting a value applies
                * it immediately; there is nothing to type, so there is
                * nothing to submit. */}
              {selectedSource && selectedSource.params.length > 0 && (
                <form className="flex flex-wrap items-end gap-2" onSubmit={handleParamsSubmit}>
                  {selectedSource.params.map((param) => {
                    const options = selectedSource.param_options?.[param] ?? []
                    return (
                      <div key={param} className="flex flex-col gap-1">
                        <label htmlFor={`graph-param-${param}`} className="eyebrow">
                          {param}
                        </label>
                        {options.length > 0 ? (
                          <Select
                            id={`graph-param-${param}`}
                            className="min-w-48"
                            value={appliedParams[param] ?? ''}
                            onChange={(event) => {
                              const value = event.target.value
                              setParamInputs((prev) => ({ ...prev, [param]: value }))
                              setAppliedParams((prev) => ({ ...prev, [param]: value }))
                            }}
                          >
                            <option value="">{t('graph.chooseParam', { param })}</option>
                            {options.map((option) => (
                              <option key={option} value={option}>
                                {option}
                              </option>
                            ))}
                          </Select>
                        ) : (
                          <Input
                            id={`graph-param-${param}`}
                            value={paramInputs[param] ?? ''}
                            onChange={(event) =>
                              setParamInputs((prev) => ({ ...prev, [param]: event.target.value }))
                            }
                            placeholder={param}
                          />
                        )}
                      </div>
                    )
                  })}
                  {selectedSource.params.some(
                    (param) => (selectedSource.param_options?.[param] ?? []).length === 0,
                  ) && (
                    <Button type="submit" size="sm">
                      {t('common.load')}
                    </Button>
                  )}
                </form>
              )}

              {selectedSourceName !== null && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  data-testid="graph-reload-button"
                  onClick={handleReload}
                >
                  {t('graph.reload')}
                </Button>
              )}
            </div>

            {graphState.status === 'loading' && (
              <div role="status" className="animate-pulse text-sm text-muted-foreground">
                {t('graph.loadingGraph')}
              </div>
            )}

            {graphState.status === 'error' && (
              <>
                {graphState.error instanceof ApiForbiddenError ? (
                  <div
                    data-testid="graph-forbidden"
                    className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
                  >
                    {t('graph.requiresTokenLead')}{' '}
                    <span className="font-medium text-foreground">
                      {selectedSource?.min_role ?? t('graph.higherPrivilege')}
                    </span>{' '}
                    {t('graph.requiresTokenTail')} {t('graph.requiresTokenNote')}
                  </div>
                ) : (
                  <div
                    role="alert"
                    className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
                  >
                    {describeApiError(graphState.error)}
                  </div>
                )}
              </>
            )}

            {graphState.status === 'success' && graphData && errorNode && (
              <>
                {missingParams.length > 0 ? (
                  <EmptyState
                    data-testid="graph-missing-param-hint"
                    icon={<Workflow className="size-4" />}
                    title={t('graph.missingParamTitle', { params: missingParams.join(', ') })}
                    body={
                      missingParams.every(
                        (param) => (selectedSource?.param_options?.[param] ?? []).length > 0,
                      )
                        ? t('graph.missingParamBodySelect')
                        : t('graph.missingParamBodyType')
                    }
                    className="max-w-xl"
                  />
                ) : (
                  <div
                    role="alert"
                    data-testid="graph-error-node"
                    className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
                  >
                    {t('graph.failedToLoad', { label: errorNode.label })}
                  </div>
                )}
              </>
            )}

            {graphState.status === 'success' && graphData && !errorNode && (
              <>
                {kindCounts.size > 0 && (
                  <div className="flex flex-wrap gap-2" data-testid="graph-kind-filters">
                    {Array.from(kindCounts.entries()).map(([kind, count]) => (
                      <Badge
                        key={kind}
                        variant={hiddenKinds.has(kind) ? 'outline' : 'secondary'}
                        className="cursor-pointer select-none"
                        onClick={() => toggleKind(kind)}
                        role="button"
                        aria-pressed={!hiddenKinds.has(kind)}
                      >
                        {kind} ({count})
                      </Badge>
                    ))}
                  </div>
                )}

                {/* Run selector — feature-detected via `parseGraphRunSelector`
                 * (see `runSelector` above), so a source with no run concept
                 * (e.g. `plugins`) never shows this at all. Rendered outside
                 * the `nodes.length > 0` branch below so it's still usable
                 * even when the currently-selected run happens to have zero
                 * evidence rows yet. */}
                {runSelector && runSelector.runs.length > 0 && (
                  <div className="flex flex-wrap items-center gap-3" data-testid="graph-run-selector">
                    <div className="flex flex-col gap-1">
                      <label htmlFor="graph-run-select" className="text-sm font-medium">
                        {t('graph.run')}
                      </label>
                      <Select
                        id="graph-run-select"
                        className="min-w-40"
                        value={selectedRunId ?? ''}
                        onChange={(event) =>
                          setSelectedRunId(event.target.value === '' ? null : Number(event.target.value))
                        }
                      >
                        <option value="">{t('graph.latestRun')}</option>
                        {runSelector.runs.map((run) => (
                          <option key={run.id} value={run.id}>
                            #{run.id} — {run.started_at ?? '—'} ({run.status ?? '—'})
                          </option>
                        ))}
                      </Select>
                    </div>

                    {/* Only shown while the currently-displayed run is
                     * itself still live — a finished run is a static
                     * record, so there's nothing to toggle for it. */}
                    {runSelector.live && (
                      <button
                        type="button"
                        data-testid="graph-live-toggle"
                        aria-pressed={liveEnabled}
                        onClick={() => setLiveEnabled((prev) => !prev)}
                        className={`h-7 rounded-md border border-border px-2.5 text-xs font-medium transition-colors ${
                          liveEnabled
                            ? 'bg-primary text-primary-foreground'
                            : 'bg-transparent text-muted-foreground hover:bg-muted'
                        }`}
                      >
                        {t('graph.live')}
                      </button>
                    )}
                  </div>
                )}

                {graphData.nodes.length === 0 && (
                  <p className="text-sm text-muted-foreground">{t('graph.noNodes')}</p>
                )}

                {graphData.nodes.length > 0 && (
                  <>
                    {/* visual identity: color-by control — matches the
                     * reference mockup's canvas HUD "color by" chip. Purely
                     * a rendering toggle over the already-fetched graph. */}
                    <div className="flex items-center gap-2" data-testid="graph-color-by">
                      <span className="eyebrow">{t('graph.colorBy')}</span>
                      <div className="flex overflow-hidden rounded-md border border-border">
                        {(['status', 'kind', 'role'] as const).map((option) => (
                          <button
                            key={option}
                            type="button"
                            data-testid={`graph-color-by-${option}`}
                            aria-pressed={colorBy === option}
                            onClick={() => setColorBy(option)}
                            className={`px-2.5 py-1 text-xs font-medium transition-colors ${
                              colorBy === option
                                ? 'bg-primary text-primary-foreground'
                                : 'bg-transparent text-muted-foreground hover:bg-muted'
                            }`}
                          >
                            {option === 'status'
                              ? t('graph.colorByStatus')
                              : option === 'kind'
                                ? t('graph.colorByKind')
                                : t('graph.colorByRole')}
                          </button>
                        ))}
                      </div>
                    </div>

                    {/* Mobile-first: a single column (canvas, then the
                     * detail pane full-width below it) below `lg:`; only
                     * becomes a canvas+320px-detail-pane row at `lg:`
                     * (>=1024px), where a phone-cramped side-by-side layout
                     * stops being an issue. */}
                    <div
                      className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_320px]"
                      data-testid="graph-layout-row"
                    >
                      <GraphCanvas
                        nodes={visibleNodes}
                        edges={visibleEdges}
                        layoutHint={graphData.layout_hint}
                        selectedNodeId={selectedNodeId}
                        onNodeClick={handleNodeClick}
                        colorBy={colorBy}
                      />

                      <div
                        className="bg-card/60 rounded-lg border border-border p-3 backdrop-blur-sm"
                        data-testid="graph-detail-pane"
                      >
                        {selectedNodeId === null && (
                          <p className="text-sm text-muted-foreground">{t('graph.selectNodeForDetail')}</p>
                        )}

                      {selectedNodeId !== null && detailState.status === 'loading' && (
                        <div role="status" className="animate-pulse text-sm text-muted-foreground">
                          {t('graph.loadingDetail')}
                        </div>
                      )}

                      {selectedNodeId !== null && detailState.status === 'error' && (
                        <>
                          {detailState.error instanceof ApiForbiddenError ? (
                            <div
                              data-testid="graph-detail-forbidden"
                              className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
                            >
                              {t('graph.nodeRequiresTokenLead')}{' '}
                              <span className="font-medium text-foreground">
                                {selectedSource?.min_role ?? t('graph.higherPrivilege')}
                              </span>{' '}
                              {t('graph.nodeRequiresTokenTail')}
                            </div>
                          ) : (
                            <div
                              role="alert"
                              className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
                            >
                              {describeApiError(detailState.error)}
                            </div>
                          )}
                        </>
                      )}

                      {selectedNodeId !== null && detailState.status === 'success' && detailState.data && (
                        <div className="flex flex-col gap-3">
                          <h3 className="font-heading text-sm font-medium">{detailState.data.title}</h3>
                          {detailState.data.tags.length > 0 && (
                            <div className="flex flex-wrap gap-1">
                              {detailState.data.tags.map((tag) => (
                                <Badge key={tag} variant="outline">
                                  {tag}
                                </Badge>
                              ))}
                            </div>
                          )}
                          <PanelRenderer data={{ sections: detailState.data.sections }} />
                        </div>
                      )}
                      </div>
                    </div>
                  </>
                )}
              </>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
