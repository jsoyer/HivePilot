import { ServerCog, TriangleAlert } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { MetricReadout } from '@/components/dashboard/MetricReadout'
import { describeApiError } from '@/lib/format-error'
import { formatAge, formatTimestamp } from '@/lib/format-time'
import { useT } from '@/lib/i18n'
import {
  agentLogin,
  connectModel,
  fetchAnalyticsCost,
  fetchOnboardingMachine,
  fetchProviderFallbacks,
  verifyModel,
  type CliSession,
  type LocalBackend,
  type ModelConnectResult,
  type ModelVerifyResult,
  type ProviderFallback,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

const CONNECT_PROVIDERS = [
  'openai',
  'openrouter',
  'anthropic',
  'google',
  'mistral',
  'perplexity',
] as const

/** Fixed 30-day spend window (matches ModelsView); fallbacks use a 24h window
 * since they are a "what's failing right now" signal. */
const SPEND_DAYS = 30
const FALLBACK_HOURS = 24

const NO_FALLBACKS: ProviderFallback[] = []

function formatTokens(n: number): string {
  return n.toLocaleString('en-US')
}

function formatCost(n: number): string {
  return `$${n.toFixed(3)}`
}

interface ProviderRow {
  provider: string
  cost_usd: number
  input_tokens: number
  output_tokens: number
  fallback: ProviderFallback | null
}

/**
 * Providers panel (HP-73) — the visible companion to HP-70's model fallback.
 * Per provider it shows REAL recorded spend + tokens (`/v1/analytics/cost`
 * `by_provider`) and, when present, a fallback badge from
 * `/v1/providers/fallbacks` (how many times it fell over in the last 24h, when
 * last, and why). Nothing here is estimated beyond the existing cost envelope:
 * a provider with no recorded activity simply doesn't appear, and quota
 * %/runway (which needs a real provider API) is deliberately not shown.
 */
export function ProvidersView() {
  const t = useT()
  const { can } = useRole()
  const canAdmin = can('admin')
  const costState = useAsyncData(() => fetchAnalyticsCost(SPEND_DAYS), [])
  const fallbackState = useAsyncData(() => fetchProviderFallbacks(FALLBACK_HOURS), [])
  const [machineTick, setMachineTick] = useState(0)
  const machine = useAsyncData(() => fetchOnboardingMachine(), [machineTick])
  const fallbacks = fallbackState.status === 'success' ? fallbackState.data.providers : NO_FALLBACKS
  const [verifyKey, setVerifyKey] = useState<string | null>(null)
  const [verifyByKey, setVerifyByKey] = useState<Record<string, ModelVerifyResult>>({})

  async function runVerify(key: string, body: { provider?: string; agent_kind?: string; base_url?: string }) {
    setVerifyKey(key)
    try {
      const result = await verifyModel(body)
      setVerifyByKey((prev) => ({ ...prev, [key]: result }))
    } finally {
      setVerifyKey(null)
    }
  }

  function fallbackReason(reason: string | null): string {
    if (reason === 'quota') return t('providers.reason.quota')
    if (reason === 'unavailable') return t('providers.reason.unavailable')
    return reason ?? ''
  }

  return (
    <div className="flex flex-col gap-4">
    <Card data-testid="providers-machine" className="border-dashed">
      <CardHeader>
        <CardTitle>{t('providers.machineTitle')}</CardTitle>
        <CardDescription>{t('providers.machineDescription')}</CardDescription>
      </CardHeader>
      <CardContent>
        <AsyncSection
          state={machine}
          emptyMessage={t('providers.machineEmpty')}
          isEmpty={(data) =>
            data.local.every((b) => !b.reachable && b.models.length === 0) &&
            data.cli.every((s) => s.state !== 'present')
          }
        >
          {(data) => (
            <div className="flex flex-col gap-4">
              <div>
                <h3 className="mb-2 text-sm font-medium">{t('providers.localTitle')}</h3>
                <ul className="flex flex-col gap-2">
                  {data.local.map((b) => (
                    <LocalBackendRow
                      key={b.kind}
                      backend={b}
                      verifying={verifyKey === `local:${b.kind}`}
                      result={verifyByKey[`local:${b.kind}`]}
                      onVerify={() =>
                        void runVerify(`local:${b.kind}`, {
                          provider: b.kind,
                          base_url: b.base_url,
                        })
                      }
                    />
                  ))}
                </ul>
              </div>
              <div>
                <h3 className="mb-2 text-sm font-medium">{t('providers.cliTitle')}</h3>
                <ul className="flex flex-col gap-2">
                  {data.cli.map((s) => (
                    <CliSessionRow
                      key={s.kind}
                      session={s}
                      verifying={verifyKey === `cli:${s.kind}`}
                      result={verifyByKey[`cli:${s.kind}`]}
                      onVerify={() => void runVerify(`cli:${s.kind}`, { agent_kind: s.kind })}
                      canAdmin={canAdmin}
                      onChanged={() => setMachineTick((n) => n + 1)}
                    />
                  ))}
                </ul>
              </div>
            </div>
          )}
        </AsyncSection>
      </CardContent>
    </Card>
    <ConnectModelCard canAdmin={canAdmin} />
    <Card data-testid="providers-view">
      <CardHeader>
        <CardTitle>{t('nav.providers')}</CardTitle>
        <CardDescription>{t('providers.description')}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <AsyncSection
          state={costState}
          emptyMessage={t('providers.noData')}
          isEmpty={(data) => data.by_provider.length === 0 && fallbacks.length === 0}
        >
          {(cost) => {
            const byName = new Map<string, ProviderRow>()
            for (const p of cost.by_provider) {
              byName.set(p.provider, {
                provider: p.provider,
                cost_usd: p.cost_usd,
                input_tokens: p.input_tokens,
                output_tokens: p.output_tokens,
                fallback: null,
              })
            }
            // A provider that fell back but recorded no spend in-window still
            // matters — surface it as a fallback-only row rather than hiding it.
            for (const fb of fallbacks) {
              const row = byName.get(fb.provider)
              if (row) row.fallback = fb
              else
                byName.set(fb.provider, {
                  provider: fb.provider,
                  cost_usd: 0,
                  input_tokens: 0,
                  output_tokens: 0,
                  fallback: fb,
                })
            }
            const rows = [...byName.values()].sort((a, b) => b.cost_usd - a.cost_usd)
            const fallbackCount = fallbacks.reduce((sum, f) => sum + f.count, 0)

            return (
              <>
                <div className="grid gap-3 sm:grid-cols-3">
                  <MetricReadout
                    label={t('providers.overallSpend')}
                    value={formatCost(cost.overall.cost_usd)}
                  />
                  <MetricReadout
                    label={t('providers.providerCount')}
                    value={String(cost.by_provider.length)}
                  />
                  <MetricReadout
                    label={t('providers.fallbackCount', { hours: FALLBACK_HOURS })}
                    value={String(fallbackCount)}
                  />
                </div>

                <Table data-testid="providers-table">
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t('providers.provider')}</TableHead>
                      <TableHead className="text-right">{t('providers.cost')}</TableHead>
                      <TableHead className="text-right">{t('providers.tokens')}</TableHead>
                      <TableHead>{t('providers.status')}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {rows.map((row) => (
                      <TableRow key={row.provider} data-testid={`providers-row-${row.provider}`}>
                        <TableCell className="font-medium">
                          <span className="flex items-center gap-1.5">
                            <ServerCog className="size-4 text-muted-foreground" />
                            {row.provider}
                          </span>
                        </TableCell>
                        <TableCell className="metric-mono text-right">
                          {formatCost(row.cost_usd)}
                        </TableCell>
                        <TableCell className="metric-mono text-right text-muted-foreground">
                          {formatTokens(row.input_tokens + row.output_tokens)}
                        </TableCell>
                        <TableCell>
                          {row.fallback ? (
                            <Badge
                              variant="outline"
                              data-testid={`providers-fallback-${row.provider}`}
                              className="gap-1 border-[var(--color-warn)]/40 text-[var(--color-warn)]"
                              title={
                                row.fallback.last_at
                                  ? formatTimestamp(row.fallback.last_at)
                                  : undefined
                              }
                            >
                              <TriangleAlert className="size-3" />
                              {t('providers.fallbackBadge', {
                                count: row.fallback.count,
                                reason: fallbackReason(row.fallback.last_reason),
                              })}
                              {row.fallback.last_at
                                ? ` · ${t('home.ageAgo', { age: formatAge(row.fallback.last_at) })}`
                                : ''}
                            </Badge>
                          ) : (
                            <span className="text-xs text-muted-foreground">
                              {t('providers.healthy')}
                            </span>
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>

                <p className="text-xs text-muted-foreground">{t('providers.note')}</p>
              </>
            )
          }}
        </AsyncSection>
      </CardContent>
    </Card>
    </div>
  )
}

function ConnectModelCard({ canAdmin }: { canAdmin: boolean }) {
  const t = useT()
  const [provider, setProvider] = useState<(typeof CONNECT_PROVIDERS)[number]>('openai')
  const [apiKey, setApiKey] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState<ModelConnectResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (!apiKey.trim()) {
      setError(t('providers.connectNeedKey'))
      return
    }
    setSaving(true)
    setError(null)
    setResult(null)
    try {
      const next = await connectModel({
        provider,
        api_key: apiKey,
        base_url: baseUrl.trim() || undefined,
      })
      setResult(next)
      if (next.ok) setApiKey('')
    } catch (err) {
      setError(describeApiError(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card data-testid="providers-connect">
      <CardHeader>
        <CardTitle>{t('providers.connectTitle')}</CardTitle>
        <CardDescription>{t('providers.connectDescription')}</CardDescription>
      </CardHeader>
      <CardContent>
        {canAdmin ? (
          <form className="flex flex-col gap-3" onSubmit={(e) => void onSubmit(e)}>
            <label className="flex flex-col gap-1 text-sm">
              <span>{t('providers.connectProvider')}</span>
              <select
                data-testid="providers-connect-provider"
                className="h-8 rounded-lg border border-input bg-transparent px-2.5 text-sm"
                value={provider}
                onChange={(e) => setProvider(e.target.value as (typeof CONNECT_PROVIDERS)[number])}
              >
                {CONNECT_PROVIDERS.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span>{t('providers.connectKey')}</span>
              <Input
                data-testid="providers-connect-key"
                type="password"
                autoComplete="off"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span>{t('providers.connectBaseUrl')}</span>
              <Input
                data-testid="providers-connect-base-url"
                type="url"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
              />
            </label>
            <div>
              <Button type="submit" size="sm" disabled={saving} data-testid="providers-connect-submit">
                {saving ? t('providers.connectSaving') : t('providers.connectSave')}
              </Button>
            </div>
            {result && (
              <p
                data-testid="providers-connect-result"
                className={result.ok ? 'text-sm' : 'text-sm text-destructive'}
              >
                {result.ok && result.env_key
                  ? t('providers.connectSaved', { envKey: result.env_key })
                  : (result.error ?? result.detail)}
                {result.ok && result.detail ? ` · ${result.detail}` : ''}
              </p>
            )}
            {error && (
              <p data-testid="providers-connect-error" className="text-sm text-destructive">
                {error}
              </p>
            )}
          </form>
        ) : (
          <p className="text-sm text-muted-foreground">{t('providers.connectNeedAdmin')}</p>
        )}
      </CardContent>
    </Card>
  )
}

function LocalBackendRow({
  backend,
  verifying,
  result,
  onVerify,
}: {
  backend: LocalBackend
  verifying: boolean
  result?: ModelVerifyResult
  onVerify: () => void
}) {
  const t = useT()
  return (
    <li
      data-testid={`local-backend-${backend.kind}`}
      className="flex flex-wrap items-center justify-between gap-2 text-sm"
    >
      <div>
        <span className="font-medium">{backend.kind}</span>{' '}
        <span className="text-xs text-muted-foreground">{backend.base_url}</span>
        <div className="text-xs text-muted-foreground">
          {backend.reachable
            ? `${t('providers.reachable')}${backend.models.length ? ` · ${backend.models.join(', ')}` : ''}`
            : t('providers.unreachable')}
        </div>
        {result && (
          <div className="text-xs" data-testid={`local-verify-${backend.kind}`}>
            {result.ok ? result.detail : result.error ?? result.detail}
          </div>
        )}
      </div>
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={verifying}
        onClick={onVerify}
      >
        {verifying ? t('providers.verifying') : t('providers.verify')}
      </Button>
    </li>
  )
}

type LoginState =
  | { kind: 'idle' }
  | { kind: 'working' }
  | { kind: 'url'; url: string | null; log: string }
  | { kind: 'error'; message: string }

function CliSessionRow({
  session,
  verifying,
  result,
  onVerify,
  canAdmin,
  onChanged,
}: {
  session: CliSession
  verifying: boolean
  result?: ModelVerifyResult
  onVerify: () => void
  canAdmin: boolean
  onChanged: () => void
}) {
  const t = useT()
  const [login, setLogin] = useState<LoginState>({ kind: 'idle' })
  const showLogin = canAdmin && session.login_available && session.state !== 'present'

  const runLogin = async () => {
    setLogin({ kind: 'working' })
    try {
      const next = await agentLogin(session.kind)
      setLogin({ kind: 'url', url: next.url, log: next.log })
      onChanged()
    } catch (err) {
      setLogin({ kind: 'error', message: describeApiError(err) })
    }
  }

  return (
    <li
      data-testid={`cli-session-${session.kind}`}
      className="flex flex-wrap items-center justify-between gap-2 text-sm"
    >
      <div>
        <span className="font-medium">{session.kind}</span>{' '}
        <span className="text-xs text-muted-foreground">
          {session.state === 'present'
            ? t('providers.sessionPresent')
            : t('providers.sessionAbsent')}
        </span>
        {result && (
          <div className="text-xs" data-testid={`cli-verify-${session.kind}`}>
            {result.ok ? result.detail : result.error ?? result.detail}
          </div>
        )}
        {login.kind === 'working' && (
          <div className="text-xs text-muted-foreground">{t('providers.loginWorking')}</div>
        )}
        {login.kind === 'url' && (
          <div className="text-xs" data-testid={`cli-login-url-${session.kind}`}>
            {login.url ? (
              <a href={login.url} target="_blank" rel="noreferrer" className="underline break-all">
                {login.url}
              </a>
            ) : (
              t('providers.loginNoUrl', { log: login.log })
            )}
          </div>
        )}
        {login.kind === 'error' && (
          <div className="text-xs text-destructive">{login.message}</div>
        )}
      </div>
      <span className="inline-flex flex-wrap gap-1">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={verifying}
          onClick={onVerify}
        >
          {verifying ? t('providers.verifying') : t('providers.verify')}
        </Button>
        {showLogin && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={login.kind === 'working'}
            data-testid={`cli-login-${session.kind}`}
            onClick={() => void runLogin()}
          >
            {t('providers.login')}
          </Button>
        )}
      </span>
    </li>
  )
}
