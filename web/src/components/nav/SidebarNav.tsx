import { ChevronLeft, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { TabsList, TabsTrigger } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'
import { usePersistedState } from '@/lib/use-persisted-state'
import type { NavGroup } from './nav-config'

const COLLAPSED_STORAGE_KEY = 'hivepilot.webui.sidebar-collapsed'

export interface SidebarNavProps {
  groups: NavGroup[]
  /** Mobile off-canvas drawer open state — owned by the header's hamburger
   * button (`Pollen.tsx`), not this component, since the trigger lives
   * outside the sidebar itself. */
  mobileOpen: boolean
  /** Called when the drawer should close: backdrop click, or any item click
   * (navigating on mobile should always close the drawer behind it). */
  onCloseMobile: () => void
}

/**
 * Left sidebar navigation (Mirador → "Vigie" dashboard upgrade, P0b) —
 * replaces the old flat top tab bar. Renders the SAME underlying
 * `Tabs`/`TabsList`/`TabsTrigger` primitives `Pollen.tsx` already used for
 * the tab bar (just restyled + grouped), so the existing uncontrolled
 * `Tabs` value/routing state is completely unchanged — this component only
 * changes the nav UI that sets it. One `TabsList` instance (not one per
 * group) — Base UI's Tabs root manages a SINGLE shared tab registry
 * (`tabMap`) fed by whichever `TabsList` last registers into it, so multiple
 * `TabsList`s under one `Tabs` root would clobber each other's tabs; group
 * headers are therefore plain non-interactive `<span>`s interleaved inside
 * one list, which Base UI's composite keyboard navigation simply skips
 * (only registered `Tab` children participate).
 *
 * Desktop (`md:` and up): a static, always-visible aside. `collapsed`
 * persists to localStorage (`usePersistedState`) so a reload keeps the
 * operator's choice; icon-only mode hides every label (and un-hides the
 * group header for screen readers via `sr-only` rather than removing it).
 * Bug fix: this used to be `lg:` (1024px) — any normal, non-maximized
 * desktop browser window narrower than that (split-screen, a smaller
 * laptop, etc.) was silently treated as "mobile", so clicking ANY nav item
 * called `onCloseMobile` and translated the whole sidebar off-screen with
 * no visible way back ("the whole menu disappears"). `md:` (768px) is a
 * much more realistic floor for "this is a desktop window, not a phone" —
 * Pollen is an ops console, not a page that's ever hand-held.
 *
 * Mobile (below `md:`): an off-canvas drawer (`fixed`, translated out of
 * view by default) plus a click-to-close backdrop, controlled entirely by
 * `mobileOpen`/`onCloseMobile` props from the header's hamburger button.
 */
export function SidebarNav({ groups, mobileOpen, onCloseMobile }: SidebarNavProps) {
  const [collapsed, setCollapsed] = usePersistedState(COLLAPSED_STORAGE_KEY, false)

  return (
    <>
      {mobileOpen && (
        <div
          data-testid="sidebar-backdrop"
          aria-hidden="true"
          className="fixed inset-0 z-30 bg-black/50 md:hidden"
          onClick={onCloseMobile}
        />
      )}
      <div
        data-slot="sidebar-nav"
        data-collapsed={collapsed}
        data-mobile-open={mobileOpen}
        className={cn(
          'fixed inset-y-0 left-0 z-40 flex w-64 -translate-x-full flex-col gap-3 overflow-y-auto border-r border-border bg-card p-2 transition-transform duration-200 ease-out md:static md:z-auto md:h-auto md:w-56 md:translate-x-0',
          mobileOpen && 'translate-x-0',
          collapsed && 'md:w-16',
        )}
      >
        <div className="flex items-center justify-end">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            data-testid="sidebar-collapse-toggle"
            className="hidden md:inline-flex"
            onClick={() => {
              setCollapsed((prev) => !prev)
            }}
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            {collapsed ? <ChevronRight className="size-4" /> : <ChevronLeft className="size-4" />}
          </Button>
        </div>
        {/* Bug fix: TabsList's shared CVA base (`ui/tabs.tsx`) sets
         * `justify-center` for the horizontal tab-bar use case — that must
         * be overridden to `justify-start` here, or a group list shorter
         * than the sidebar's full height gets vertically centered inside
         * this `flex-1` column, leaving a large empty gap above "COMMAND
         * CENTER" instead of the nav starting right under the chevron. */}
        <TabsList className="h-auto w-full flex-1 flex-col items-stretch justify-start gap-3 bg-transparent p-0">
          {groups.map((group) => (
            <div key={group.label} className="flex flex-col gap-1">
              <span
                className={cn(
                  'px-2 text-[11px] font-semibold tracking-wide text-muted-foreground/70 uppercase',
                  collapsed && 'md:sr-only',
                )}
              >
                {group.label}
              </span>
              {group.items.map((item) => (
                <TabsTrigger
                  key={item.value}
                  value={item.value}
                  onClick={onCloseMobile}
                  className={cn(
                    // visual identity: the active item gets a subtle
                    // phosphor gradient tint + a left "on" stripe (mirrors
                    // the reference mockup's `.nav a.on`) — `group/navitem`
                    // lets the leading status dot below react to the SAME
                    // Base UI `data-active` attribute this trigger already
                    // uses for its own styling.
                    'group/navitem min-h-10 w-full justify-start gap-2 rounded-md border-l-2 border-l-transparent px-2 text-sm',
                    'data-active:border-l-[var(--color-good)] data-active:bg-gradient-to-r data-active:from-[var(--color-good)]/10 data-active:to-transparent',
                  )}
                  title={item.label}
                >
                  <span
                    data-slot="nav-item-dot"
                    aria-hidden="true"
                    className="size-1.5 shrink-0 rounded-full bg-muted-foreground/40 group-data-active/navitem:bg-[var(--color-good)] group-data-active/navitem:shadow-[0_0_6px_var(--color-good)]"
                  />
                  <item.Icon className="size-4 shrink-0" />
                  <span className={cn(collapsed && 'lg:sr-only')}>{item.label}</span>
                </TabsTrigger>
              ))}
            </div>
          ))}
        </TabsList>
      </div>
    </>
  )
}
