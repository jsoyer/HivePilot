import { useEffect, useState } from 'react'
import { Plug, TriangleAlert } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import {
  addMcpFromCatalog,
  deleteMcpServer,
  fetchMcpCatalog,
  fetchMcpServers,
  importMcpConfig,
  probeMcpServer,
  type McpCatalogEntry,
  type McpServer,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

const POLL_MS = 30_000

function statusVariant(status: string | null | undefined): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (status === 'ok') return 'default'
  if (status === 'missing' || status === 'error') return 'destructive'
  if (status === 'remote') return 'secondary'
  return 'outline'
}

/**
 * MCP command center (HP-76) — servers + catalog on one page, paste-anything
 * import, on-read probe refresh (60s TTL server-side) + 30s client poll.
 * Cost per server is honestly null: HP-73 meters LLM providers, not MCP tools.
 */
export function McpView() {
  const t = useT()
  const { can } = useRole()
  const canAdmin = can('admin')
  const [paste, setPaste] = useState('')
  const [importError, setImportError] = useState<string | null>(null)
  const [importing, setImporting] = useState(false)
  const [stripped, setStripped] = useState<string[]>([])
  const [tick, setTick] = useState(0)

  const serversState = useAsyncData(() => fetchMcpServers(), [tick])
  const catalogState = useAsyncData(() => fetchMcpCatalog(), [tick])

  // Light poll so probe badges stay current without a websocket.
  useEffect(() => {
    const id = window.setInterval(() => setTick((n) => n + 1), POLL_MS)
    return () => window.clearInterval(id)
  }, [])

  async function handleImport() {
    setImportError(null)
    setStripped([])
    setImporting(true)
    try {
      const result = await importMcpConfig(paste)
      setStripped(result.stripped_env_keys)
      setPaste('')
      setTick((n) => n + 1)
    } catch (err) {
      setImportError(err instanceof ApiForbiddenError ? t('mcp.forbidden') : describeApiError(err))
    } finally {
      setImporting(false)
    }
  }

  async function handleAdd(entry: McpCatalogEntry) {
    setImportError(null)
    try {
      await addMcpFromCatalog(entry.name)
      setTick((n) => n + 1)
    } catch (err) {
      setImportError(err instanceof ApiForbiddenError ? t('mcp.forbidden') : describeApiError(err))
    }
  }

  async function handleProbe(server: McpServer) {
    await probeMcpServer(server.id)
    setTick((n) => n + 1)
  }

  async function handleDelete(server: McpServer) {
    await deleteMcpServer(server.id)
    setTick((n) => n + 1)
  }

  return (
    <div className="flex flex-col gap-4" data-testid="mcp-view">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Plug className="h-5 w-5" />
            {t('nav.mcp')}
          </CardTitle>
          <CardDescription>{t('mcp.description')}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {canAdmin ? (
            <>
              <textarea
                data-testid="mcp-paste"
                className="min-h-24 w-full rounded-md border bg-background px-3 py-2 font-mono text-sm"
                placeholder={t('mcp.importPlaceholder')}
                value={paste}
                onChange={(e) => setPaste(e.target.value)}
              />
              <div className="flex items-center gap-2">
                <Button
                  data-testid="mcp-import"
                  disabled={!paste.trim() || importing}
                  onClick={() => void handleImport()}
                >
                  {importing ? t('mcp.importing') : t('mcp.import')}
                </Button>
                {importError ? (
                  <span role="alert" className="text-sm text-destructive">
                    {importError}
                  </span>
                ) : null}
              </div>
              {stripped.length > 0 ? (
                <p className="flex items-center gap-2 text-sm text-muted-foreground">
                  <TriangleAlert className="h-4 w-4" />
                  {t('mcp.secretsStripped', { keys: stripped.join(', ') })}
                </p>
              ) : null}
            </>
          ) : (
            <p className="text-sm text-muted-foreground">{t('mcp.readOnly')}</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('mcp.serversTitle')}</CardTitle>
          <CardDescription>{t('mcp.costNote')}</CardDescription>
        </CardHeader>
        <CardContent>
          <AsyncSection
            state={serversState}
            emptyMessage={t('mcp.noServers')}
            isEmpty={(data) => data.servers.length === 0}
          >
            {(data) => (
              <Table data-testid="mcp-servers-table">
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('mcp.server')}</TableHead>
                    <TableHead>{t('mcp.transport')}</TableHead>
                    <TableHead>{t('mcp.target')}</TableHead>
                    <TableHead>{t('mcp.status')}</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.servers.map((server) => (
                    <TableRow key={server.id} data-testid={`mcp-row-${server.name}`}>
                      <TableCell className="font-medium">{server.name}</TableCell>
                      <TableCell>{server.transport}</TableCell>
                      <TableCell className="font-mono text-xs">
                        {server.transport === 'http' ? server.url : [server.command, ...(server.args ?? [])].join(' ')}
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant={statusVariant(server.last_probe_status)}
                          data-testid={`mcp-status-${server.name}`}
                        >
                          {server.last_probe_status ?? t('mcp.unprobed')}
                        </Badge>
                      </TableCell>
                      <TableCell className="flex gap-2">
                        <Button size="sm" variant="outline" onClick={() => void handleProbe(server)}>
                          {t('mcp.probe')}
                        </Button>
                        {canAdmin ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => void handleDelete(server)}
                          >
                            {t('mcp.remove')}
                          </Button>
                        ) : null}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </AsyncSection>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('mcp.catalogTitle')}</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-2">
          <AsyncSection state={catalogState} isEmpty={(data) => data.catalog.length === 0}>
            {(data) => (
              <>
                {data.catalog.map((entry) => (
                  <div
                    key={entry.name}
                    className="flex flex-col gap-2 rounded-md border p-3"
                    data-testid={`mcp-catalog-${entry.name}`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium">{entry.name}</span>
                      {entry.installed ? (
                        <Badge variant="secondary">{t('mcp.installed')}</Badge>
                      ) : canAdmin ? (
                        <Button size="sm" onClick={() => void handleAdd(entry)}>
                          {t('mcp.add')}
                        </Button>
                      ) : null}
                    </div>
                    <p className="text-sm text-muted-foreground">{entry.description}</p>
                  </div>
                ))}
              </>
            )}
          </AsyncSection>
        </CardContent>
      </Card>
    </div>
  )
}
