import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useEscapeKey } from './use-escape-key'

let container: HTMLDivElement
let root: Root

function Harness({ enabled, onEscape }: { enabled: boolean; onEscape: () => void }) {
  useEscapeKey(enabled, onEscape)
  return null
}

function render(enabled: boolean, onEscape: () => void) {
  act(() => {
    root.render(<Harness enabled={enabled} onEscape={onEscape} />)
  })
}

function pressKey(key: string) {
  act(() => {
    window.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }))
  })
}

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
})

describe('useEscapeKey', () => {
  it('calls the handler when Escape is pressed while enabled', () => {
    const onEscape = vi.fn()
    render(true, onEscape)
    pressKey('Escape')
    expect(onEscape).toHaveBeenCalledTimes(1)
  })

  it('ignores every other key', () => {
    const onEscape = vi.fn()
    render(true, onEscape)
    for (const key of ['Enter', 'a', 'Tab', 'ArrowDown', 'Esc']) pressKey(key)
    expect(onEscape).not.toHaveBeenCalled()
  })

  // The whole point of the `enabled` flag: a closed drawer must not steal
  // Escape from whatever else is on screen (the command palette, a native
  // <select> popup, a browser find bar).
  it('does not listen at all while disabled', () => {
    const onEscape = vi.fn()
    render(false, onEscape)
    pressKey('Escape')
    expect(onEscape).not.toHaveBeenCalled()
  })

  it('starts and stops listening as enabled flips', () => {
    const onEscape = vi.fn()
    render(false, onEscape)
    pressKey('Escape')
    expect(onEscape).not.toHaveBeenCalled()

    render(true, onEscape)
    pressKey('Escape')
    expect(onEscape).toHaveBeenCalledTimes(1)

    render(false, onEscape)
    pressKey('Escape')
    expect(onEscape).toHaveBeenCalledTimes(1)
  })

  it('removes its listener on unmount so it cannot fire against a dead component', () => {
    const onEscape = vi.fn()
    render(true, onEscape)
    act(() => {
      root.unmount()
    })
    pressKey('Escape')
    expect(onEscape).not.toHaveBeenCalled()

    // Re-establish a root so the shared afterEach unmount stays valid.
    root = createRoot(container)
  })

  // A caller that passes an inline arrow (every caller, in practice) would
  // otherwise tear down and re-add the listener on every single render.
  it('keeps working when the handler identity changes every render', () => {
    const calls: string[] = []
    render(true, () => calls.push('first'))
    render(true, () => calls.push('second'))
    pressKey('Escape')
    expect(calls).toEqual(['second'])
  })
})
