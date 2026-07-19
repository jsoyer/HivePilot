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
})
