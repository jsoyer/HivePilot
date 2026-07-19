import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiForbiddenError } from '@/lib/api'
import type { GraphData, GraphDetail, GraphSourcesResponse } from '@/lib/mirador-api'
// `?raw` — a Vite-native import (see `vite/client.d.ts`), not a Node `fs`
// read, so this works identically under `vitest run` and the production
// `vite build`. Loads this file's OWN source as a plain string for the
// static-scan assertion at the bottom of this file.
import graphViewSource from './GraphView.tsx?raw'

const { fetchGraphSources, fetchGraph, fetchGraphNode } = vi.hoisted(() => ({
  fetchGraphSources: vi.fn(),
  fetchGraph: vi.fn(),
  fetchGraphNode: vi.fn(),
}))

vi.mock('@/lib/mirador-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/mirador-api')>()
  return { ...actual, fetchGraphSources, fetchGraph, fetchGraphNode }
})

// `GraphCanvas` wraps `@xyflow/react`, which needs `ResizeObserver` — a
// DOM API jsdom doesn't implement. GraphView's OWN unit tests exercise its
// data-fetch/filter/detail-pane logic only; `GraphCanvas` itself is proven
// safe by `GraphCanvas.test.tsx` (source-level, no DOM mount needed) plus
// this stub, which records exactly what GraphView passes it (node/edge
// counts, the click callback) without needing a real canvas mount.
const { onNodeClickSpy } = vi.hoisted(() => ({ onNodeClickSpy: vi.fn() }))
vi.mock('./GraphCanvas', () => ({
  GraphCanvas: (props: {
    nodes: { id: string }[]
    edges: unknown[]
    onNodeClick: (id: string) => void
  }) => {
    onNodeClickSpy(props.onNodeClick)
    return (
      <div data-testid="graph-canvas-stub">
        <span data-testid="graph-canvas-node-count">{props.nodes.length}</span>
        {props.nodes.map((n) => (
          <button key={n.id} type="button" onClick={() => props.onNodeClick(n.id)}>
            node:{n.id}
          </button>
        ))}
      </div>
    )
  },
}))

import { GraphView } from './GraphView'

let container: HTMLDivElement
let root: Root

function mount() {
  act(() => {
    root.render(<GraphView />)
  })
}

function setNativeValue(input: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
  nativeSetter.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

const SOURCES: GraphSourcesResponse = {
  sources: [
    { name: 'plugins', title: 'Plugins', min_role: 'read', params: [] },
    { name: 'pipeline', title: 'Pipeline', min_role: 'read', params: ['pipeline'] },
  ],
}

const GRAPH: GraphData = {
  source: 'plugins',
  nodes: [
    { id: 'a', label: 'Plugin A', kind: 'plugin', status: 'ok', group: null, badges: [], meta: {} },
    { id: 'b', label: 'Plugin B', kind: 'plugin', status: 'error', group: null, badges: [], meta: {} },
    { id: 'c', label: 'Role C', kind: 'role', status: null, group: null, badges: [], meta: {} },
  ],
  edges: [
    { source: 'a', target: 'c', kind: null, label: null },
    { source: 'b', target: 'c', kind: null, label: null },
  ],
  layout_hint: null,
}

const DETAIL: GraphDetail = {
  title: 'Plugin A detail',
  tags: ['plugin'],
  sections: [{ kind: 'stat', label: 'Status', value: 'ok', status: 'ok' }],
}

beforeEach(() => {
  fetchGraphSources.mockReset()
  fetchGraph.mockReset()
  fetchGraphNode.mockReset()
  onNodeClickSpy.mockReset()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
  vi.restoreAllMocks()
})

describe('GraphView', () => {
  it('shows a loading indicator before the source list resolves', () => {
    fetchGraphSources.mockReturnValue(new Promise(() => {}))
    mount()
    expect(container.querySelector('[role="status"]')).not.toBeNull()
  })

  it('fetches sources and defaults to the first one, rendering its nodes via GraphCanvas', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(GRAPH)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(fetchGraph).toHaveBeenCalledWith('plugins', {})
    expect(container.querySelector('[data-testid="graph-canvas-node-count"]')?.textContent).toBe('3')
  })

  it('shows kind-filter chips with correct counts and toggles node visibility', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(GRAPH)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    const chips = container.querySelector('[data-testid="graph-kind-filters"]')
    expect(chips?.textContent).toContain('plugin (2)')
    expect(chips?.textContent).toContain('role (1)')

    const pluginChip = Array.from(chips?.querySelectorAll('[role="button"]') ?? []).find((el) =>
      el.textContent?.startsWith('plugin'),
    ) as HTMLElement

    await act(async () => {
      pluginChip.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    // Hiding the "plugin" kind drops the two plugin nodes, leaving only "role C".
    expect(container.querySelector('[data-testid="graph-canvas-node-count"]')?.textContent).toBe('1')
  })

  it('opens the detail pane via PanelRenderer when a node is clicked', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(GRAPH)
    fetchGraphNode.mockResolvedValue(DETAIL)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    const nodeButton = Array.from(container.querySelectorAll('button')).find(
      (el) => el.textContent === 'node:a',
    ) as HTMLElement

    await act(async () => {
      nodeButton.click()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(fetchGraphNode).toHaveBeenCalledWith('plugins', 'a')
    const pane = container.querySelector('[data-testid="graph-detail-pane"]')
    expect(pane?.textContent).toContain('Plugin A detail')
    expect(pane?.textContent).toContain('Status')
    expect(pane?.textContent).toContain('ok')
  })

  it('CRITICAL: shows a graceful "requires a <role> token" message on a 403 fetching the graph — not a crash', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockRejectedValue(new ApiForbiddenError())

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[role="alert"]')).toBeNull()
    const forbidden = container.querySelector('[data-testid="graph-forbidden"]')
    expect(forbidden).not.toBeNull()
    expect(forbidden?.textContent).toMatch(/read/i)
  })

  it('CRITICAL: shows a graceful message on a 403 fetching node detail — not a crash', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(GRAPH)
    fetchGraphNode.mockRejectedValue(new ApiForbiddenError())

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    const nodeButton = Array.from(container.querySelectorAll('button')).find(
      (el) => el.textContent === 'node:a',
    ) as HTMLElement

    await act(async () => {
      nodeButton.click()
      await Promise.resolve()
      await Promise.resolve()
    })

    const forbidden = container.querySelector('[data-testid="graph-detail-forbidden"]')
    expect(forbidden).not.toBeNull()
    expect(container.querySelector('[data-testid="graph-detail-pane"] [role="alert"]')).toBeNull()
  })

  it('shows a param input + Load button for a source declaring params, and passes them through on submit', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue({ ...GRAPH, source: 'pipeline', nodes: [], edges: [] })

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    const select = container.querySelector('#graph-source-select') as HTMLSelectElement
    await act(async () => {
      select.value = 'pipeline'
      select.dispatchEvent(new Event('change', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(fetchGraph).toHaveBeenCalledWith('pipeline', {})

    const paramInput = container.querySelector('#graph-param-pipeline') as HTMLInputElement
    expect(paramInput).not.toBeNull()

    await act(async () => {
      setNativeValue(paramInput, 'acme')
    })

    const form = paramInput.closest('form') as HTMLFormElement
    await act(async () => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(fetchGraph).toHaveBeenCalledWith('pipeline', { pipeline: 'acme' })
  })

  it('never renders untrusted graph content via dangerouslySetInnerHTML', () => {
    expect(graphViewSource).not.toContain('dangerouslySetInnerHTML')
  })
})
