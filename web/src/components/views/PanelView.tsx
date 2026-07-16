import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { fetchPanel } from '@/lib/mirador-api'
import { useAsyncData } from '@/lib/use-async-data'
import { PanelRenderer } from './PanelRenderer'

interface PanelViewProps {
  name: string
  title: string
  minRole: string
}

/**
 * Generic tab body for a single plugin-contributed Mirador panel (Sprint 3
 * web surface) — `GET /v1/panels/{name}`, rendered via `PanelRenderer`.
 *
 * Mirrors `Mem0View`'s 403 handling: a panel's own `min_role` may be higher
 * than the token gate's floor check (exactly like `/v1/memories`'s `admin`
 * gate), so `fetchPanel` calls `apiFetch` with `{ on403: 'forbidden' }`. A
 * 403 here throws `ApiForbiddenError` and leaves the token untouched; this
 * view special-cases that one error type into a graceful "requires a
 * <minRole> token" message instead of the generic error card, so a valid
 * lower-role token keeps working for every other Mirador tab.
 */
export function PanelView({ name, title, minRole }: PanelViewProps) {
  const state = useAsyncData(() => fetchPanel(name), [name])

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>Plugin panel</CardDescription>
      </CardHeader>
      <CardContent>
        {state.status === 'loading' && (
          <div role="status" className="animate-pulse text-sm text-muted-foreground">
            Loading…
          </div>
        )}

        {state.status === 'error' && (
          <>
            {state.error instanceof ApiForbiddenError ? (
              <div
                data-testid="panel-forbidden"
                className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
              >
                This panel requires a{' '}
                <span className="font-medium text-foreground">{minRole}</span> token. Your
                current token can still use the other Mirador tabs — only this panel needs a
                higher role.
              </div>
            ) : (
              <div
                role="alert"
                className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
              >
                {describeApiError(state.error)}
              </div>
            )}
          </>
        )}

        {state.status === 'success' && <PanelRenderer data={state.data} />}
      </CardContent>
    </Card>
  )
}
