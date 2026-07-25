import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { SectionHeader } from './SectionHeader'

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

describe('SectionHeader', () => {
  it('renders the index and title', () => {
    act(() => {
      root.render(<SectionHeader index="01" title="Posture" />)
    })
    expect(container.textContent).toContain('01')
    expect(container.textContent).toContain('Posture')
  })

  it('renders optional right-aligned meta text', () => {
    act(() => {
      root.render(<SectionHeader index="02" title="Flow map" meta="live" />)
    })
    expect(container.querySelector('[data-slot="section-header-meta"]')?.textContent).toBe('live')
  })

  it('omits the meta slot when none is given', () => {
    act(() => {
      root.render(<SectionHeader index="03" title="Swarm" />)
    })
    expect(container.querySelector('[data-slot="section-header-meta"]')).toBeNull()
  })

  it('renders the index in the mono instrument-numeral treatment', () => {
    act(() => {
      root.render(<SectionHeader index="04" title="Efficiency" />)
    })
    const index = container.querySelector('[data-slot="section-header-index"]')
    expect(index?.className).toContain('metric-mono')
  })
})
