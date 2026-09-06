import { SectionHeader } from '@/components/dashboard/SectionHeader'
import { useT } from '@/lib/i18n'
import { RoutinesCard } from './RoutinesCard'
import { SchedulesCard } from './SchedulesCard'

/**
 * HP-57: one Operate surface for YAML schedules, role routines, and the
 * autopilot drain they share. Lives on AutopilotView — the widget that was
 * already on screen as SchedulesCard.
 */
export function AutomationsWidget() {
  const t = useT()
  return (
    <section className="flex flex-col gap-6" data-testid="automations-widget">
      <div className="flex flex-col gap-1">
        <SectionHeader index="04" title={t('automations.title')} />
        <p className="text-sm text-muted-foreground">{t('automations.subtitle')}</p>
      </div>
      <RoutinesCard />
      <SchedulesCard />
    </section>
  )
}
