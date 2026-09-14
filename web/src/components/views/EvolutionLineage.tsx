import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  type Edge,
  type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import dagre from 'dagre'
import { useMemo } from 'react'
import type { SkillEvolutionLineage } from '@/lib/pollen-api'

const NODE_WIDTH = 180
const NODE_HEIGHT = 56

function layout(nodes: Node[], edges: Edge[]): Node[] {
  const graph = new dagre.graphlib.Graph()
  graph.setDefaultEdgeLabel(() => ({}))
  graph.setGraph({ rankdir: 'LR', nodesep: 32, ranksep: 48 })
  for (const node of nodes) {
    graph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT })
  }
  for (const edge of edges) {
    graph.setEdge(edge.source, edge.target)
  }
  dagre.layout(graph)
  return nodes.map((node) => {
    const placed = graph.node(node.id)
    return {
      ...node,
      position: {
        x: placed.x - NODE_WIDTH / 2,
        y: placed.y - NODE_HEIGHT / 2,
      },
    }
  })
}

export function EvolutionLineage({ lineage }: { lineage: SkillEvolutionLineage }) {
  const { nodes, edges } = useMemo(() => {
    const rawNodes: Node[] = lineage.nodes.map((node) => ({
      id: node.id,
      data: { label: `${node.label}${node.origin ? ` (${node.origin})` : ''}` },
      position: { x: 0, y: 0 },
      style: {
        width: NODE_WIDTH,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        fontSize: 12,
      },
    }))
    const rawEdges: Edge[] = lineage.edges.map((edge, index) => ({
      id: `${edge.source}-${edge.target}-${index}`,
      source: edge.source,
      target: edge.target,
    }))
    return { nodes: layout(rawNodes, rawEdges), edges: rawEdges }
  }, [lineage])

  if (lineage.nodes.length === 0) {
    return null
  }

  return (
    <div
      data-testid="evolution-lineage"
      className="h-[220px] w-full overflow-hidden rounded-md border border-border"
    >
      <ReactFlow nodes={nodes} edges={edges} fitView proOptions={{ hideAttribution: true }}>
        <Background />
        <MiniMap pannable zoomable />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
