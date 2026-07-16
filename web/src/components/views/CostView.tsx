import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { fetchAnalyticsCost, fetchAnalyticsProviders } from '@/lib/mirador-api'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

const DAYS = 30

function formatTokens(n: number): string {
  return n.toLocaleString('en-US')
}

function formatCost(n: number): string {
  return `$${n.toFixed(3)}`
}

/**
 * Cost tab — `GET /v1/analytics/cost` (token + cost totals, per
 * provider/model, plus `unpriced_steps` coverage) and `GET
 * /v1/analytics/providers` (volume/outcome split per provider/model).
 * Both fetch independently so one failing doesn't blank the other.
 */
export function CostView() {
  const cost = useAsyncData(() => fetchAnalyticsCost(DAYS), [DAYS])
  const providers = useAsyncData(() => fetchAnalyticsProviders(DAYS), [DAYS])

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Cost &amp; tokens</CardTitle>
          <CardDescription>Last {DAYS} days</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <AsyncSection
            state={cost}
            isEmpty={(data) => data.overall.total_steps === 0}
            emptyMessage="No cost data yet."
          >
            {(data) => (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="secondary">{formatCost(data.overall.cost_usd)} total</Badge>
                  <Badge variant="outline">{formatTokens(data.overall.input_tokens)} input tokens</Badge>
                  <Badge variant="outline">{formatTokens(data.overall.output_tokens)} output tokens</Badge>
                  {data.overall.unpriced_steps > 0 && (
                    <Badge variant="destructive">{data.overall.unpriced_steps} unpriced steps</Badge>
                  )}
                </div>

                {data.by_provider.length > 0 && (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Provider</TableHead>
                        <TableHead>Steps</TableHead>
                        <TableHead>Tokens (in/out)</TableHead>
                        <TableHead>Cost</TableHead>
                        <TableHead>Unpriced</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {data.by_provider.map((row) => (
                        <TableRow key={row.provider}>
                          <TableCell>{row.provider}</TableCell>
                          <TableCell>{row.total_steps}</TableCell>
                          <TableCell>
                            {formatTokens(row.input_tokens)} / {formatTokens(row.output_tokens)}
                          </TableCell>
                          <TableCell>{formatCost(row.cost_usd)}</TableCell>
                          <TableCell>{row.unpriced_steps}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}

                {data.by_model.length > 0 && (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Model</TableHead>
                        <TableHead>Steps</TableHead>
                        <TableHead>Tokens (in/out)</TableHead>
                        <TableHead>Cost</TableHead>
                        <TableHead>Unpriced</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {data.by_model.map((row) => (
                        <TableRow key={row.model}>
                          <TableCell>{row.model}</TableCell>
                          <TableCell>{row.total_steps}</TableCell>
                          <TableCell>
                            {formatTokens(row.input_tokens)} / {formatTokens(row.output_tokens)}
                          </TableCell>
                          <TableCell>{formatCost(row.cost_usd)}</TableCell>
                          <TableCell>{row.unpriced_steps}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </>
            )}
          </AsyncSection>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Provider &amp; model volume</CardTitle>
          <CardDescription>Step counts and outcome split</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <AsyncSection
            state={providers}
            isEmpty={(data) => data.by_provider.length === 0 && data.by_model.length === 0}
            emptyMessage="No provider/model data yet."
          >
            {(data) => (
              <>
                {data.by_provider.length > 0 && (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Provider</TableHead>
                        <TableHead>Total</TableHead>
                        <TableHead>Succeeded</TableHead>
                        <TableHead>Failed</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {data.by_provider.map((row) => (
                        <TableRow key={row.provider}>
                          <TableCell>{row.provider}</TableCell>
                          <TableCell>{row.total}</TableCell>
                          <TableCell>{row.outcomes.succeeded}</TableCell>
                          <TableCell>{row.outcomes.failed}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </>
            )}
          </AsyncSection>
        </CardContent>
      </Card>
    </div>
  )
}
