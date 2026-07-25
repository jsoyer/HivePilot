import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SweepRadar } from './SweepRadar'

let container: HTMLDivElement
let root: Root

function makeMockContext() {
  return {
    clearRect: vi.fn(),
    beginPath: vi.fn(),
    arc: vi.fn(),
    stroke: vi.fn(),
    fill: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    save: vi.fn(),
    restore: vi.fn(),
    setTransform: vi.fn(),
    strokeStyle: '',
    fillStyle: '',
    lineWidth: 0,
    globalAlpha: 1,
  }
}

function mockMatchMedia(reducedMotion: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query.includes('prefers-reduced-motion') ? reducedMotion : false,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia
}

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  HTMLCanvasElement.prototype.getContext = vi.fn(() => makeMockContext()) as unknown as typeof HTMLCanvasElement.prototype.getContext
  mockMatchMedia(false)
  // jsdom's rAF/cAF support varies by version — fall back to a timer-backed
  // shim so `vi.spyOn` always has something real to wrap.
  if (typeof window.requestAnimationFrame !== 'function') {
    window.requestAnimationFrame = ((cb: FrameRequestCallback) =>
      setTimeout(() => cb(performance.now()), 16) as unknown as number) as typeof window.requestAnimationFrame
  }
  if (typeof window.cancelAnimationFrame !== 'function') {
    window.cancelAnimationFrame = ((id: number) => clearTimeout(id)) as typeof window.cancelAnimationFrame
  }
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
  vi.restoreAllMocks()
})

describe('SweepRadar', () => {
  it('renders a canvas with an aria-label summarizing the agents', () => {
    act(() => {
      root.render(
        <SweepRadar
          agents={[{ status: 'good' }, { status: 'warn' }, { status: 'crit' }, { status: 'idle' }]}
        />,
      )
    })
    const canvas = container.querySelector('[data-slot="sweep-radar"]')
    expect(canvas).not.toBeNull()
    expect(canvas?.tagName).toBe('CANVAS')
    expect(canvas?.getAttribute('role')).toBe('img')
    expect(canvas?.getAttribute('aria-label')).toContain('4 agents')
  })

  it('accepts a custom aria-label', () => {
    act(() => {
      root.render(<SweepRadar agents={[]} ariaLabel="Custom radar" />)
    })
    expect(container.querySelector('[data-slot="sweep-radar"]')?.getAttribute('aria-label')).toBe('Custom radar')
  })

  it('is empty-safe: an empty agents array renders without crashing', () => {
    expect(() => {
      act(() => {
        root.render(<SweepRadar agents={[]} />)
      })
    }).not.toThrow()
    expect(container.querySelector('[data-slot="sweep-radar"]')).not.toBeNull()
  })

  it('starts a requestAnimationFrame sweep loop when motion is not reduced', () => {
    const rafSpy = vi.spyOn(window, 'requestAnimationFrame')
    act(() => {
      root.render(<SweepRadar agents={[{ status: 'good' }]} />)
    })
    expect(rafSpy).toHaveBeenCalled()
  })

  it('renders statically (no rAF loop) when prefers-reduced-motion is set', () => {
    mockMatchMedia(true)
    const rafSpy = vi.spyOn(window, 'requestAnimationFrame')
    act(() => {
      root.render(<SweepRadar agents={[{ status: 'good' }]} />)
    })
    expect(rafSpy).not.toHaveBeenCalled()
  })

  it('cancels the animation frame on unmount', () => {
    const cancelSpy = vi.spyOn(window, 'cancelAnimationFrame')
    act(() => {
      root.render(<SweepRadar agents={[{ status: 'good' }]} />)
    })
    act(() => {
      root.unmount()
    })
    expect(cancelSpy).toHaveBeenCalled()
  })

  it('does not crash when the canvas context is unavailable (e.g. jsdom without a canvas backend)', () => {
    HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as unknown as typeof HTMLCanvasElement.prototype.getContext
    expect(() => {
      act(() => {
        root.render(<SweepRadar agents={[{ status: 'good' }]} />)
      })
    }).not.toThrow()
  })
})
