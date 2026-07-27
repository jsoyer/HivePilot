import { ChevronLeft, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useT } from '@/lib/i18n'
import { useEscapeKey } from '@/lib/use-escape-key'
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
 * Left sidebar navigation (Pollen dashboard upgrade, P0b) —
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
  const t = useT()
  const [collapsed, setCollapsed] = usePersistedState(COLLAPSED_STORAGE_KEY, false)

  // Mobile audit: this was the one dismissible overlay in Pollen that Escape
  // did NOT close — it could only be dismissed by tapping the backdrop or a
  // nav item, both of which are undiscoverable and neither of which a
  // keyboard user can reach while the drawer covers the page. Shares the
  // exact hook `ui/drawer.tsx` uses rather than hand-rolling a second copy.
  // Guarded on `mobileOpen` because, unlike `Drawer`, this component stays
  // mounted when closed (it is translated off-canvas, not unmounted), so an
  // unguarded listener would swallow Escape from the ⌘K palette.
  useEscapeKey(mobileOpen, onCloseMobile)

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
        // Only a dialog while it behaves like one (an off-canvas overlay on
        // top of the page). Once it docks statically at `md:` it is just a
        // landmark, and announcing a permanently-open modal dialog there
        // would be a lie — hence the runtime `mobileOpen` check rather than
        // static attributes.
        {...(mobileOpen
          ? { role: 'dialog' as const, 'aria-modal': true, 'aria-label': t('common.navigation') }
          : {})}
        className={cn(
          'fixed inset-y-0 left-0 z-40 flex w-64 -translate-x-full flex-col gap-3 overflow-y-auto border-r border-border bg-card p-2 md:static md:z-auto md:h-auto md:w-56 md:translate-x-0',
          // Respect prefers-reduced-motion: the slide-in is decorative, so
          // it is opt-in via `motion-safe:` rather than always-on.
          'motion-safe:transition-transform motion-safe:duration-200 motion-safe:ease-out',
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
                    // Mobile audit: 40px rows in the drawer sit just under the
                    // comfortable touch target. 44px below `md:`, unchanged (40px)
                    // once the sidebar docks and is driven by a mouse.
                    'group/navitem min-h-11 w-full justify-start gap-2 rounded-md border-l-2 border-l-transparent px-2 text-sm md:min-h-10',
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
