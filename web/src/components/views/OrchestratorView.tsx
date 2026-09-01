import { Rocket, Workflow } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { type TranslationKey, useT } from '@/lib/i18n'
import {
  type DecomposeResult,
  decomposeFeature,
  type LaunchMissionResult,
  launchMission,
  type MissionStrategyDetail,
  fetchMissionStrategies,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { cn } from '@/lib/utils'

const NO_STRATEGIES: MissionStrategyDetail[] = []

/**
 * Orchestrator decomposition panel (HP-49 / HP-69). A goal in → a MissionPlan
 * preview (tasks) + the five strategy MODE CARDS (mockup): pick HOW the tasks
 * execute and merge, then launch. Each card shows its guarantee label
 * ("+6 min/task, for the night") plus the stages / dispatch / merge policy the
 * preset ties together. Decompose + launch are `run`-gated (the actions hide
 * for a read-only token; the server enforces it regardless).
 */
export function OrchestratorView() {
  const t = useT()
  const { can } = useRole()
  const canRun = can('run')

  const strategiesState = useAsyncData(() => fetchMissionStrategies(), [])
  const strategies =
    strategiesState.status === 'success' ? strategiesState.data.strategies : NO_STRATEGIES
  const defaultStrategy =
    strategiesState.status === 'success' ? strategiesState.data.default : null

  const [goal, setGoal] = useState('')
  const [project, setProject] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [preview, setPreview] = useState<DecomposeResult | null>(null)
  const [launched, setLaunched] = useState<LaunchMissionResult | null>(null)
  const [busy, setBusy] = useState<'decompose' | 'launch' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const effectiveStrategy = useMemo(
    () => selected ?? preview?.plan.strategy ?? defaultStrategy ?? null,
    [selected, preview, defaultStrategy],
  )

  async function onDecompose() {
    const g = goal.trim()
    if (!g || busy) return
    setBusy('decompose')
    setError(null)
    setLaunched(null)
    try {
      const result = await decomposeFeature(g, project.trim() || undefined, selected ?? undefined)
      setPreview(result)
      setSelected(result.plan.strategy)
    } catch (err) {
      setError(err instanceof ApiForbiddenError ? t('orchestrator.forbidden') : describeApiError(err))
    } finally {
      setBusy(null)
    }
  }

  async function onLaunch() {
    const g = goal.trim()
    if (!g || busy || !effectiveStrategy) return
    setBusy('launch')
    setError(null)
    try {
      const result = await launchMission(g, project.trim() || undefined, effectiveStrategy)
      setLaunched(result)
      setPreview({ plan: result.plan, space_id: result.space_id })
    } catch (err) {
      setError(err instanceof ApiForbiddenError ? t('orchestrator.forbidden') : describeApiError(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card data-testid="orchestrator-view">
      <CardHeader>
        <CardTitle>{t('nav.orchestrator')}</CardTitle>
        <CardDescription>{t('orchestrator.description')}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {!canRun && (
          <div className="rounded-lg border border-border bg-muted/40 p-3 text-xs text-muted-foreground">
            {t('orchestrator.readOnly')}
          </div>
        )}

        {/* Goal + project */}
        <div className="flex flex-col gap-2">
          <textarea
            data-testid="orchestrator-goal"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            rows={3}
            disabled={!canRun}
            placeholder={t('orchestrator.goalPlaceholder')}
            className="resize-none rounded-md border border-border bg-background px-2.5 py-1.5 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-60"
          />
          <div className="flex items-center gap-2">
            <input
              data-testid="orchestrator-project"
              value={project}
              onChange={(e) => setProject(e.target.value)}
              disabled={!canRun}
              placeholder={t('orchestrator.projectPlaceholder')}
              className="w-48 rounded-md border border-border bg-background px-2.5 py-1.5 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-60"
            />
            {canRun && (
              <Button
                data-testid="orchestrator-decompose"
                variant="outline"
                size="sm"
                className="gap-1.5"
                disabled={busy !== null || !goal.trim()}
                onClick={() => void onDecompose()}
              >
                <Workflow className="size-4" />
                {busy === 'decompose' ? t('orchestrator.decomposing') : t('orchestrator.decompose')}
              </Button>
            )}
          </div>
        </div>

        {error && (
          <div role="alert" className="text-xs text-destructive">
            {error}
          </div>
        )}

        {/* Strategy mode cards */}
        {strategies.length > 0 && (
          <div>
            <h3 className="mb-2 text-sm font-medium">{t('orchestrator.strategyTitle')}</h3>
            <div
              data-testid="orchestrator-strategies"
              className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3"
            >
              {strategies.map((s) => {
                const active = s.name === effectiveStrategy
                return (
                  <button
                    key={s.name}
                    type="button"
                    data-testid={`orchestrator-strategy-${s.name}`}
                    aria-pressed={active}
                    disabled={!canRun}
                    onClick={() => setSelected(s.name)}
                    className={cn(
                      'flex flex-col gap-1.5 rounded-lg border p-3 text-left transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-60',
                      active
                        ? 'border-primary bg-primary/10'
                        : 'border-border hover:bg-muted/50',
                    )}
                  >
                    <span className="text-sm font-medium">
                      {t(`strategy.${s.name}` as TranslationKey)}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {t(s.guarantee as TranslationKey)}
                    </span>
                    <span className="flex flex-wrap gap-1">
                      <Badge variant="secondary" className="text-[10px]">
                        {s.stages.join(' → ')}
                      </Badge>
                      <Badge variant="outline" className="text-[10px]">
                        {t(`strategy.dispatch.${s.dispatch}` as TranslationKey)}
                      </Badge>
                      <Badge variant="outline" className="text-[10px]">
                        {t(`strategy.merge.${s.merge}` as TranslationKey)}
                      </Badge>
                    </span>
                  </button>
                )
              })}
            </div>
          </div>
        )}

        {/* Plan preview */}
        {preview && (
          <div data-testid="orchestrator-preview" className="rounded-lg border border-border p-3">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-sm font-medium">
                {t('orchestrator.planTitle', { count: preview.plan.tasks.length })}
              </h3>
              {canRun && (
                <Button
                  data-testid="orchestrator-launch"
                  size="sm"
                  className="gap-1.5"
                  disabled={busy !== null || !effectiveStrategy}
                  onClick={() => void onLaunch()}
                >
                  <Rocket className="size-4" />
                  {busy === 'launch' ? t('orchestrator.launching') : t('orchestrator.launch')}
                </Button>
              )}
            </div>
            <ol className="flex flex-col gap-1">
              {preview.plan.tasks.map((task) => (
                <li
                  key={task.id}
                  data-testid={`orchestrator-task-${task.id}`}
                  className="flex items-baseline gap-2 text-sm"
                >
                  <span className="metric-mono text-xs text-muted-foreground">{task.id}</span>
                  <span className="font-medium">{task.title}</span>
                  <Badge variant="outline" className="text-[10px]">
                    {task.role}
                  </Badge>
                  {task.depends_on && task.depends_on.length > 0 && (
                    <span className="metric-mono text-[10px] text-muted-foreground">
                      ← {task.depends_on.join(', ')}
                    </span>
                  )}
                </li>
              ))}
            </ol>
          </div>
        )}

        {/* Launch confirmation */}
        {launched && (
          <div
            data-testid="orchestrator-launched"
            className="rounded-lg border border-primary/30 bg-primary/10 p-3 text-sm"
          >
            {t('orchestrator.launched', {
              count: Object.keys(launched.runs).length,
              mission: launched.mission_id,
            })}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
