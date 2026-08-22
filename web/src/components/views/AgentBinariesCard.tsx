import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { describeApiError } from '@/lib/format-error'
import { EM_DASH } from '@/lib/format-time'
import { useT } from '@/lib/i18n'
import {
  agentAction,
  fetchAgentsAdmin,
  type AgentActionResult,
  type AgentAdminEntry,
} from '@/lib/pollen-api'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

/**
 * The agent CLI binaries on the box: installed version, whether the SERVICE
 * can see them, and — for kinds whose registry entry carries a vetted
 * command — an install/update action.
 *
 * Two decisions are the card; the rest is a table:
 *
 * **The confirm step IS the consent.** `agent_install.py` refuses to run
 * without an interactive TTY "yes"; this surface replaces that guarantee, so
 * the action is two clicks — the second explicitly worded — and the request
 * carries `{"consent": true}` as the button's signature on the decision. One
 * accidental click never runs a vendor's install pipeline.
 *
 * **`on_service_path` is the service's own view, and the one that decides.**
 * A binary installed by a login shell can be invisible to the units — grok's
 * installer landed in `$HOME/.grok/bin`, off the systemd PATH, and the runner
 * silently never registered. An installed-but-off-path row shows a warning
 * badge instead of pretending.
 *
 * No button is ever rendered for a capability the registry does not declare:
 * a docs-only kind gets its link, a kind with no verified updater gets no
 * update button — never a greyed-out one that lies.
 */

type RowState =
  | { kind: 'idle' }
  | { kind: 'confirming'; action: 'install' | 'update' }
  | { kind: 'working'; action: 'install' | 'update' }
  | { kind: 'done'; result: AgentActionResult }
  | { kind: 'error'; message: string }

function AgentRow({
  agent,
  canAdmin,
  onChanged,
}: {
  agent: AgentAdminEntry
  canAdmin: boolean
  onChanged: () => void
}) {
  const t = useT()
  const [state, setState] = useState<RowState>({ kind: 'idle' })

  const run = async (action: 'install' | 'update') => {
    setState({ kind: 'working', action })
    try {
      const result = await agentAction(agent.kind, action)
      setState({ kind: 'done', result })
      onChanged()
    } catch (err) {
      setState({ kind: 'error', message: describeApiError(err) })
    }
  }

  const actionButton = (action: 'install' | 'update', label: string, confirmLabel: string) => {
    if (state.kind === 'confirming' && state.action === action) {
      return (
        <span className="inline-flex gap-1">
          <Button size="sm" variant="destructive" onClick={() => void run(action)}>
            {confirmLabel}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setState({ kind: 'idle' })}>
            {t('agents.binaries.cancel')}
          </Button>
        </span>
      )
    }
    return (
      <Button
        size="sm"
        variant="outline"
        disabled={!canAdmin || state.kind === 'working'}
        title={canAdmin ? undefined : t('agents.binaries.adminOnly')}
        onClick={() => setState({ kind: 'confirming', action })}
      >
        {label}
      </Button>
    )
  }

  return (
    <TableRow>
      <TableCell>
        <div className="font-medium">{agent.name}</div>
        <div className="text-xs text-muted-foreground">{agent.vendor}</div>
      </TableCell>
      <TableCell className="font-mono text-sm">{agent.installed_version ?? EM_DASH}</TableCell>
      <TableCell>
        {agent.on_service_path ? (
          <Badge variant="outline">{t('agents.binaries.onPath')}</Badge>
        ) : (
          <Badge variant="destructive" title={t('agents.binaries.offPathTitle')}>
            {t('agents.binaries.offPath')}
          </Badge>
        )}
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap items-center gap-1">
          {agent.installable ? (
            actionButton('install', t('agents.binaries.install'), t('agents.binaries.confirmInstall'))
          ) : (
            <a
              className="text-xs text-muted-foreground underline"
              href={agent.docs_url}
              target="_blank"
              rel="noreferrer"
            >
              {t('agents.binaries.docsOnly')}
            </a>
          )}
          {agent.updatable &&
            actionButton('update', t('agents.binaries.update'), t('agents.binaries.confirmUpdate'))}
        </div>
        {state.kind === 'working' && (
          <div className="text-xs text-muted-foreground">{t('agents.binaries.working')}</div>
        )}
        {state.kind === 'done' && (
          <div className="text-xs" data-testid={`result-${agent.kind}`}>
            {state.result.ok
              ? `${state.result.version_before ?? EM_DASH} → ${state.result.version_after ?? EM_DASH}`
              : t('agents.binaries.failed')}
            {!state.result.on_service_path && (
              <span className="text-destructive"> {t('agents.binaries.offPathAfter')}</span>
            )}
          </div>
        )}
        {state.kind === 'error' && <div className="text-xs text-destructive">{state.message}</div>}
      </TableCell>
    </TableRow>
  )
}

export function AgentBinariesCard({ canAdmin }: { canAdmin: boolean }) {
  const t = useT()
  const [refreshKey, setRefreshKey] = useState(0)
  const agents = useAsyncData(() => fetchAgentsAdmin(), [refreshKey])

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('agents.binaries.title')}</CardTitle>
        <CardDescription>{t('agents.binaries.description')}</CardDescription>
      </CardHeader>
      <CardContent>
        <AsyncSection state={agents} isEmpty={(data) => data.agents.length === 0}>
          {(data) => (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t('agents.binaries.colAgent')}</TableHead>
                  <TableHead>{t('agents.binaries.colVersion')}</TableHead>
                  <TableHead>{t('agents.binaries.colPath')}</TableHead>
                  <TableHead>{t('agents.binaries.colActions')}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.agents.map((agent) => (
                  <AgentRow
                    key={agent.kind}
                    agent={agent}
                    canAdmin={canAdmin}
                    onChanged={() => setRefreshKey((k) => k + 1)}
                  />
                ))}
              </TableBody>
            </Table>
          )}
        </AsyncSection>
      </CardContent>
    </Card>
  )
}
