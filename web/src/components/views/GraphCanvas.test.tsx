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

  it('IA/Cyber identity: only ever surfaces primitive (string/number/boolean) meta values as on-card metrics, never nested objects/arrays', () => {
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

  it('IA/Cyber identity: the animated flow edge relies on the shared CSS class for its dash animation, never a JS matchMedia check of its own', () => {
    // Reduced-motion handling for `.flow-edge-path` lives entirely in the
    // shared `@media (prefers-reduced-motion: reduce)` rule in
    // `src/index.css` — this file must never duplicate that logic with its
    // own `window.matchMedia` call (unlike `SweepRadar`, which draws to a
    // `<canvas>` and genuinely needs JS-level motion detection).
    expect(source).toMatch(/flow-edge-path/)
    expect(source).not.toContain('matchMedia')
  })
})
