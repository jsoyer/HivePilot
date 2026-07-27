import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { Tabs, TabsContent, TabsList, TabsTrigger } from './tabs'

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

function renderInnerTabs(outerOrientation: 'horizontal' | 'vertical') {
  act(() => {
    root.render(
      <Tabs orientation={outerOrientation} defaultValue="shell">
        <TabsContent value="shell">
          <Tabs defaultValue="a">
            <TabsList data-testid="inner-list">
              <TabsTrigger value="a">A</TabsTrigger>
              <TabsTrigger value="b">B</TabsTrigger>
            </TabsList>
            <TabsContent value="a">panel a</TabsContent>
          </Tabs>
        </TabsContent>
      </Tabs>,
    )
  })
  return container.querySelector('[data-testid="inner-list"]') as HTMLElement
}

describe('Tabs', () => {
  it('renders triggers and the active panel', () => {
    act(() => {
      root.render(
        <Tabs defaultValue="a">
          <TabsList>
            <TabsTrigger value="a">Quality</TabsTrigger>
            <TabsTrigger value="b">Growth</TabsTrigger>
          </TabsList>
          <TabsContent value="a">quality body</TabsContent>
        </Tabs>,
      )
    })
    expect(container.textContent).toContain('Quality')
    expect(container.textContent).toContain('Growth')
    expect(container.textContent).toContain('quality body')
  })

  it('marks a horizontal list with its own data-orientation attribute', () => {
    act(() => {
      root.render(
        <Tabs defaultValue="a">
          <TabsList data-testid="list">
            <TabsTrigger value="a">A</TabsTrigger>
          </TabsList>
        </Tabs>,
      )
    })
    const list = container.querySelector('[data-testid="list"]')
    expect(list?.getAttribute('data-orientation')).toBe('horizontal')
  })

  // Regression: Pollen's app shell mounts a VERTICAL `Tabs` root (the
  // sidebar nav), and views such as Memory mount their own horizontal
  // `Tabs` INSIDE it. The inner list must never inherit the outer root's
  // orientation styling -- that bug rendered the Memory tab bar as a
  // vertical stack in a tiny box.
  it('does not let a nested horizontal list inherit an outer vertical root', () => {
    const insideVertical = renderInnerTabs('vertical')
    expect(insideVertical.getAttribute('data-orientation')).toBe('horizontal')
    // Orientation-dependent styling must key off the element's OWN
    // data-orientation (`data-vertical:`), never a `group/tabs` ancestor
    // whose name collides across nested Tabs instances.
    expect(insideVertical.className).not.toMatch(/group-data-vertical\/tabs/)
    expect(insideVertical.className).not.toMatch(/group-data-horizontal\/tabs/)
  })

  it('keeps the same list classes whatever the outer root orientation is', () => {
    const insideVertical = renderInnerTabs('vertical').className
    act(() => {
      root.unmount()
    })
    root = createRoot(container)
    const insideHorizontal = renderInnerTabs('horizontal').className
    expect(insideVertical).toBe(insideHorizontal)
  })

  it('marks triggers with their own data-orientation attribute', () => {
    act(() => {
      root.render(
        <Tabs orientation="vertical" defaultValue="a">
          <TabsList>
            <TabsTrigger value="a" data-testid="trigger">
              A
            </TabsTrigger>
          </TabsList>
        </Tabs>,
      )
    })
    expect(container.querySelector('[data-testid="trigger"]')?.getAttribute('data-orientation')).toBe(
      'vertical',
    )
  })
})
