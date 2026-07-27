import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiForbiddenError } from '@/lib/api'
import { LANG_STORAGE_KEY, LanguageProvider } from '@/lib/i18n'
import type { GraphData, GraphDetail, GraphSourcesResponse } from '@/lib/pollen-api'
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

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
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
  meta: {},
}

// A `pipeline`-source-shaped graph exposing the run-selector `meta` shape
// (see `parseGraphRunSelector`) — used by the run-selector/live-toggle
// tests below. `live: true` mirrors an in-progress run.
const GRAPH_WITH_LIVE_RUN: GraphData = {
  ...GRAPH,
  source: 'pipeline',
  meta: {
    runs: [
      { id: 2, started_at: '2026-07-20T10:00:00', status: 'running' },
      { id: 1, started_at: '2026-07-19T10:00:00', status: 'complete' },
    ],
    selected_run_id: 2,
    live: true,
  },
}

// Same shape, but the selected run has already finished — `live: false`.
const GRAPH_WITH_FINISHED_RUN: GraphData = {
  ...GRAPH,
  source: 'pipeline',
  meta: {
    runs: [{ id: 1, started_at: '2026-07-19T10:00:00', status: 'complete' }],
    selected_run_id: 1,
    live: false,
  },
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

  it('mobile-first: the canvas + detail-pane row stacks vertically (grid-cols-1) and only goes side-by-side at lg:', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(GRAPH)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    const layoutRow = container.querySelector('[data-testid="graph-layout-row"]')
    expect(layoutRow).not.toBeNull()
    // Single column (stacked: canvas on top, detail pane below) below `lg:`,
    // an explicit two-column row only from `lg:` up — never side-by-side on
    // a narrow screen.
    expect(layoutRow?.className).toContain('grid-cols-1')
    expect(layoutRow?.className).toMatch(/lg:grid-cols-/)
  })

  const ERROR_GRAPH: GraphData = {
    source: 'pipeline',
    nodes: [{ id: 'error', label: 'ValueError', kind: 'error', status: 'error', group: null, badges: [], meta: {} }],
    edges: [],
    layout_hint: null,
    meta: {},
  }

  it('CRITICAL: shows a friendly hint (not a red error node) when a required param is missing', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(ERROR_GRAPH)

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

    const hint = container.querySelector('[data-testid="graph-missing-param-hint"]')
    expect(hint).not.toBeNull()
    expect(hint?.textContent).toContain('pipeline')
    // This fixture declares no enumerable values, so free text is the
    // documented last resort and the Load button is what applies it.
    expect(hint?.textContent).toMatch(/type one above and click Load/i)
    // No scary error card, and no error node handed to the canvas.
    expect(container.querySelector('[data-testid="graph-error-node"]')).toBeNull()
    expect(container.querySelector('[data-testid="graph-canvas-stub"]')).toBeNull()
  })

  it('CRITICAL: shows a clean error message (not a red error node) for a genuine backend error', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(ERROR_GRAPH)

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

    const paramInput = container.querySelector('#graph-param-pipeline') as HTMLInputElement
    await act(async () => {
      setNativeValue(paramInput, 'unknown-pipeline')
    })
    const form = paramInput.closest('form') as HTMLFormElement
    await act(async () => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    const errorCard = container.querySelector('[data-testid="graph-error-node"]')
    expect(errorCard).not.toBeNull()
    expect(errorCard?.textContent).toContain('ValueError')
    expect(container.querySelector('[data-testid="graph-missing-param-hint"]')).toBeNull()
    expect(container.querySelector('[data-testid="graph-canvas-stub"]')).toBeNull()
  })

  it('visual identity: defaults to "status" color-by and toggles to "kind" without re-fetching the graph', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(GRAPH)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    const statusButton = container.querySelector('[data-testid="graph-color-by-status"]') as HTMLElement
    const kindButton = container.querySelector('[data-testid="graph-color-by-kind"]') as HTMLElement
    expect(statusButton.getAttribute('aria-pressed')).toBe('true')
    expect(kindButton.getAttribute('aria-pressed')).toBe('false')

    const fetchCallsBefore = fetchGraph.mock.calls.length
    await act(async () => {
      kindButton.click()
      await Promise.resolve()
    })

    expect(kindButton.getAttribute('aria-pressed')).toBe('true')
    expect(statusButton.getAttribute('aria-pressed')).toBe('false')
    // A purely client-side rendering toggle — never triggers a re-fetch of
    // the already-loaded graph.
    expect(fetchGraph.mock.calls.length).toBe(fetchCallsBefore)
  })

  it('Pollen cascade rebuild: color-by supports a third "role" option alongside status/kind', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(GRAPH)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    const roleButton = container.querySelector('[data-testid="graph-color-by-role"]') as HTMLElement
    expect(roleButton).not.toBeNull()
    expect(roleButton.getAttribute('aria-pressed')).toBe('false')

    await act(async () => {
      roleButton.click()
      await Promise.resolve()
    })

    expect(roleButton.getAttribute('aria-pressed')).toBe('true')
  })

  it('Pollen cascade rebuild: no run selector for a source with no run-selector meta', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(GRAPH)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="graph-run-selector"]')).toBeNull()
  })

  it('Pollen cascade rebuild: shows a run selector + Live toggle for a live run, and re-fetches with run_id on selection', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(GRAPH_WITH_LIVE_RUN)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    // Switch to the `pipeline` source (the `plugins` default has no run
    // concept) — mirrors the existing "shows a param input…" test's
    // source-switch flow.
    const select = container.querySelector('#graph-source-select') as HTMLSelectElement
    await act(async () => {
      select.value = 'pipeline'
      select.dispatchEvent(new Event('change', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    const runSelect = container.querySelector('#graph-run-select') as HTMLSelectElement
    expect(runSelect).not.toBeNull()
    expect(runSelect.textContent).toContain('#2')
    expect(runSelect.textContent).toContain('#1')

    const liveToggle = container.querySelector('[data-testid="graph-live-toggle"]') as HTMLElement
    expect(liveToggle).not.toBeNull()
    expect(liveToggle.getAttribute('aria-pressed')).toBe('true')

    await act(async () => {
      runSelect.value = '1'
      runSelect.dispatchEvent(new Event('change', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(fetchGraph).toHaveBeenLastCalledWith('pipeline', { run_id: '1' })
  })

  it('Pollen cascade rebuild: hides the Live toggle for a finished (non-live) run', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(GRAPH_WITH_FINISHED_RUN)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.querySelector('[data-testid="graph-run-selector"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="graph-live-toggle"]')).toBeNull()
  })

  it('Pollen cascade rebuild: Live toggle polls (re-fetches) on an interval while a live run is displayed', async () => {
    vi.useFakeTimers()
    try {
      fetchGraphSources.mockResolvedValue(SOURCES)
      fetchGraph.mockResolvedValue(GRAPH_WITH_LIVE_RUN)

      await act(async () => {
        mount()
        await Promise.resolve()
        await Promise.resolve()
        await Promise.resolve()
      })

      const callsBefore = fetchGraph.mock.calls.length

      await act(async () => {
        vi.advanceTimersByTime(5000)
        await Promise.resolve()
        await Promise.resolve()
      })

      expect(fetchGraph.mock.calls.length).toBeGreaterThan(callsBefore)
    } finally {
      vi.useRealTimers()
    }
  })

  it('Pollen cascade rebuild: turning the Live toggle off stops polling', async () => {
    vi.useFakeTimers()
    try {
      fetchGraphSources.mockResolvedValue(SOURCES)
      fetchGraph.mockResolvedValue(GRAPH_WITH_LIVE_RUN)

      await act(async () => {
        mount()
        await Promise.resolve()
        await Promise.resolve()
        await Promise.resolve()
      })

      const liveToggle = container.querySelector('[data-testid="graph-live-toggle"]') as HTMLElement
      await act(async () => {
        liveToggle.dispatchEvent(new MouseEvent('click', { bubbles: true }))
        await Promise.resolve()
      })
      expect(liveToggle.getAttribute('aria-pressed')).toBe('false')

      const callsBefore = fetchGraph.mock.calls.length

      await act(async () => {
        vi.advanceTimersByTime(15000)
        await Promise.resolve()
        await Promise.resolve()
      })

      expect(fetchGraph.mock.calls.length).toBe(callsBefore)
    } finally {
      vi.useRealTimers()
    }
  })

  it('Pollen cascade rebuild: the Reload button re-fetches the graph with the same params', async () => {
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(GRAPH)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    const callsBefore = fetchGraph.mock.calls.length
    const reloadButton = container.querySelector('[data-testid="graph-reload-button"]') as HTMLElement
    expect(reloadButton).not.toBeNull()

    await act(async () => {
      reloadButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(fetchGraph.mock.calls.length).toBe(callsBefore + 1)
    expect(fetchGraph).toHaveBeenLastCalledWith('plugins', {})
  })

  it('renders French title and copy when the language is fr (P1a)', async () => {
    window.localStorage.setItem(LANG_STORAGE_KEY, JSON.stringify('fr'))
    fetchGraphSources.mockResolvedValue(SOURCES)
    fetchGraph.mockResolvedValue(GRAPH)

    await act(async () => {
      root.render(
        <LanguageProvider>
          <GraphView />
        </LanguageProvider>,
      )
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(container.textContent).toContain('Graphe')
    expect(container.textContent).toContain('Source')
    expect(container.textContent).toContain('Sélectionnez un nœud pour voir le détail.')
  })

  // -------------------------------------------------------------------------
  // Content first: the Graph tab must never open empty behind a form.
  // In production `GET /v1/graph/sources` is sorted by name, so "pipeline"
  // (which REQUIRES a `?pipeline=`) came first and the old `sources[0]`
  // default rendered nothing until the operator guessed a pipeline name.
  // -------------------------------------------------------------------------

  it('CRITICAL: defaults to a source that needs no parameter, even when a param-requiring one sorts first', async () => {
    fetchGraphSources.mockResolvedValue({
      sources: [
        { name: 'pipeline', title: 'Pipeline', min_role: 'read', params: ['pipeline'] },
        { name: 'plugins', title: 'Plugins', min_role: 'read', params: [] },
      ],
    })
    fetchGraph.mockResolvedValue(GRAPH)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect((container.querySelector('#graph-source-select') as HTMLSelectElement).value).toBe('plugins')
    expect(container.querySelector('[data-testid="graph-missing-param-hint"]')).toBeNull()
    expect(container.querySelector('[data-testid="graph-canvas-stub"]')).not.toBeNull()
  })

  it('falls back to the first source when every registered source takes a parameter', async () => {
    fetchGraphSources.mockResolvedValue({
      sources: [{ name: 'pipeline', title: 'Pipeline', min_role: 'read', params: ['pipeline'] }],
    })
    fetchGraph.mockResolvedValue(GRAPH)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect((container.querySelector('#graph-source-select') as HTMLSelectElement).value).toBe('pipeline')
  })

  it('CRITICAL: an enumerable parameter renders a pick-list, not a free-text box, and applies on selection', async () => {
    fetchGraphSources.mockResolvedValue({
      sources: [
        {
          name: 'pipeline',
          title: 'Pipeline',
          min_role: 'read',
          params: ['pipeline'],
          param_options: { pipeline: ['nightly', 'release'] },
        },
      ],
    })
    fetchGraph.mockResolvedValue(GRAPH)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    const field = container.querySelector('#graph-param-pipeline') as HTMLSelectElement
    expect(field.tagName).toBe('SELECT')
    expect(Array.from(field.querySelectorAll('option')).map((o) => o.value)).toEqual([
      '',
      'nightly',
      'release',
    ])
    // Nothing to type means nothing to submit.
    expect(field.closest('form')?.querySelector('button[type="submit"]')).toBeNull()

    await act(async () => {
      field.value = 'release'
      field.dispatchEvent(new Event('change', { bubbles: true }))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(fetchGraph).toHaveBeenLastCalledWith('pipeline', { pipeline: 'release' })
  })

  it('keeps a free-text box, with its Load button, for a parameter the backend cannot enumerate', async () => {
    fetchGraphSources.mockResolvedValue({
      sources: [
        { name: 'pipeline', title: 'Pipeline', min_role: 'read', params: ['pipeline'], param_options: {} },
      ],
    })
    fetchGraph.mockResolvedValue(GRAPH)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    const field = container.querySelector('#graph-param-pipeline') as HTMLInputElement
    expect(field.tagName).toBe('INPUT')
    expect(field.closest('form')?.querySelector('button[type="submit"]')).not.toBeNull()
  })

  it('the missing-parameter state tells the operator to choose, not to guess, when values are enumerable', async () => {
    fetchGraphSources.mockResolvedValue({
      sources: [
        {
          name: 'pipeline',
          title: 'Pipeline',
          min_role: 'read',
          params: ['pipeline'],
          param_options: { pipeline: ['nightly'] },
        },
      ],
    })
    fetchGraph.mockResolvedValue(ERROR_GRAPH)

    await act(async () => {
      mount()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    const hint = container.querySelector('[data-testid="graph-missing-param-hint"]')
    expect(hint).not.toBeNull()
    expect(hint?.textContent).toMatch(/choose one from the selector above/i)
    expect(hint?.textContent).not.toMatch(/click Load/i)
  })
})
