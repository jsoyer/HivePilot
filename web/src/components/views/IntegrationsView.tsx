import { useState } from 'react'
import { Cable } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import {
  fetchManagedCatalogs,
  fetchMcpServers,
  fetchPluginPacks,
  fetchTypedTools,
  importOpenApi,
  installPluginPack,
  syncComposio,
  syncMcpServer,
  syncPipedream,
  type McpServer,
  type PluginPackPreview,
  type TypedTool,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData, type AsyncState } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

/**
 * HP-60 — one Pollen page for tool sources. MCP command center stays on
 * its own tab (HP-76). Composio / Pipedream are honest coming-soon cards
 * until HP-59.
 */
export function IntegrationsView() {
  const t = useT()
  const { can } = useRole()
  const canAdmin = can('admin')
  const [tick, setTick] = useState(0)
  const toolsState = useAsyncData(() => fetchTypedTools(), [tick])
  const serversState = useAsyncData(() => fetchMcpServers(), [tick])
  const packsState = useAsyncData(() => fetchPluginPacks(), [tick])
  const managedState = useAsyncData(() => fetchManagedCatalogs(), [tick])

  return (
    <div className="flex flex-col gap-4" data-testid="integrations-view">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Cable className="size-5" />
            {t('nav.integrations')}
          </CardTitle>
          <CardDescription>{t('integrations.description')}</CardDescription>
        </CardHeader>
        {!canAdmin ? (
          <CardContent>
            <p className="text-muted-foreground text-sm">{t('integrations.readOnly')}</p>
          </CardContent>
        ) : null}
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <OpenApiCard canAdmin={canAdmin} onImported={() => setTick((n) => n + 1)} />
        <McpSyncCard
          canAdmin={canAdmin}
          state={serversState}
          onSynced={() => setTick((n) => n + 1)}
        />
        <ManagedCatalogCard
          testId="integrations-composio"
          title={t('integrations.composioTitle')}
          hint={t('integrations.composioHint')}
          needKey={t('integrations.composioNeedKey')}
          filterPlaceholder={t('integrations.composioToolkit')}
          filterTestId="composio-toolkit"
          syncTestId="composio-sync"
          configured={managedState.status === 'success' && managedState.data.composio.configured}
          canAdmin={canAdmin}
          onSync={(toolkit) => syncComposio(toolkit)}
          onSynced={() => setTick((n) => n + 1)}
        />
        <ManagedCatalogCard
          testId="integrations-pipedream"
          title={t('integrations.pipedreamTitle')}
          hint={t('integrations.pipedreamHint')}
          needKey={t('integrations.pipedreamNeedKey')}
          filterPlaceholder={t('integrations.pipedreamApp')}
          filterTestId="pipedream-app"
          syncTestId="pipedream-sync"
          configured={managedState.status === 'success' && managedState.data.pipedream.configured}
          canAdmin={canAdmin}
          onSync={(app) => syncPipedream(app)}
          onSynced={() => setTick((n) => n + 1)}
        />
      </div>

      <PacksCard canAdmin={canAdmin} state={packsState} onInstalled={() => setTick((n) => n + 1)} />

      <Card>
        <CardHeader>
          <CardTitle>{t('integrations.toolsTitle')}</CardTitle>
          <CardDescription>{t('integrations.toolsHint')}</CardDescription>
        </CardHeader>
        <CardContent>
          <AsyncSection
            state={toolsState}
            isEmpty={(data) => data.tools.length === 0}
            emptyMessage={t('integrations.noTools')}
          >
            {(data) => (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('integrations.toolName')}</TableHead>
                    <TableHead>{t('integrations.toolSource')}</TableHead>
                    <TableHead>{t('integrations.toolLocal')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.tools.map((tool: TypedTool) => (
                    <TableRow key={tool.qualified_name} data-testid={`typed-tool-${tool.qualified_name}`}>
                      <TableCell className="font-mono text-xs">{tool.qualified_name}</TableCell>
                      <TableCell>
                        {tool.source_kind}/{tool.source_id}
                      </TableCell>
                      <TableCell>{tool.local_name}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </AsyncSection>
        </CardContent>
      </Card>
    </div>
  )
}

function ManagedCatalogCard({
  testId,
  title,
  hint,
  needKey,
  filterPlaceholder,
  filterTestId,
  syncTestId,
  configured,
  canAdmin,
  onSync,
  onSynced,
}: {
  testId: string
  title: string
  hint: string
  needKey: string
  filterPlaceholder: string
  filterTestId: string
  syncTestId: string
  configured: boolean
  canAdmin: boolean
  onSync: (filter: string) => Promise<unknown>
  onSynced: () => void
}) {
  const t = useT()
  const [filter, setFilter] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function handleSync() {
    setError(null)
    setBusy(true)
    try {
      await onSync(filter.trim())
      setFilter('')
      onSynced()
    } catch (err) {
      setError(err instanceof ApiForbiddenError ? t('integrations.forbidden') : describeApiError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{configured ? hint : needKey}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <input
          className="border-input bg-background rounded-md border px-3 py-2 text-sm"
          data-testid={filterTestId}
          disabled={!canAdmin || !configured || busy}
          placeholder={filterPlaceholder}
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
        <Button
          data-testid={syncTestId}
          disabled={!canAdmin || !configured || busy || !filter.trim()}
          onClick={() => void handleSync()}
        >
          {busy ? t('integrations.syncing') : t('integrations.sync')}
        </Button>
        {error ? <p className="text-destructive text-sm">{error}</p> : null}
      </CardContent>
    </Card>
  )
}

function OpenApiCard({
  canAdmin,
  onImported,
}: {
  canAdmin: boolean
  onImported: () => void
}) {
  const t = useT()
  const [text, setText] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function handleImport() {
    setError(null)
    setBusy(true)
    try {
      await importOpenApi({ text, name: name || undefined })
      setText('')
      setName('')
      onImported()
    } catch (err) {
      setError(err instanceof ApiForbiddenError ? t('integrations.forbidden') : describeApiError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card data-testid="integrations-openapi">
      <CardHeader>
        <CardTitle>{t('integrations.openapiTitle')}</CardTitle>
        <CardDescription>{t('integrations.openapiHint')}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <input
          className="border-input bg-background rounded-md border px-3 py-2 text-sm"
          data-testid="openapi-name"
          disabled={!canAdmin || busy}
          placeholder={t('integrations.openapiName')}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <textarea
          className="border-input bg-background min-h-32 rounded-md border px-3 py-2 font-mono text-xs"
          data-testid="openapi-text"
          disabled={!canAdmin || busy}
          placeholder={t('integrations.openapiPlaceholder')}
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
        <Button
          data-testid="openapi-import"
          disabled={!canAdmin || busy || !text.trim()}
          onClick={() => void handleImport()}
        >
          {busy ? t('integrations.importing') : t('integrations.import')}
        </Button>
        {error ? <p className="text-destructive text-sm">{error}</p> : null}
      </CardContent>
    </Card>
  )
}

function McpSyncCard({
  canAdmin,
  state,
  onSynced,
}: {
  canAdmin: boolean
  state: AsyncState<{ servers: McpServer[]; cost_note: string }>
  onSynced: () => void
}) {
  const t = useT()
  const [busyId, setBusyId] = useState<number | null>(null)
  const [syncError, setSyncError] = useState<string | null>(null)

  async function handleSync(id: number) {
    setSyncError(null)
    setBusyId(id)
    try {
      await syncMcpServer(id)
      onSynced()
    } catch (err) {
      setSyncError(err instanceof ApiForbiddenError ? t('integrations.forbidden') : describeApiError(err))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <Card data-testid="integrations-mcp">
      <CardHeader>
        <CardTitle>{t('integrations.mcpTitle')}</CardTitle>
        <CardDescription>{t('integrations.mcpHint')}</CardDescription>
      </CardHeader>
      <CardContent>
        <AsyncSection
          state={state}
          isEmpty={(data) => data.servers.filter((row) => row.transport === 'http').length === 0}
          emptyMessage={t('integrations.noHttpMcp')}
        >
          {(data) => (
            <ul className="flex flex-col gap-2">
              {data.servers
                .filter((row) => row.transport === 'http')
                .map((server) => (
                  <li key={server.id} className="flex items-center justify-between gap-2">
                    <span className="font-mono text-sm">{server.name}</span>
                    <Button
                      size="sm"
                      data-testid={`mcp-sync-${server.id}`}
                      disabled={!canAdmin || busyId === server.id}
                      onClick={() => void handleSync(server.id)}
                    >
                      {busyId === server.id ? t('integrations.syncing') : t('integrations.sync')}
                    </Button>
                  </li>
                ))}
            </ul>
          )}
        </AsyncSection>
        {syncError ? <p className="text-destructive mt-2 text-sm">{syncError}</p> : null}
      </CardContent>
    </Card>
  )
}

function PacksCard({
  canAdmin,
  state,
  onInstalled,
}: {
  canAdmin: boolean
  state: AsyncState<{ packs: PluginPackPreview[] }>
  onInstalled: () => void
}) {
  const t = useT()
  const [busy, setBusy] = useState<string | null>(null)
  const [packError, setPackError] = useState<string | null>(null)

  async function handleInstall(name: string) {
    setPackError(null)
    setBusy(name)
    try {
      await installPluginPack(name)
      onInstalled()
    } catch (err) {
      setPackError(err instanceof ApiForbiddenError ? t('integrations.forbidden') : describeApiError(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card data-testid="integrations-packs">
      <CardHeader>
        <CardTitle>{t('integrations.packsTitle')}</CardTitle>
        <CardDescription>{t('integrations.packsHint')}</CardDescription>
      </CardHeader>
      <CardContent>
        <AsyncSection
          state={state}
          isEmpty={(data) => data.packs.length === 0}
          emptyMessage={t('integrations.noPacks')}
        >
          {(data) => (
            <ul className="flex flex-col gap-2">
              {data.packs.map((preview) => (
                <li key={preview.pack.name} className="flex items-center justify-between gap-2">
                  <div>
                    <div className="font-medium">{preview.pack.name}</div>
                    <div className="text-muted-foreground text-xs">{preview.pack.description}</div>
                  </div>
                  <Button
                    size="sm"
                    data-testid={`pack-install-${preview.pack.name}`}
                    disabled={!canAdmin || busy === preview.pack.name}
                    onClick={() => void handleInstall(preview.pack.name)}
                  >
                    {busy === preview.pack.name ? t('integrations.installing') : t('integrations.installPack')}
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </AsyncSection>
        {packError ? <p className="text-destructive mt-2 text-sm">{packError}</p> : null}
      </CardContent>
    </Card>
  )
}
