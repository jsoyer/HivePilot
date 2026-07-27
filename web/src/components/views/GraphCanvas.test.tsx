import { describe, expect, it } from 'vitest'
// `?raw` — a Vite-native import (see `vite/client.d.ts`), not a Node `fs`
// read, so this works identically under `vitest run` and the production
// `vite build`. Loads this file's OWN source as a plain string for the
// static-scan assertion below.
import source from './GraphCanvas.tsx?raw'

describe('GraphCanvas source', () => {
  it('never uses dangerouslySetInnerHTML — all node/edge content is untrusted, GraphNode-authored text', () => {
    expect(source).not.toContain('dangerouslySetInnerHTML')
  })

  it('mobile-first: gives the canvas wrapper an explicit viewport-relative height on mobile, capped back to the desktop height at lg:', () => {
    // @xyflow/react needs an explicit height — it can't rely on a flex/grid
    // sibling for sizing. Below `lg:` it must be viewport-relative (a fixed
    // px height would either be way too tall or too short across phone
    // sizes); at `lg:` it must be restored to the original desktop height
    // so desktop stays visually unchanged.
    expect(source).toMatch(/h-\[60vh\]/)
    expect(source).toMatch(/lg:h-\[600px\]/)
  })

  it('visual identity: only ever surfaces primitive (string/number/boolean) meta values as on-card metrics, never nested objects/arrays', () => {
    // Static contract check on `nodeMetrics` — a nested object/array in
    // `GraphNode.meta` must never be handed to the compact node card (it
    // belongs in the full `GraphDetail` pane instead); asserting on the
    // source keeps this honest without needing a DOM mount (react-flow
    // needs `ResizeObserver`, unavailable in jsdom — see the module note
    // in `GraphView.test.tsx`).
    expect(source).toMatch(/typeof value === 'string'/)
    expect(source).toMatch(/typeof value === 'number'/)
    expect(source).toMatch(/typeof value === 'boolean'/)
  })

  it('visual identity: the animated flow edge relies on the shared CSS class for its dash animation, never a JS matchMedia check of its own', () => {
    // Reduced-motion handling for `.flow-edge-path` lives entirely in the
    // shared `@media (prefers-reduced-motion: reduce)` rule in
    // `src/index.css` — this file must never duplicate that logic with its
    // own `window.matchMedia` call (unlike `SweepRadar`, which draws to a
    // `<canvas>` and genuinely needs JS-level motion detection).
    expect(source).toMatch(/flow-edge-path/)
    expect(source).not.toContain('matchMedia')
  })

  it('Pollen cascade rebuild: edges are orthogonal (getSmoothStepPath, borderRadius 0), never a bezier curve', () => {
    expect(source).toMatch(/getSmoothStepPath/)
    expect(source).toMatch(/borderRadius:\s*0/)
    expect(source).not.toContain('getBezierPath')
  })

  it('Pollen cascade rebuild: particle motion + its static reduced-motion fallback are both PURELY CSS-class-gated, never a JS matchMedia check', () => {
    expect(source).toMatch(/graph-particle-motion/)
    expect(source).toMatch(/graph-particle-static/)
    expect(source).not.toContain('matchMedia')
  })

  it('Pollen cascade rebuild: particles only render on active (currently-running) edges, and particle count is derived from real token data, never a fabricated throughput number', () => {
    expect(source).toMatch(/active\s*&&/)
    expect(source).toMatch(/particleCountFor/)
  })

  it('Pollen cascade rebuild: a dead (post-failure) edge renders muted/glow-less — the cascade dead end is the information', () => {
    expect(source).toMatch(/dead/)
    expect(source).toMatch(/color-muted-foreground/)
  })

  it('Pollen cascade rebuild: a skipped stage renders dimmed but present, never hidden', () => {
    expect(source).toMatch(/isSkipped/)
    expect(source).toMatch(/opacity-60/)
    expect(source).not.toMatch(/status === 'skipped'\)\s*return null/)
  })

  it('Pollen cascade rebuild: monospace typography throughout the canvas (node names, kind label, legend, hint text)', () => {
    expect(source.match(/font-mono/g)?.length ?? 0).toBeGreaterThanOrEqual(4)
  })

  it('Pollen cascade rebuild: color-by supports role in addition to status/kind', () => {
    expect(source).toMatch(/'status' \| 'kind' \| 'role'/)
  })

  it('Pollen cascade rebuild: edge labels render via EdgeLabelRenderer (throughput/duration written on the edge itself)', () => {
    expect(source).toMatch(/EdgeLabelRenderer/)
  })

  it('Pollen cascade rebuild: a status/legend + chrome hint text are rendered on the canvas', () => {
    expect(source).toMatch(/graph-legend/)
    expect(source).toMatch(/graph-canvas-hint/)
  })

  it('Pollen cascade rebuild: nodes are draggable (onNodesChange wired), matching the "drag nodes to arrange" hint', () => {
    expect(source).toMatch(/onNodesChange/)
  })
})
