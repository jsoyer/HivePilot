import { ChevronLeft, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useT } from '@/lib/i18n'
import { useEscapeKey } from '@/lib/use-escape-key'
import { cn } from '@/lib/utils'
import { usePersistedState } from '@/lib/use-persisted-state'
import type { NavItem } from './nav-config'

const COLLAPSED_STORAGE_KEY = 'hivepilot.webui.sidebar-collapsed'

export interface SidebarNavProps {
  primary: NavItem[]
  plusItems: NavItem[]
  plusLabel: string
  plusHint: string
  /** Failed runs + degraded plugins — badge on the Alerts door only. */
  alertsCount?: number
  mobileOpen: boolean
  onCloseMobile: () => void
}

/**
 * Redesign shell sidebar: four doors + Plus tray. Same TabsTrigger
 * primitives as before (one TabsList — Base UI's tab registry is singular).
 * Desktop docks at `md:`; below that this is an off-canvas drawer.
 */
export function SidebarNav({
  primary,
  plusItems,
  plusLabel,
  plusHint,
  alertsCount = 0,
  mobileOpen,
  onCloseMobile,
}: SidebarNavProps) {
  const t = useT()
  const [collapsed, setCollapsed] = usePersistedState(COLLAPSED_STORAGE_KEY, false)

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
        {...(mobileOpen
          ? { role: 'dialog' as const, 'aria-modal': true, 'aria-label': t('common.navigation') }
          : {})}
        className={cn(
          'fixed inset-y-0 left-0 z-40 flex w-64 -translate-x-full flex-col border-r border-border bg-background p-3 md:static md:z-auto md:h-auto md:w-56 md:translate-x-0',
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
            aria-label={collapsed ? t('common.expandSidebar') : t('common.collapseSidebar')}
            title={collapsed ? t('common.expandSidebar') : t('common.collapseSidebar')}
          >
            {collapsed ? <ChevronRight className="size-4" /> : <ChevronLeft className="size-4" />}
          </Button>
        </div>
        <TabsList className="h-auto w-full flex-1 flex-col items-stretch justify-start gap-1 bg-transparent p-0">
          {primary.map((item) => (
            <NavTrigger
              key={item.value}
              item={item}
              collapsed={collapsed}
              onCloseMobile={onCloseMobile}
              badge={item.value === 'alerts' && alertsCount > 0 ? alertsCount : undefined}
            />
          ))}
          <div className="mt-auto flex flex-col gap-1 pt-4" data-testid="sidebar-plus">
            <span
              className={cn(
                'px-3 text-xs font-medium text-muted-foreground',
                collapsed && 'md:sr-only',
              )}
            >
              {plusLabel}
            </span>
            {plusItems.map((item) => (
              <NavTrigger key={item.value} item={item} collapsed={collapsed} onCloseMobile={onCloseMobile} />
            ))}
            <span
              className={cn(
                'px-3 pt-1 text-[11px] text-muted-foreground/80',
                collapsed && 'md:sr-only',
              )}
            >
              {plusHint}
            </span>
          </div>
        </TabsList>
      </div>
    </>
  )
}

function NavTrigger({
  item,
  collapsed,
  onCloseMobile,
  badge,
}: {
  item: NavItem
  collapsed: boolean
  onCloseMobile: () => void
  badge?: number
}) {
  return (
    <TabsTrigger
      value={item.value}
      onClick={onCloseMobile}
      className={cn(
        'min-h-11 w-full justify-start gap-2 rounded-[10px] px-3 text-sm md:min-h-10',
        'data-active:bg-card data-active:text-foreground data-active:shadow-none',
      )}
      title={item.label}
    >
      <item.Icon className="size-4 shrink-0" />
      <span className={cn('truncate', collapsed && 'md:sr-only')}>{item.label}</span>
      {badge != null && (
        <span
          data-testid="alerts-badge"
          className={cn(
            'ml-auto inline-flex min-w-5 items-center justify-center rounded-full bg-muted px-1.5 text-[11px] font-medium text-muted-foreground',
            collapsed && 'md:sr-only',
          )}
        >
          {badge}
        </span>
      )}
    </TabsTrigger>
  )
}
