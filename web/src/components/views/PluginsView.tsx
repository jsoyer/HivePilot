import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Switch } from '@/components/ui/switch'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import {
  fetchPluginCatalog,
  installPlugin,
  togglePlugin,
  type PluginCatalogEntry,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

/**
 * One card per curated plugin: what it does, whether it is on, and what it
 * needs.
 *
 * Built on `/plugins/catalog`, not `/plugins/health`. Health reports what
 * LOADED, so a page built on it could only show the handful already
 * installed — and the interesting set is the ~23 that are written and NOT
 * installed, which is how they sat inert here unnoticed.
 *
 * **The switch never installs a prerequisite.** HivePilot fetches the plugin
 * FILE (a closed, curated registry — no arbitrary URL) and stops there. A
 * `pip install` triggered from a web switch runs arbitrary package code as
 * the service user, and a heavy one has wedged this project's production host
 * before. The prerequisite is shown for a human to run, and the card says so
 * outright — leaving it implicit is how a plugin ends up enabled, on disk,
 * and doing nothing.
 */

type PendingState = { kind: 'idle' } | { kind: 'working' } | { kind: 'error'; message: string }

/** What flipping this switch would actually do. Three different actions wear
 * one control, so the card has to know which one applies. */
type ActionKind = 'install' | 'enable' | 'disable'

function actionFor(plugin: PluginCatalogEntry): ActionKind {
  if (!plugin.installed) return 'install'
  if (!plugin.enabled) return 'enable'
  return 'disable'
}

interface PluginCardProps {
  plugin: PluginCatalogEntry
  canAdmin: boolean
  onChanged: () => void
}

function PluginCard({ plugin, canAdmin, onChanged }: PluginCardProps) {
  const t = useT()
  const [pending, setPending] = useState<PendingState>({ kind: 'idle' })
  const [restartRequired, setRestartRequired] = useState(false)
  const [showPrereq, setShowPrereq] = useState(false)

  const action = actionFor(plugin)
  const on = plugin.installed && plugin.enabled

  async function handleToggle() {
    setPending({ kind: 'working' })
    try {
      if (action === 'install') {
        await installPlugin(plugin.name)
        // Installing enables in the same step, so the prerequisite is the ONLY
        // thing left between "on" and "actually working". Open it rather than
        // making the operator go looking for it.
        setShowPrereq(true)
      } else {
        await togglePlugin(plugin.name)
      }
      setRestartRequired(true)
      setPending({ kind: 'idle' })
      onChanged()
    } catch (err) {
      setPending({
        kind: 'error',
        message: err instanceof ApiForbiddenError ? t('plugins.forbidden') : describeApiError(err),
      })
    }
  }

  return (
    <Card data-testid={`plugin-card-${plugin.name}`} className="flex flex-col">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 flex-col gap-1">
            <CardTitle className="truncate">{plugin.name}</CardTitle>
            <div className="flex flex-wrap items-center gap-1.5">
              {!plugin.installed && <Badge variant="outline">{t('plugins.notInstalled')}</Badge>}
              {restartRequired && (
                <Badge variant="outline" title={t('plugins.restartTitle')}>
                  {t('plugins.restartRequired')}
                </Badge>
              )}
            </div>
          </div>
          <Switch
            checked={on}
            disabled={!canAdmin || pending.kind === 'working'}
            onCheckedChange={() => void handleToggle()}
            aria-label={`${t(`plugins.action.${action}`)} ${plugin.name}`}
            title={canAdmin ? t('plugins.restartTitle') : t('plugins.adminOnly')}
          />
        </div>
        {/* A switch with no description is a switch nobody dares flip. */}
        <CardDescription>{plugin.description}</CardDescription>
      </CardHeader>

      <CardContent className="mt-auto flex flex-col gap-2">
        <Button
          size="sm"
          variant="ghost"
          className="w-fit px-0"
          onClick={() => setShowPrereq((v) => !v)}
          aria-expanded={showPrereq}
        >
          {showPrereq ? t('plugins.hidePrereq') : t('plugins.showPrereq')}
        </Button>

        {showPrereq && (
          <div className="flex flex-col gap-1 rounded-lg border border-border bg-muted/30 p-3">
            <span className="text-xs font-semibold uppercase text-muted-foreground">
              {t('plugins.prereqLabel')} · {plugin.prereq_kind}
            </span>
            <span className="text-sm text-muted-foreground">{plugin.prereq_detail}</span>
            <span className="text-sm text-muted-foreground">{t('plugins.prereqNotAutomatic')}</span>
            <code className="mt-1 rounded bg-background px-2 py-1 text-xs">{plugin.env_flag}</code>
          </div>
        )}

        {pending.kind === 'error' && (
          <div role="alert" className="text-sm text-destructive">
            {pending.message}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export function PluginsView() {
  const t = useT()
  const { can } = useRole()
  const [reloadKey, setReloadKey] = useState(0)
  const catalog = useAsyncData(fetchPluginCatalog, [reloadKey])
  const canAdmin = can('admin')

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('plugins.title')}</CardTitle>
        <CardDescription>
          {t('plugins.description')}
          {!canAdmin && ` ${t('plugins.adminOnly')}`}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <AsyncSection
          state={catalog}
          isEmpty={(data) => data.plugins.length === 0}
          emptyMessage={t('plugins.empty')}
        >
          {(data) => (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {data.plugins.map((plugin) => (
                <PluginCard
                  key={plugin.name}
                  plugin={plugin}
                  canAdmin={canAdmin}
                  onChanged={() => setReloadKey((k) => k + 1)}
                />
              ))}
            </div>
          )}
        </AsyncSection>
      </CardContent>
    </Card>
  )
}
