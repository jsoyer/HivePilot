import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { Select } from './select'

let container: HTMLDivElement
let root: Root

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

describe('Select', () => {
  it('renders a real native select so it is keyboard- and screen-reader-native', () => {
    act(() => {
      root.render(
        <Select value="a" onChange={() => {}} aria-label="pick">
          <option value="a">A</option>
          <option value="b">B</option>
        </Select>,
      )
    })
    const select = container.querySelector('select')
    expect(select).not.toBeNull()
    expect(select?.value).toBe('a')
    expect(select?.querySelectorAll('option')).toHaveLength(2)
  })

  it('carries a visible focus ring class (keyboard focus must be visible)', () => {
    act(() => {
      root.render(<Select value="" onChange={() => {}} aria-label="pick" />)
    })
    expect(container.querySelector('select')?.className).toMatch(/focus-visible:ring/)
  })

  it('merges a caller className', () => {
    act(() => {
      root.render(<Select value="" onChange={() => {}} aria-label="pick" className="custom-class" />)
    })
    expect(container.querySelector('select')?.className).toContain('custom-class')
  })

  it('forwards disabled', () => {
    act(() => {
      root.render(<Select value="" onChange={() => {}} aria-label="pick" disabled />)
    })
    expect(container.querySelector('select')?.disabled).toBe(true)
  })
})
