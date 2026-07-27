import {
  BaseEdge,
  Controls,
  getBezierPath,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import dagre from 'dagre'
import { useMemo } from 'react'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import type { GraphEdge, GraphNode } from '@/lib/pollen-api'

const CARD_WIDTH = 220
const CARD_HEIGHT = 88
const GRID_GAP_X = 48
const GRID_GAP_Y = 32
const GRID_COLUMNS = 4
/** Cap on how many `GraphNode.meta` entries render as on-card metrics — a
 * source can put arbitrary keys in `meta` (e.g. a `pipeline` source might
 * stash a dozen fields there); the card is a compact 220px readout, not a
 * full detail view (that's `GraphDetail`'s `PanelRenderer` pane). */
const MAX_NODE_METRICS = 3

/** visual identity: node/edge coloring can be driven by either `status`
 * (health-oriented) or `kind` (category-oriented) — a small control in
 * `GraphView` toggles this per source, matching the reference mockup's
 * "color by" HUD chip. */
export type GraphColorBy = 'status' | 'kind'

/** `GraphNode.status` -> a `--chart-*` token (see `src/index.css`) for the
 * small status dot — deliberately reuses the SAME closed set of design
 * tokens every other Pollen view already draws from, no new palette. Any
 * unrecognized/absent status falls back to `--muted-foreground` (a neutral
 * dot, not an alarming color) rather than guessing. */
function statusDotClass(status: string | null): string {
  if (status === 'ok' || status === 'success' || status === 'healthy') return 'bg-(--chart-1)'
  if (status === 'warn' || status === 'degraded' || status === 'pending') return 'bg-(--chart-3)'
  if (status === 'error' || status === 'failed') return 'bg-destructive'
  return 'bg-muted-foreground'
}

/** `GraphNode.kind` -> a deterministic `--chart-*` slot, cycling the SAME
 * 5-color palette `DistributionBar`/`BurnRibbon` already cycle by index —
 * a stable string hash (not array index, which would shift every color
 * whenever the node list re-orders) so the SAME kind always draws the SAME
 * color across renders/sources. */
const KIND_DOT_CLASSES = ['bg-(--chart-1)', 'bg-(--chart-2)', 'bg-(--chart-3)', 'bg-(--chart-4)', 'bg-(--chart-5)']
const KIND_COLOR_VARS = [
  '--color-chart-1',
  '--color-chart-2',
  '--color-chart-3',
  '--color-chart-4',
  '--color-chart-5',
]
function hashKind(kind: string): number {
  let hash = 0
  for (let i = 0; i < kind.length; i++) hash = (hash * 31 + kind.charCodeAt(i)) >>> 0
  return hash
}
function kindDotClass(kind: string): string {
  return KIND_DOT_CLASSES[hashKind(kind) % KIND_DOT_CLASSES.length]
}
function kindColorVar(kind: string): string {
  return KIND_COLOR_VARS[hashKind(kind) % KIND_COLOR_VARS.length]
}

/** A single on-card metric readout — `[label, formattedValue]`. Only
 * primitive `meta` values (string/number/boolean) are ever surfaced here;
 * nested objects/arrays are source-internal detail, not a compact metric,
 * and are left for `GraphDetail`'s full `PanelRenderer` pane instead. Never
 * fabricates a metric that isn't actually present in the source's real
 * `meta` payload. */
function nodeMetrics(meta: Record<string, unknown>): [string, string][] {
  const entries: [string, string][] = []
  for (const [key, value] of Object.entries(meta)) {
    if (entries.length >= MAX_NODE_METRICS) break
    if (typeof value === 'string' && value.length > 0) entries.push([key, value])
    else if (typeof value === 'number') entries.push([key, value.toLocaleString('en-US')])
    else if (typeof value === 'boolean') entries.push([key, value ? 'yes' : 'no'])
  }
  return entries
}

export interface CardNodeData {
  label: string
  kind: string
  status: string | null
  badges: string[]
  meta: Record<string, unknown>
  isError: boolean
  selected: boolean
  colorBy: GraphColorBy
  [key: string]: unknown
}

/**
 * Custom react-flow node — a small shadcn `Card` showing the node's label,
 * a `kind` badge, a status/kind dot (per `colorBy`), any declared badges,
 * and — the "Service Map" visual identity upgrade — up to
 * `MAX_NODE_METRICS` real metrics lifted straight from `GraphNode.meta`, in
 * the shared mono/tabular-nums instrument-readout treatment (`metric-mono`,
 * see `src/index.css`) every other Pollen viz primitive uses. The
 * normalized `kind="error"` node (`run_graph_fetch`'s never-raise fallback,
 * `hivepilot/graph.py`) renders with a destructive ring instead of crashing
 * the canvas. All text content here is `GraphNode`-authored and UNTRUSTED
 * (see `pollen-api.ts`'s module note) — plain JSX interpolation only,
 * exactly like `PanelRenderer`; this file must never inject raw markup via
 * React's escape-hatch innerHTML prop.
 */
function CardNode({ data }: NodeProps & { data: CardNodeData }) {
  const dotClass = data.colorBy === 'kind' ? kindDotClass(data.kind) : statusDotClass(data.status)
  const metrics = nodeMetrics(data.meta)

  return (
    <Card
      size="sm"
      className={`cursor-pointer bg-card/90 backdrop-blur-sm transition-shadow ${
        data.isError ? 'ring-2 ring-destructive' : data.selected ? 'ring-2 ring-ring' : 'ring-1 ring-(--color-line-2)'
      }`}
      style={{ width: CARD_WIDTH }}
      data-testid="graph-card-node"
    >
      <Handle type="target" position={Position.Top} className="opacity-0" />
      <CardHeader className="gap-1 px-3 pt-2">
        <div className="flex items-center gap-1.5">
          <span aria-hidden="true" className={`size-2 shrink-0 rounded-full ${dotClass}`} />
          <CardTitle className="truncate text-xs">{data.label}</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-1.5 px-3 pb-2">
        <div className="flex flex-wrap gap-1">
          <Badge variant={data.isError ? 'destructive' : 'outline'}>{data.kind}</Badge>
          {data.badges.map((badge) => (
            <Badge key={badge} variant="secondary">
              {badge}
            </Badge>
          ))}
        </div>
        {metrics.length > 0 && (
          <div data-testid="graph-card-node-metrics" className="flex gap-3">
            {metrics.map(([label, value]) => (
              <div key={label} className="flex min-w-0 flex-col gap-0.5">
                <span className="eyebrow truncate">{label}</span>
                <span className="metric-mono truncate text-sm">{value}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
      <Handle type="source" position={Position.Bottom} className="opacity-0" />
    </Card>
  )
}

const nodeTypes = { card: CardNode }

export interface FlowEdgeData {
  colorVar: string
  [key: string]: unknown
}

/**
 * "Service Map" visual identity: an animated glowing flow edge — a
 * bezier path in the edge's `colorVar` color with a moving-dash animation
 * (`.flow-edge-path`, see `src/index.css`) plus a soft `drop-shadow` glow,
 * reading as live data flowing along the edge (matches the reference
 * mockup's `.flow`/glow-underlay treatment). The dash animation is
 * disabled entirely under `prefers-reduced-motion: reduce` (see that CSS
 * rule) — the edge itself, and its color, remain fully visible either way,
 * so reduced-motion never loses information, only movement.
 */
function FlowEdge({
  sourceX,
  sourceY,
  sourcePosition,
  targetX,
  targetY,
  targetPosition,
  markerEnd,
  data,
}: EdgeProps) {
  const [path] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition })
  const colorVar = (data as FlowEdgeData | undefined)?.colorVar ?? '--color-primary'
  return (
    <BaseEdge
      path={path}
      markerEnd={markerEnd}
      className="flow-edge-path"
      style={{
        stroke: `var(${colorVar})`,
        strokeWidth: 1.5,
        filter: `drop-shadow(0 0 4px var(${colorVar}))`,
      }}
    />
  )
}

const edgeTypes = { flow: FlowEdge }

/** Deterministic column-grid layout for `layout_hint !== "dag"` (i.e.
 * `"grid"` or `null`) — nodes keep their source-given order, laid out
 * left-to-right/top-to-bottom in `GRID_COLUMNS` columns. No randomness, no
 * dependency on prior render state, so the SAME `GraphData` always produces
 * the SAME layout. */
function gridLayout(nodes: GraphNode[]): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>()
  nodes.forEach((node, index) => {
    const column = index % GRID_COLUMNS
    const row = Math.floor(index / GRID_COLUMNS)
    positions.set(node.id, {
      x: column * (CARD_WIDTH + GRID_GAP_X),
      y: row * (CARD_HEIGHT + GRID_GAP_Y),
    })
  })
  return positions
}

/** Layered layout for `layout_hint === "dag"`, via `dagre` — positions
 * nodes top-to-bottom by edge direction. A node with no edges still gets a
 * position (dagre places disconnected nodes on their own row), so an empty
 * `edges` array never crashes this. */
function dagLayout(nodes: GraphNode[], edges: GraphEdge[]): Map<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'TB', nodesep: GRID_GAP_X, ranksep: GRID_GAP_Y })
  g.setDefaultEdgeLabel(() => ({}))
  for (const node of nodes) {
    g.setNode(node.id, { width: CARD_WIDTH, height: CARD_HEIGHT })
  }
  for (const edge of edges) {
    if (g.hasNode(edge.source) && g.hasNode(edge.target)) {
      g.setEdge(edge.source, edge.target)
    }
  }
  dagre.layout(g)
  const positions = new Map<string, { x: number; y: number }>()
  for (const node of nodes) {
    const pos = g.node(node.id) as { x: number; y: number } | undefined
    positions.set(node.id, pos ? { x: pos.x - CARD_WIDTH / 2, y: pos.y - CARD_HEIGHT / 2 } : { x: 0, y: 0 })
  }
  return positions
}

export interface GraphCanvasProps {
  nodes: GraphNode[]
  edges: GraphEdge[]
  layoutHint: string | null
  selectedNodeId: string | null
  onNodeClick: (nodeId: string) => void
  /** "Service Map" visual identity: drives BOTH the node status dot and
   * the edge glow color — `'status'` (default) is health-oriented (ok/warn/
   * error), `'kind'` is category-oriented (a stable color per node/edge
   * `kind`). See `GraphView`'s color-by toggle. */
  colorBy?: GraphColorBy
}

/** MiniMap node fill — real CSS custom-property VALUES (not Tailwind
 * classes), since `@xyflow/react`'s `<MiniMap>` renders plain SVG `<rect>`
 * elements via inline styles, not through Tailwind's class scanner. Mirrors
 * `statusDotClass`/`kindDotClass` above exactly, just as literal `var(...)`
 * strings instead of `bg-*` class names. */
function minimapNodeColor(node: Node, colorBy: GraphColorBy): string {
  const data = node.data as CardNodeData
  if (colorBy === 'kind') return `var(${kindColorVar(data.kind)})`
  if (data.status === 'ok' || data.status === 'success' || data.status === 'healthy') return 'var(--color-chart-1)'
  if (data.status === 'warn' || data.status === 'degraded' || data.status === 'pending') return 'var(--color-chart-3)'
  if (data.status === 'error' || data.status === 'failed') return 'var(--color-destructive)'
  return 'var(--color-muted-foreground)'
}

/**
 * Pannable/zoomable card-node canvas (`@xyflow/react`) — `GraphView`'s
 * rendering layer, kept in its own file so layout math stays testable in
 * isolation from data-fetch/filter state. Read-only: no `onNodesChange` /
 * `onEdgesChange` wiring, so nodes never become draggable — pan/zoom/fit
 * (native react-flow viewport behavior) still work regardless.
 *
 * "Service Map" visual restyle: a faint dot-grid canvas background
 * (`bg-grid-dot`, see `src/index.css`), animated glowing `flow` edges
 * (colored by `colorBy`, see `FlowEdge` above) instead of plain lines, node
 * cards that surface real `meta` metrics, and a restyled `MiniMap`
 * (glass panel + status/kind-colored node dots) — all built entirely from
 * the EXISTING `/v1/graph/*` data this component already consumed; no new
 * data source.
 */
export function GraphCanvas({
  nodes,
  edges,
  layoutHint,
  selectedNodeId,
  onNodeClick,
  colorBy = 'status',
}: GraphCanvasProps) {
  const positions = useMemo(
    () => (layoutHint === 'dag' ? dagLayout(nodes, edges) : gridLayout(nodes)),
    [nodes, edges, layoutHint],
  )

  const flowNodes = useMemo<Node[]>(
    () =>
      nodes.map((node) => ({
        id: node.id,
        type: 'card',
        position: positions.get(node.id) ?? { x: 0, y: 0 },
        data: {
          label: node.label,
          kind: node.kind,
          status: node.status,
          badges: node.badges,
          meta: node.meta,
          isError: node.kind === 'error',
          selected: node.id === selectedNodeId,
          colorBy,
        } satisfies CardNodeData,
      })),
    [nodes, positions, selectedNodeId, colorBy],
  )

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges.map((edge, index) => ({
        id: `${edge.source}->${edge.target}-${index}`,
        source: edge.source,
        target: edge.target,
        label: edge.label ?? undefined,
        type: 'flow',
        data: {
          colorVar: colorBy === 'kind' && edge.kind ? kindColorVar(edge.kind) : '--color-primary',
        } satisfies FlowEdgeData,
      })),
    [edges, colorBy],
  )

  return (
    // Mobile-first: `@xyflow/react` needs an explicit height on its own
    // wrapper — it can't infer one from a grid/flex sibling (`GraphView`'s
    // detail pane) the way ordinary block content can. Below `lg:` this
    // uses a viewport-relative height (a fixed px height would be way too
    // tall on a short phone screen, or too short on a tall one); at `lg:`
    // it's pinned back to the original `600px` desktop height so desktop
    // stays pixel-identical to before.
    <div className="bg-grid-dot h-[60vh] w-full overflow-hidden rounded-lg border border-border lg:h-[600px]">
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodeClick={(_event, node) => onNodeClick(node.id)}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        {/* No `<Background />` component here — the wrapper `<div>`'s
         * `bg-grid-dot` CSS class (see `src/index.css`) already paints the
         * faint dot-grid texture behind the canvas; layering react-flow's
         * OWN dot background on top would double it up. */}
        <Controls position="bottom-left" />
        <MiniMap
          pannable
          zoomable
          nodeColor={(node) => minimapNodeColor(node, colorBy)}
          maskColor="color-mix(in srgb, var(--color-background) 70%, transparent)"
          className="rounded-md! border border-border bg-card/80!"
        />
      </ReactFlow>
    </div>
  )
}
