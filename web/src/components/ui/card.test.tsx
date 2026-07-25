import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './card'

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

describe('Card', () => {
  it('renders its children through the header/title/description/content slots', () => {
    act(() => {
      root.render(
        <Card>
          <CardHeader>
            <CardTitle>Title</CardTitle>
            <CardDescription>Description</CardDescription>
          </CardHeader>
          <CardContent>Body</CardContent>
        </Card>,
      )
    })
    expect(container.textContent).toContain('Title')
    expect(container.textContent).toContain('Description')
    expect(container.textContent).toContain('Body')
  })

  it('applies the IA/Cyber glass-panel gradient background (panel -> panel-2)', () => {
    act(() => {
      root.render(<Card>content</Card>)
    })
    const card = container.querySelector('[data-slot="card"]')
    expect(card?.className).toMatch(/from-card/)
    expect(card?.className).toMatch(/to-(popover|card)/)
  })

  it('merges a caller-provided className with the base classes', () => {
    act(() => {
      root.render(<Card className="custom-class">content</Card>)
    })
    expect(container.querySelector('[data-slot="card"]')?.className).toContain('custom-class')
  })
})
