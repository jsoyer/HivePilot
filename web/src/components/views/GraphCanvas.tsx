import {
  BaseEdge,
  Controls,
  EdgeLabelRenderer,
  getSmoothStepPath,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeChange,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import dagre from 'dagre'
import { useCallback, useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useT } from '@/lib/i18n'
import type { GraphEdge, GraphNode } from '@/lib/pollen-api'

const CARD_WIDTH = 220
const CARD_HEIGHT = 104
const GRID_GAP_X = 48
const GRID_GAP_Y = 32
const GRID_COLUMNS = 4
/** Cap on how many `GraphNode.meta` entries render as on-card metrics — a
 * source can put arbitrary keys in `meta` (e.g. a `pipeline` source might
 * stash a dozen fields there); the card is a compact 220px readout, not a
 * full detail view (that's `GraphDetail`'s `PanelRenderer` pane). Bumped
 * from 3 to 4 (Pollen graph-cascade rebuild) to fit the `pipeline` source's
 * `model`/`tokens`/`cost`/`duration` quartet — the Pollen translation of the
 * reference mockup's 4-column `REQ/S ERR% AVG PODS` card table. */
const MAX_NODE_METRICS = 4
/** `task`/`role` are real `GraphNode.meta` entries the `pipeline` source
 * also sets, but they're rendered elsewhere on the card (role -> a badge
 * chip, task -> only in the full `GraphDetail` pane) — excluded here so
 * they never crowd out the 4-column metrics table on a source that has
 * more to say than fits. Generic: harmless for any OTHER source that
 * happens not to use these exact key names. */
const CARD_METRIC_BLOCKLIST = new Set(['task', 'role'])

/** visual identity: node/edge coloring can be driven by `status` (health-
 * oriented), `kind` (category-oriented), or `role` (which agent/task role
 * executed this stage) — a small control in `GraphView` toggles this per
 * source, matching the reference mockup's "COLOR BY: Service" HUD chip.
 * `role` reads `GraphNode.meta.role` (set by the `pipeline` source); a node
 * with no role falls back to the same neutral treatment as an absent
 * status/kind. */
export type GraphColorBy = 'status' | 'kind' | 'role'

/** `GraphNode.status` -> a `--color-*` design token for the status dot —
 * reuses the SAME good/warn/crit/idle status-semantics palette every other
 * Pollen viz primitive (Sparkline/Gauge/BurnRibbon/RunBoardView) already
 * draws from, rather than the generic chart-N categorical palette (this
 * file's pre-"Service Map" version used `--chart-1..5` for status, which
 * was inconsistent with the rest of the dashboard). `running` — a status
 * that didn't exist before this rebuild — uses `--color-primary` (an
 * active/live accent, distinct from the four closed good/warn/crit/idle
 * slots); `skipped`/`pending`/`warn` all read as neutral `idle`. Any
 * unrecognized/absent status falls back to `--color-muted-foreground` (a
 * neutral dot, not an alarming color) rather than guessing. */
function statusColorVar(status: string | null): string {
  if (status === 'ok' || status === 'success' || status === 'healthy') return '--color-good'
  if (status === 'running') return '--color-primary'
  if (status === 'error' || status === 'failed') return '--color-crit'
  if (status === 'skipped' || status === 'pending' || status === 'warn' || status === 'degraded') {
    return '--color-idle'
  }
  return '--color-muted-foreground'
}

/** A stable, deterministic string -> `--chart-*` slot, cycling the SAME
 * 5-color palette `DistributionBar`/`BurnRibbon` already cycle by index —
 * a hash (not array index, which would shift every color whenever the node
 * list re-orders) so the SAME kind/role always draws the SAME color across
 * renders/sources. Shared by both `kind` and `role` color-by modes. */
const CATEGORY_COLOR_VARS = [
  '--color-chart-1',
  '--color-chart-2',
  '--color-chart-3',
  '--color-chart-4',
  '--color-chart-5',
]
function hashString(value: string): number {
  let hash = 0
  for (let i = 0; i < value.length; i++) hash = (hash * 31 + value.charCodeAt(i)) >>> 0
  return hash
}
function categoryColorVar(value: string): string {
  return CATEGORY_COLOR_VARS[hashString(value) % CATEGORY_COLOR_VARS.length]
}

/** Resolves the `--color-*` CSS variable name driving BOTH a node's status
 * dot and its outgoing edges' glow color, for the given `colorBy` mode.
 * `role` reads `GraphNode.meta.role` (a plain string the `pipeline` source
 * sets); an absent/non-string role falls back to the neutral muted color,
 * same posture as an absent status. */
function nodeColorVar(
  node: { status: string | null; kind: string; meta: Record<string, unknown> },
  colorBy: GraphColorBy,
): string {
  if (colorBy === 'kind') return categoryColorVar(node.kind)
  if (colorBy === 'role') {
    const role = typeof node.meta.role === 'string' ? node.meta.role : ''
    return role ? categoryColorVar(role) : '--color-muted-foreground'
  }
  return statusColorVar(node.status)
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
    if (CARD_METRIC_BLOCKLIST.has(key)) continue
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
 * Custom react-flow node — a small shadcn `Card` in the "Service Map"
 * layout: a header row (status/kind/role dot + monospace name, right-
 * aligned monospace `kind` type label) and a body metrics TABLE (a shared
 * header row of up to `MAX_NODE_METRICS` metric labels, then a shared value
 * row beneath — a real 2-row grid, matching the reference mockup's
 * `REQ/S ERR% AVG PODS` card table, not N independent mini stat blocks).
 * All text content here is `GraphNode`-authored and UNTRUSTED (see
 * `pollen-api.ts`'s module note) — plain JSX interpolation only, exactly
 * like `PanelRenderer`; this file must never inject raw markup via React's
 * escape-hatch innerHTML prop. A `status === "skipped"` node renders
 * visibly DIMMED (`opacity-60` + a "skipped" badge) rather than hidden —
 * the operator needs to see a stage was considered and explicitly skipped,
 * not wonder why it's missing.
 */
function CardNode({ data }: NodeProps & { data: CardNodeData }) {
  const t = useT()
  const colorVar = nodeColorVar(data, data.colorBy)
  const metrics = nodeMetrics(data.meta)
  const isSkipped = data.status === 'skipped'
  const isPending = data.status === 'pending'

  return (
    <Card
      size="sm"
      className={`cursor-pointer bg-card/90 backdrop-blur-sm transition-shadow ${
        data.isError ? 'ring-2 ring-destructive' : data.selected ? 'ring-2 ring-ring' : 'ring-1 ring-(--color-line-2)'
      } ${isSkipped || isPending ? 'opacity-60' : ''}`}
      style={{ width: CARD_WIDTH }}
      data-testid="graph-card-node"
      data-status={data.status ?? undefined}
    >
      <Handle type="target" position={Position.Top} className="opacity-0" />
      <CardHeader className="gap-1 px-3 pt-2">
        <div className="flex items-center justify-between gap-1.5">
          <div className="flex min-w-0 items-center gap-1.5">
            <span
              aria-hidden="true"
              className="size-2 shrink-0 rounded-full"
              style={{ backgroundColor: `var(${colorVar})` }}
            />
            <CardTitle className="truncate font-mono text-xs">{data.label}</CardTitle>
          </div>
          <span className="eyebrow shrink-0 font-mono">{data.kind}</span>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-1.5 px-3 pb-2">
        {(data.badges.length > 0 || isSkipped) && (
          <div className="flex flex-wrap gap-1">
            {isSkipped && (
              <Badge variant="outline" data-testid="graph-card-skipped-badge">
                {t('graph.statusSkipped')}
              </Badge>
            )}
            {data.badges.map((badge) => (
              <Badge key={badge} variant="secondary">
                {badge}
              </Badge>
            ))}
          </div>
        )}
        {metrics.length > 0 && (
          <div
            data-testid="graph-card-node-metrics"
            className="grid gap-x-2 gap-y-0.5"
            style={{ gridTemplateColumns: `repeat(${metrics.length}, minmax(0,1fr))` }}
          >
            {metrics.map(([label]) => (
              <span key={`${label}:h`} className="eyebrow truncate font-mono">
                {label}
              </span>
            ))}
            {metrics.map(([label, value]) => (
              <span key={`${label}:v`} className="metric-mono truncate text-xs">
                {value}
              </span>
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
  /** Whether this edge represents flow currently in transit — see
   * `FlowEdge`'s docstring. `false` for a completed, skipped, dead
   * (downstream of a failure), or not-yet-reached edge — a completed run
   * is a static record, never animated. */
  active: boolean
  /** Cascade requirement: "a failed stage must visibly stop the particle
   * flow — the dead end is the information." Set when this edge's SOURCE
   * node status is `error`/`failed`; rendered with a muted, non-glowing
   * stroke instead of the normal colored glow, regardless of `colorVar`. */
  dead: boolean
  /** Reasoned density choice (documented constraint: the reference's real
   * particle density semantics are unknown from still frames) — the
   * number of particles traveling this edge, derived from REAL token-count
   * data when available (more tokens moved -> more particles), never a
   * fabricated throughput number. Always >= 1 for an active edge. */
  particleCount: number
}

/** Reasoned motion choices for the "Service Map" cascade — documented here
 * since the reference gave no motion-speed/density spec (still frames
 * only): a CONSTANT lap duration for every particle (no data on the
 * reference's real speed, so no per-edge speed variation is invented), and
 * particle COUNT (not speed) scales with real per-edge token volume when
 * known (`FlowEdgeData.particleCount`) — density proportional to activity,
 * never to a throughput number we don't actually have. */
const PARTICLE_LAP_SECONDS = 1.6

/**
 * "Service Map" visual identity: an orthogonal (circuit-trace-style),
 * glowing flow edge — `getSmoothStepPath` with `borderRadius: 0` renders
 * strict horizontal/vertical segments with 90° corners (the reference
 * mockup's strongest visual signature), NOT a bezier curve. The path has a
 * bright core (`flow-edge-path` dash animation, reduced-motion-safe via
 * the shared CSS rule in `src/index.css` — this file must never duplicate
 * that with a JS media-query check of its own) plus a soft `colorVar`-tinted
 * glow.
 *
 * Animated particles travel along the path via SVG `<animateMotion>`
 * referencing the path by id (`mpath`) — rendered ONLY on `data.active`
 * edges (a completed run is a static record: no motion at all once
 * finished, per this feature's documented motion policy). Reduced-motion
 * is handled PURELY in CSS (`.graph-particle-motion` / `.graph-particle-static`,
 * see `src/index.css`) — never a JS media-query check in this file, same
 * discipline as the existing `.flow-edge-path` dash animation.
 *
 * A `data.dead` edge (downstream of a failed stage) renders muted/glow-less
 * — the cascade's dead end is the information the operator needs, not a
 * cosmetic detail to smooth over.
 */
function FlowEdge({
  id,
  sourceX,
  sourceY,
  sourcePosition,
  targetX,
  targetY,
  targetPosition,
  markerEnd,
  label,
  data,
}: EdgeProps) {
  const [path, labelX, labelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    borderRadius: 0,
  })
  const edgeData = data as FlowEdgeData | undefined
  const colorVar = edgeData?.colorVar ?? '--color-primary'
  const dead = edgeData?.dead ?? false
  const active = edgeData?.active ?? false
  const particleCount = Math.max(1, edgeData?.particleCount ?? 1)
  const pathId = `graph-edge-path-${id}`

  return (
    <>
      <BaseEdge
        id={pathId}
        path={path}
        markerEnd={markerEnd}
        className={dead ? '' : 'flow-edge-path'}
        style={{
          stroke: dead ? 'var(--color-muted-foreground)' : `var(${colorVar})`,
          strokeWidth: 1.5,
          strokeDasharray: dead ? '2 4' : undefined,
          filter: dead ? undefined : `drop-shadow(0 0 4px var(${colorVar}))`,
        }}
      />
      {active &&
        Array.from({ length: particleCount }).map((_, index) => (
          <circle
            key={index}
            r={2.5}
            fill={`var(${colorVar})`}
            className="graph-particle-motion"
            data-testid="graph-edge-particle"
          >
            <animateMotion
              dur={`${PARTICLE_LAP_SECONDS}s`}
              begin={`${(index * PARTICLE_LAP_SECONDS) / particleCount}s`}
              repeatCount="indefinite"
            >
              <mpath href={`#${pathId}`} />
            </animateMotion>
          </circle>
        ))}
      {active && (
        <circle
          cx={labelX}
          cy={labelY}
          r={2.5}
          fill={`var(${colorVar})`}
          className="graph-particle-static"
          data-testid="graph-edge-particle-static"
        />
      )}
      {label != null && (
        <EdgeLabelRenderer>
          <div
            className="metric-mono pointer-events-none absolute rounded bg-card/80 px-1 text-[10px] text-muted-foreground"
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY - 12}px)`,
            }}
            data-testid="graph-edge-label"
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
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

/** Real per-edge particle density (see `FlowEdgeData.particleCount`'s
 * docstring for the honesty constraint this satisfies) — parses the
 * TARGET node's own real `meta.tokens` value (`"<in>/<out>"`, set by the
 * `pipeline` source) rather than inventing a throughput number. Absent/
 * unparseable token data -> the constant baseline of 1 particle. */
function particleCountFor(targetMeta: Record<string, unknown> | undefined): number {
  const tokens = targetMeta?.tokens
  if (typeof tokens !== 'string') return 1
  const match = /^(\d+)\/(\d+)$/.exec(tokens)
  if (!match) return 1
  const total = Number(match[1]) + Number(match[2])
  if (total > 5000) return 3
  if (total > 500) return 2
  return 1
}

export interface GraphCanvasProps {
  nodes: GraphNode[]
  edges: GraphEdge[]
  layoutHint: string | null
  selectedNodeId: string | null
  onNodeClick: (nodeId: string) => void
  /** "Service Map" visual identity: drives the node status/role dot and
   * the edge glow color — `'status'` (default) is health-oriented (ok/
   * running/skipped/error), `'kind'`/`'role'` are category-oriented (a
   * stable color per node `kind`/`meta.role`). See `GraphView`'s color-by
   * toggle. */
  colorBy?: GraphColorBy
}

/** MiniMap node fill — real CSS custom-property VALUES (not Tailwind
 * classes), since `@xyflow/react`'s `<MiniMap>` renders plain SVG `<rect>`
 * elements via inline styles, not through Tailwind's class scanner. Mirrors
 * `nodeColorVar` above exactly, just as a literal `var(...)` string. */
function minimapNodeColor(node: Node, colorBy: GraphColorBy): string {
  const data = node.data as CardNodeData
  return `var(${nodeColorVar(data, colorBy)})`
}

/**
 * Pannable/zoomable card-node canvas (`@xyflow/react`) — `GraphView`'s
 * rendering layer, kept in its own file so layout math stays testable in
 * isolation from data-fetch/filter state.
 *
 * Nodes are draggable (`onNodesChange` wired to a local drag-override map,
 * merged over the computed dagre/grid layout) — matches the reference
 * mockup's "drag nodes to arrange" hint text; a fresh data fetch keeps any
 * still-relevant drag positions rather than snapping everything back, since
 * they're keyed by node id, not by array index.
 */
export function GraphCanvas({
  nodes,
  edges,
  layoutHint,
  selectedNodeId,
  onNodeClick,
  colorBy = 'status',
}: GraphCanvasProps) {
  const t = useT()
  const positions = useMemo(
    () => (layoutHint === 'dag' ? dagLayout(nodes, edges) : gridLayout(nodes)),
    [nodes, edges, layoutHint],
  )

  const [dragOverrides, setDragOverrides] = useState<Map<string, { x: number; y: number }>>(new Map())
  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setDragOverrides((prev) => {
      let next = prev
      for (const change of changes) {
        if (change.type === 'position' && change.position) {
          if (next === prev) next = new Map(prev)
          next.set(change.id, change.position)
        }
      }
      return next
    })
  }, [])

  const nodesById = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes])

  const flowNodes = useMemo<Node[]>(
    () =>
      nodes.map((node) => ({
        id: node.id,
        type: 'card',
        position: dragOverrides.get(node.id) ?? positions.get(node.id) ?? { x: 0, y: 0 },
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
    [nodes, positions, dragOverrides, selectedNodeId, colorBy],
  )

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges.map((edge, index) => {
        const sourceNode = nodesById.get(edge.source)
        const targetNode = nodesById.get(edge.target)
        const dead = sourceNode?.status === 'error' || sourceNode?.status === 'failed'
        const active = !dead && targetNode?.status === 'running'
        return {
          id: `${edge.source}->${edge.target}-${index}`,
          source: edge.source,
          target: edge.target,
          label: edge.label ?? undefined,
          type: 'flow',
          data: {
            colorVar: nodeColorVar(targetNode ?? { status: null, kind: '', meta: {} }, colorBy),
            active,
            dead,
            particleCount: particleCountFor(targetNode?.meta),
          } satisfies FlowEdgeData,
        }
      }),
    [edges, colorBy, nodesById],
  )

  return (
    <div
      className="bg-grid-dot relative h-[60vh] w-full overflow-hidden rounded-lg border border-border lg:h-[600px]"
      data-testid="graph-canvas-wrapper"
    >
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodeClick={(_event, node) => onNodeClick(node.id)}
        onNodesChange={onNodesChange}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        {/* No `<Background />` component here — the wrapper `<div>`'s
         * `bg-grid-dot` CSS class (see `src/index.css`) already paints the
         * faint dot-grid texture behind the canvas; layering react-flow's
         * OWN dot background on top would double it up. */}
        <Controls position="bottom-left" />
        {/* Mobile audit: `<MiniMap>` renders a FIXED 202x152px regardless of
         * viewport. Measured, that is 18% of the entire canvas area at 390px
         * and 44% of its width at 768px — and it sits directly on top of the
         * canvas hint text below, covering 69% of it at 390px and 37% at
         * 768px. A minimap of an already-small canvas earns none of that
         * space, so it is hidden below `lg:`; at 1440px there was no overlap
         * and nothing changes. `!` on both utilities because react-flow ships
         * its own `.react-flow__minimap` positioning rules. */}
        <MiniMap
          pannable
          zoomable
          nodeColor={(node) => minimapNodeColor(node, colorBy)}
          maskColor="color-mix(in srgb, var(--color-background) 70%, transparent)"
          className="hidden! rounded-md! border border-border bg-card/80! lg:block!"
        />
      </ReactFlow>
      <p
        className="eyebrow pointer-events-none absolute bottom-2 left-1/2 -translate-x-1/2 font-mono"
        data-testid="graph-canvas-hint"
      >
        {t('graph.canvasHint')}
      </p>
      <div
        className="pointer-events-none absolute top-2 left-2 flex flex-wrap gap-x-3 gap-y-1 rounded-md bg-card/70 px-2 py-1 font-mono text-[10px] backdrop-blur-sm"
        data-testid="graph-legend"
      >
        {(['ok', 'running', 'skipped', 'error'] as const).map((status) => (
          <span key={status} className="flex items-center gap-1">
            <span
              aria-hidden="true"
              className="size-1.5 rounded-full"
              style={{ backgroundColor: `var(${statusColorVar(status)})` }}
            />
            {t(
              status === 'ok'
                ? 'graph.statusSuccess'
                : status === 'running'
                  ? 'graph.statusRunning'
                  : status === 'skipped'
                    ? 'graph.statusSkipped'
                    : 'graph.statusFailed',
            )}
          </span>
        ))}
      </div>
    </div>
  )
}
