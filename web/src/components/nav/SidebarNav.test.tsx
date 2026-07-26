import { Activity, DollarSign, HeartPulse } from 'lucide-react'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Tabs, TabsContent } from '@/components/ui/tabs'
import type { NavGroup } from './nav-config'
import { SidebarNav } from './SidebarNav'

let container: HTMLDivElement
let root: Root

const groups: NavGroup[] = [
  {
    label: "Vue d'ensemble",
    items: [
      { value: 'analytics', label: 'Analytics', Icon: Activity },
      { value: 'cost', label: 'Cost', Icon: DollarSign },
    ],
  },
  {
    label: 'Système',
    items: [{ value: 'health', label: 'Health', Icon: HeartPulse }],
  },
]

function Harness({ mobileOpen = false, onCloseMobile = () => {} }: { mobileOpen?: boolean; onCloseMobile?: () => void }) {
  return (
    <Tabs defaultValue="analytics" orientation="vertical">
      <SidebarNav groups={groups} mobileOpen={mobileOpen} onCloseMobile={onCloseMobile} />
      <div>
        <TabsContent value="analytics">Analytics panel</TabsContent>
        <TabsContent value="cost">Cost panel</TabsContent>
        <TabsContent value="health">Health panel</TabsContent>
      </div>
    </Tabs>
  )
}

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
  vi.restoreAllMocks()
})

function click(el: Element) {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

describe('SidebarNav', () => {
  it('renders every group label and every item across groups', () => {
    act(() => {
      root.render(<Harness />)
    })
    expect(container.textContent).toContain("Vue d'ensemble")
    expect(container.textContent).toContain('Système')

    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toEqual(['Analytics', 'Cost', 'Health'])
  })

  it('clicking an item switches the active view', () => {
    act(() => {
      root.render(<Harness />)
    })
    const costTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Cost',
    ) as HTMLElement

    act(() => {
      click(costTab)
    })

    expect(costTab.getAttribute('aria-selected')).toBe('true')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toBe('Cost panel')
  })

  it('collapse toggle flips the collapsed state and persists it to localStorage', () => {
    act(() => {
      root.render(<Harness />)
    })
    const nav = container.querySelector('[data-slot="sidebar-nav"]') as HTMLElement
    expect(nav.getAttribute('data-collapsed')).toBe('false')

    const collapseButton = container.querySelector('[data-testid="sidebar-collapse-toggle"]') as HTMLElement
    act(() => {
      click(collapseButton)
    })

    expect(nav.getAttribute('data-collapsed')).toBe('true')
    expect(window.localStorage.getItem('hivepilot.webui.sidebar-collapsed')).toBe('true')

    act(() => {
      click(collapseButton)
    })
    expect(nav.getAttribute('data-collapsed')).toBe('false')
    expect(window.localStorage.getItem('hivepilot.webui.sidebar-collapsed')).toBe('false')
  })

  it('starts collapsed when a previous session persisted collapsed=true', () => {
    window.localStorage.setItem('hivepilot.webui.sidebar-collapsed', 'true')
    act(() => {
      root.render(<Harness />)
    })
    const nav = container.querySelector('[data-slot="sidebar-nav"]') as HTMLElement
    expect(nav.getAttribute('data-collapsed')).toBe('true')
  })

  it('mobile: closed by default, no backdrop rendered', () => {
    act(() => {
      root.render(<Harness mobileOpen={false} />)
    })
    const nav = container.querySelector('[data-slot="sidebar-nav"]') as HTMLElement
    expect(nav.getAttribute('data-mobile-open')).toBe('false')
    expect(container.querySelector('[data-testid="sidebar-backdrop"]')).toBeNull()
  })

  it('mobile: open renders a backdrop and marks the nav open', () => {
    act(() => {
      root.render(<Harness mobileOpen={true} />)
    })
    const nav = container.querySelector('[data-slot="sidebar-nav"]') as HTMLElement
    expect(nav.getAttribute('data-mobile-open')).toBe('true')
    expect(container.querySelector('[data-testid="sidebar-backdrop"]')).not.toBeNull()
  })

  it('mobile: clicking an item calls onCloseMobile (item-click closes the drawer)', () => {
    const onCloseMobile = vi.fn()
    act(() => {
      root.render(<Harness mobileOpen={true} onCloseMobile={onCloseMobile} />)
    })
    const healthTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Health',
    ) as HTMLElement

    act(() => {
      click(healthTab)
    })

    expect(onCloseMobile).toHaveBeenCalled()
  })

  it('mobile: clicking the backdrop calls onCloseMobile', () => {
    const onCloseMobile = vi.fn()
    act(() => {
      root.render(<Harness mobileOpen={true} onCloseMobile={onCloseMobile} />)
    })
    const backdrop = container.querySelector('[data-testid="sidebar-backdrop"]') as HTMLElement

    act(() => {
      click(backdrop)
    })

    expect(onCloseMobile).toHaveBeenCalled()
  })

  it('every item is a real button with a >=40px (h-10) tap target class', () => {
    act(() => {
      root.render(<Harness />)
    })
    const tabs = Array.from(container.querySelectorAll('[role="tab"]')) as HTMLElement[]
    expect(tabs.length).toBeGreaterThan(0)
    for (const tab of tabs) {
      expect(tab.tagName).toBe('BUTTON')
      expect(tab.className).toMatch(/min-h-10/)
    }
  })

  it('BUG FIX: the nav list starts at the top of the sidebar (no vertical centering gap)', () => {
    // Regression for the "nav starts ~1/3 down the page" bug: the TabsList's
    // CVA base classes include `justify-center` (shared with the horizontal
    // tab-bar use case) — SidebarNav must override it to `justify-start` so
    // a short group list doesn't get centered inside the flex-1 column that
    // fills the sidebar's full height.
    act(() => {
      root.render(<Harness />)
    })
    const list = container.querySelector('[data-slot="tabs-list"]') as HTMLElement
    expect(list.className).toContain('justify-start')
    expect(list.className).not.toContain('justify-center')
  })

  it('BUG FIX: the persistent (non-drawer) sidebar breakpoint is md, not lg — a realistic, non-maximized desktop window must never fall into off-canvas drawer mode', () => {
    // Regression for "clicking a nav item makes the whole menu disappear":
    // at the old `lg:` (1024px) breakpoint, any normal desktop browser
    // window narrower than 1024px CSS px (common: split-screen, non-
    // maximized windows, smaller laptops) was treated as "mobile" and every
    // item click closed the off-canvas drawer via `onCloseMobile`,
    // translating the whole sidebar off-screen with no visible way back.
    act(() => {
      root.render(<Harness />)
    })
    const nav = container.querySelector('[data-slot="sidebar-nav"]') as HTMLElement
    expect(nav.className).toContain('md:static')
    expect(nav.className).toContain('md:translate-x-0')
    expect(nav.className).not.toMatch(/\blg:static\b/)
    expect(nav.className).not.toMatch(/\blg:translate-x-0\b/)
  })

  it('IA/Cyber identity: every item has a leading status dot, and the active item is tinted/striped', () => {
    act(() => {
      root.render(<Harness />)
    })
    const analyticsTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Analytics',
    ) as HTMLElement
    expect(analyticsTab.querySelector('[data-slot="nav-item-dot"]')).not.toBeNull()
    // `defaultValue="analytics"` in the harness — this trigger starts active.
    expect(analyticsTab.getAttribute('aria-selected')).toBe('true')
    expect(analyticsTab.className).toMatch(/data-active:/)

    const costTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Cost',
    ) as HTMLElement
    act(() => {
      click(costTab)
    })
    expect(costTab.getAttribute('aria-selected')).toBe('true')
    expect(analyticsTab.getAttribute('aria-selected')).toBe('false')
  })
})
