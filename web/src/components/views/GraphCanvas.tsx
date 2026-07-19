import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import dagre from 'dagre'
import { useMemo } from 'react'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import type { GraphEdge, GraphNode } from '@/lib/mirador-api'

const CARD_WIDTH = 220
const CARD_HEIGHT = 88
const GRID_GAP_X = 48
const GRID_GAP_Y = 32
const GRID_COLUMNS = 4

/** `GraphNode.status` -> a `--chart-*` token (see `src/index.css`) for the
 * small status dot — deliberately reuses the SAME closed set of design
 * tokens every other Mirador view already draws from, no new palette. Any
 * unrecognized/absent status falls back to `--muted-foreground` (a neutral
 * dot, not an alarming color) rather than guessing. */
function statusDotClass(status: string | null): string {
  if (status === 'ok' || status === 'success' || status === 'healthy') return 'bg-(--chart-1)'
  if (status === 'warn' || status === 'degraded' || status === 'pending') return 'bg-(--chart-3)'
  if (status === 'error' || status === 'failed') return 'bg-destructive'
  return 'bg-muted-foreground'
}

export interface CardNodeData {
  label: string
  kind: string
  status: string | null
  badges: string[]
  isError: boolean
  selected: boolean
  [key: string]: unknown
}

/**
 * Custom react-flow node — a small shadcn `Card` showing the node's label,
 * a `kind` badge, a status dot, and (if any) its declared badges. The
 * normalized `kind="error"` node (`run_graph_fetch`'s never-raise fallback,
 * `hivepilot/graph.py`) renders with a destructive ring instead of crashing
 * the canvas. All text content here is `GraphNode`-authored and UNTRUSTED
 * (see `mirador-api.ts`'s module note) — plain JSX interpolation only,
 * exactly like `PanelRenderer`; this file must never inject raw markup via
 * React's escape-hatch innerHTML prop.
 */
function CardNode({ data }: NodeProps & { data: CardNodeData }) {
  return (
    <Card
      size="sm"
      className={`cursor-pointer transition-shadow ${
        data.isError ? 'ring-2 ring-destructive' : data.selected ? 'ring-2 ring-ring' : ''
      }`}
      style={{ width: CARD_WIDTH }}
      data-testid="graph-card-node"
    >
      <Handle type="target" position={Position.Top} className="opacity-0" />
      <CardHeader className="gap-1 px-3 pt-2">
        <div className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className={`size-2 shrink-0 rounded-full ${statusDotClass(data.status)}`}
          />
          <CardTitle className="truncate text-xs">{data.label}</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="flex flex-wrap gap-1 px-3 pb-2">
        <Badge variant={data.isError ? 'destructive' : 'outline'}>{data.kind}</Badge>
        {data.badges.map((badge) => (
          <Badge key={badge} variant="secondary">
            {badge}
          </Badge>
        ))}
      </CardContent>
      <Handle type="source" position={Position.Bottom} className="opacity-0" />
    </Card>
  )
}

const nodeTypes = { card: CardNode }

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
}

/**
 * Pannable/zoomable card-node canvas (`@xyflow/react`) — `GraphView`'s
 * rendering layer, kept in its own file so layout math stays testable in
 * isolation from data-fetch/filter state. Read-only: no `onNodesChange` /
 * `onEdgesChange` wiring, so nodes never become draggable — pan/zoom/fit
 * (native react-flow viewport behavior) still work regardless.
 */
export function GraphCanvas({ nodes, edges, layoutHint, selectedNodeId, onNodeClick }: GraphCanvasProps) {
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
          isError: node.kind === 'error',
          selected: node.id === selectedNodeId,
        } satisfies CardNodeData,
      })),
    [nodes, positions, selectedNodeId],
  )

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges.map((edge, index) => ({
        id: `${edge.source}->${edge.target}-${index}`,
        source: edge.source,
        target: edge.target,
        label: edge.label ?? undefined,
        type: layoutHint === 'dag' ? 'smoothstep' : 'straight',
      })),
    [edges, layoutHint],
  )

  return (
    <div className="h-[600px] w-full overflow-hidden rounded-lg border border-border">
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        onNodeClick={(_event, node) => onNodeClick(node.id)}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls position="bottom-left" />
        <MiniMap pannable zoomable />
      </ReactFlow>
    </div>
  )
}
