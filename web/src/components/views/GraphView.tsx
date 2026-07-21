import { type FormEvent, useEffect, useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import {
  fetchGraph,
  fetchGraphNode,
  fetchGraphSources,
  type GraphData,
  type GraphDetail,
  type GraphNode,
  type GraphSourceSummary,
} from '@/lib/mirador-api'
import { useAsyncData } from '@/lib/use-async-data'
import { GraphCanvas } from './GraphCanvas'
import { PanelRenderer } from './PanelRenderer'

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
  const sourcesState = useAsyncData(() => fetchGraphSources(), [])
  const sources = sourcesState.status === 'success' ? sourcesState.data.sources : []

  const [selectedSourceName, setSelectedSourceName] = useState<string | null>(null)
  const [paramInputs, setParamInputs] = useState<Record<string, string>>({})
  const [appliedParams, setAppliedParams] = useState<Record<string, string>>({})
  const [hiddenKinds, setHiddenKinds] = useState<Set<string>>(new Set())
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)

  // Default to the first registered source once the list loads — only if
  // the caller hasn't already picked one (never overrides a user choice).
  useEffect(() => {
    if (selectedSourceName === null && sources.length > 0) {
      setSelectedSourceName(sources[0].name)
    }
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [sources])

  const selectedSource: GraphSourceSummary | undefined = sources.find((s) => s.name === selectedSourceName)

  const graphState = useAsyncData<GraphData | null>(
    () => (selectedSourceName === null ? Promise.resolve(null) : fetchGraph(selectedSourceName, appliedParams)),
    [selectedSourceName, JSON.stringify(appliedParams)],
  )
  const graphData = graphState.status === 'success' ? graphState.data : null

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
        <CardTitle>Graph</CardTitle>
        <CardDescription>
          Graph-native views of HivePilot's own state — pan/zoom the canvas, click a node for detail.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {sourcesState.status === 'loading' && (
          <div role="status" className="animate-pulse text-sm text-muted-foreground">
            Loading sources…
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
          <p className="text-sm text-muted-foreground">No graph sources registered.</p>
        )}

        {sourcesState.status === 'success' && sources.length > 0 && (
          <>
            <div className="flex flex-wrap items-end gap-3">
              <div className="flex flex-col gap-1">
                <label htmlFor="graph-source-select" className="text-sm font-medium">
                  Source
                </label>
                <select
                  id="graph-source-select"
                  value={selectedSourceName ?? ''}
                  onChange={(event) => handleSourceChange(event.target.value)}
                  className="h-8 min-w-40 rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                >
                  {sources.map((source) => (
                    <option key={source.name} value={source.name}>
                      {source.title}
                    </option>
                  ))}
                </select>
              </div>

              {selectedSource && selectedSource.params.length > 0 && (
                <form className="flex flex-wrap items-end gap-2" onSubmit={handleParamsSubmit}>
                  {selectedSource.params.map((param) => (
                    <div key={param} className="flex flex-col gap-1">
                      <label htmlFor={`graph-param-${param}`} className="text-sm font-medium">
                        {param}
                      </label>
                      <Input
                        id={`graph-param-${param}`}
                        value={paramInputs[param] ?? ''}
                        onChange={(event) =>
                          setParamInputs((prev) => ({ ...prev, [param]: event.target.value }))
                        }
                        placeholder={param}
                      />
                    </div>
                  ))}
                  <Button type="submit" size="sm">
                    Load
                  </Button>
                </form>
              )}
            </div>

            {graphState.status === 'loading' && (
              <div role="status" className="animate-pulse text-sm text-muted-foreground">
                Loading graph…
              </div>
            )}

            {graphState.status === 'error' && (
              <>
                {graphState.error instanceof ApiForbiddenError ? (
                  <div
                    data-testid="graph-forbidden"
                    className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
                  >
                    This source requires a{' '}
                    <span className="font-medium text-foreground">
                      {selectedSource?.min_role ?? 'higher-privilege'}
                    </span>{' '}
                    token. Your current token can still use the other Mirador tabs — only this graph
                    source needs a higher role.
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
                  <div
                    data-testid="graph-missing-param-hint"
                    className="rounded-lg border border-dashed border-border bg-muted/30 p-6 text-center text-sm text-muted-foreground"
                  >
                    This source needs {missingParams.length === 1 ? 'a parameter' : 'parameters'} to load
                    data. Enter{' '}
                    {missingParams.map((param, index) => (
                      <span key={param}>
                        {index > 0 && (index === missingParams.length - 1 ? ' and ' : ', ')}
                        <span className="font-medium text-foreground">{param}</span>
                      </span>
                    ))}{' '}
                    above and click <span className="font-medium text-foreground">Load</span>.
                  </div>
                ) : (
                  <div
                    role="alert"
                    data-testid="graph-error-node"
                    className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
                  >
                    Failed to load this graph ({errorNode.label}). Try again or choose a different source.
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

                {graphData.nodes.length === 0 && (
                  <p className="text-sm text-muted-foreground">This source has no nodes yet.</p>
                )}

                {graphData.nodes.length > 0 && (
                  <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
                    <GraphCanvas
                      nodes={visibleNodes}
                      edges={visibleEdges}
                      layoutHint={graphData.layout_hint}
                      selectedNodeId={selectedNodeId}
                      onNodeClick={handleNodeClick}
                    />

                    <div className="rounded-lg border border-border p-3" data-testid="graph-detail-pane">
                      {selectedNodeId === null && (
                        <p className="text-sm text-muted-foreground">Select a node for detail.</p>
                      )}

                      {selectedNodeId !== null && detailState.status === 'loading' && (
                        <div role="status" className="animate-pulse text-sm text-muted-foreground">
                          Loading detail…
                        </div>
                      )}

                      {selectedNodeId !== null && detailState.status === 'error' && (
                        <>
                          {detailState.error instanceof ApiForbiddenError ? (
                            <div
                              data-testid="graph-detail-forbidden"
                              className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
                            >
                              This node's detail requires a{' '}
                              <span className="font-medium text-foreground">
                                {selectedSource?.min_role ?? 'higher-privilege'}
                              </span>{' '}
                              token.
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
                )}
              </>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
