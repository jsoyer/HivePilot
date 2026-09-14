import { describe, expect, it } from 'vitest'
import source from './EvolutionLineage.tsx?raw'

describe('EvolutionLineage source', () => {
  it('uses existing @xyflow ReactFlow for the skill-evolution DAG', () => {
    expect(source).toContain("@xyflow/react")
    expect(source).toContain('ReactFlow')
    expect(source).toContain('dagre')
    expect(source).not.toContain('dangerouslySetInnerHTML')
  })
})
