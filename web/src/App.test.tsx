import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import App from './App'

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
})

describe('App', () => {
  it('shows the token gate (not the Mirador tabs) when no token is stored', () => {
    act(() => {
      root.render(<App />)
    })

    expect(container.querySelector('input[aria-label="HivePilot read token"]')).not.toBeNull()
    expect(container.querySelector('[role="tab"]')).toBeNull()
  })
})
