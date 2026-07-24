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
})
