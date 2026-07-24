import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { usePersistedState } from './use-persisted-state'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  window.localStorage.clear()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
  window.localStorage.clear()
})

function Probe({ storageKey, defaultValue }: { storageKey: string; defaultValue: boolean }) {
  const [value, setValue] = usePersistedState(storageKey, defaultValue)
  return (
    <div>
      <span data-testid="value">{String(value)}</span>
      <button data-testid="toggle" onClick={() => setValue((prev) => !prev)}>
        toggle
      </button>
    </div>
  )
}

function readValue(): string {
  return (container.querySelector('[data-testid="value"]') as HTMLElement).textContent ?? ''
}

function click(el: Element) {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

describe('usePersistedState', () => {
  it('starts from the default value when nothing is stored', () => {
    act(() => {
      root.render(<Probe storageKey="hivepilot.webui.test-flag" defaultValue={false} />)
    })
    expect(readValue()).toBe('false')
  })

  it('starts from a previously-persisted value, ignoring the default', () => {
    window.localStorage.setItem('hivepilot.webui.test-flag', 'true')
    act(() => {
      root.render(<Probe storageKey="hivepilot.webui.test-flag" defaultValue={false} />)
    })
    expect(readValue()).toBe('true')
  })

  it('persists updates to localStorage as they happen', () => {
    act(() => {
      root.render(<Probe storageKey="hivepilot.webui.test-flag" defaultValue={false} />)
    })
    act(() => {
      click(container.querySelector('[data-testid="toggle"]') as HTMLElement)
    })
    expect(readValue()).toBe('true')
    expect(window.localStorage.getItem('hivepilot.webui.test-flag')).toBe('true')
  })

  it('ignores a malformed stored value and falls back to the default', () => {
    window.localStorage.setItem('hivepilot.webui.test-flag', 'not-json{{{')
    act(() => {
      root.render(<Probe storageKey="hivepilot.webui.test-flag" defaultValue={true} />)
    })
    expect(readValue()).toBe('true')
  })
})
