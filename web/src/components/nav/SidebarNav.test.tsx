import { Bell, CheckSquare, Inbox, PlayCircle } from 'lucide-react'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Tabs, TabsContent } from '@/components/ui/tabs'
import type { NavItem } from './nav-config'
import { SidebarNav } from './SidebarNav'

let container: HTMLDivElement
let root: Root

const primary: NavItem[] = [
  { value: 'inbox', label: 'Inbox', Icon: Inbox },
  { value: 'approvals', label: 'Approvals', Icon: CheckSquare },
  { value: 'runs', label: 'Runs', Icon: PlayCircle },
  { value: 'alerts', label: 'Alerts', Icon: Bell },
]

const plusItems: NavItem[] = [
  { value: 'spaces', label: 'Rooms', Icon: Inbox },
  { value: 'cost', label: 'Spend', Icon: Inbox },
]

function Harness({
  mobileOpen = false,
  onCloseMobile = () => {},
  alertsCount = 0,
}: {
  mobileOpen?: boolean
  onCloseMobile?: () => void
  alertsCount?: number
}) {
  return (
    <Tabs defaultValue="inbox" orientation="vertical">
      <SidebarNav
        primary={primary}
        plusItems={plusItems}
        plusLabel="Plus"
        plusHint="Rest via ⌘K"
        alertsCount={alertsCount}
        mobileOpen={mobileOpen}
        onCloseMobile={onCloseMobile}
      />
      <div>
        <TabsContent value="inbox">Inbox panel</TabsContent>
        <TabsContent value="approvals">Approvals panel</TabsContent>
        <TabsContent value="runs">Runs panel</TabsContent>
        <TabsContent value="alerts">Alerts panel</TabsContent>
        <TabsContent value="spaces">Rooms panel</TabsContent>
        <TabsContent value="cost">Spend panel</TabsContent>
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
  it('renders the four doors plus the Plus tray', () => {
    act(() => {
      root.render(<Harness />)
    })
    expect(container.textContent).toContain('Plus')
    expect(container.textContent).toContain('Rest via ⌘K')
    const tabs = Array.from(container.querySelectorAll('[role="tab"]')).map((el) => el.textContent)
    expect(tabs).toEqual(['Inbox', 'Approvals', 'Runs', 'Alerts', 'Rooms', 'Spend'])
    expect(container.querySelector('[data-slot="nav-item-dot"]')).toBeNull()
  })

  it('clicking an item switches the active view', () => {
    act(() => {
      root.render(<Harness />)
    })
    const runsTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Runs',
    ) as HTMLElement

    act(() => {
      click(runsTab)
    })

    expect(runsTab.getAttribute('aria-selected')).toBe('true')
    const panel = container.querySelector('[role="tabpanel"]')
    expect(panel?.textContent).toBe('Runs panel')
  })

  it('shows an Alerts badge only when the count is positive', () => {
    act(() => {
      root.render(<Harness alertsCount={0} />)
    })
    expect(container.querySelector('[data-testid="alerts-badge"]')).toBeNull()

    act(() => {
      root.render(<Harness alertsCount={3} />)
    })
    expect(container.querySelector('[data-testid="alerts-badge"]')?.textContent).toBe('3')
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

  it('mobile: clicking an item calls onCloseMobile', () => {
    const onCloseMobile = vi.fn()
    act(() => {
      root.render(<Harness mobileOpen={true} onCloseMobile={onCloseMobile} />)
    })
    const alertsTab = Array.from(container.querySelectorAll('[role="tab"]')).find(
      (el) => el.textContent === 'Alerts',
    ) as HTMLElement

    act(() => {
      click(alertsTab)
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

  it('every item is a real button with a >=40px tap target class', () => {
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

  it('the nav list starts at the top of the sidebar (no vertical centering gap)', () => {
    act(() => {
      root.render(<Harness />)
    })
    const list = container.querySelector('[data-slot="tabs-list"]') as HTMLElement
    expect(list.className).toContain('justify-start')
    expect(list.className).not.toContain('justify-center')
  })

  it('the persistent sidebar breakpoint is md, not lg', () => {
    act(() => {
      root.render(<Harness />)
    })
    const nav = container.querySelector('[data-slot="sidebar-nav"]') as HTMLElement
    expect(nav.className).toContain('md:static')
    expect(nav.className).toContain('md:translate-x-0')
    expect(nav.className).not.toMatch(/\blg:static\b/)
    expect(nav.className).not.toMatch(/\blg:translate-x-0\b/)
  })

  describe('mobile drawer dismissal', () => {
    function pressEscape() {
      act(() => {
        window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      })
    }

    it('closes on Escape while open', () => {
      const onCloseMobile = vi.fn()
      act(() => {
        root.render(<Harness mobileOpen onCloseMobile={onCloseMobile} />)
      })
      pressEscape()
      expect(onCloseMobile).toHaveBeenCalledTimes(1)
    })

    it('does not consume Escape while closed', () => {
      const onCloseMobile = vi.fn()
      act(() => {
        root.render(<Harness mobileOpen={false} onCloseMobile={onCloseMobile} />)
      })
      pressEscape()
      expect(onCloseMobile).not.toHaveBeenCalled()
    })

    it('announces itself as a modal dialog only while open', () => {
      act(() => {
        root.render(<Harness mobileOpen />)
      })
      const openPanel = container.querySelector('[data-slot="sidebar-nav"]')!
      expect(openPanel.getAttribute('role')).toBe('dialog')
      expect(openPanel.getAttribute('aria-modal')).toBe('true')
      expect(openPanel.getAttribute('aria-label')).toBe('Navigation')

      act(() => {
        root.render(<Harness mobileOpen={false} />)
      })
      const closedPanel = container.querySelector('[data-slot="sidebar-nav"]')!
      expect(closedPanel.getAttribute('role')).toBeNull()
      expect(closedPanel.getAttribute('aria-modal')).toBeNull()
    })

    it('gates its slide-in animation behind motion-safe', () => {
      act(() => {
        root.render(<Harness mobileOpen />)
      })
      const panel = container.querySelector('[data-slot="sidebar-nav"]')!
      expect(panel.className).toContain('motion-safe:transition-transform')
      expect(panel.className).not.toMatch(/(^|\s)transition-transform/)
    })
  })
})
