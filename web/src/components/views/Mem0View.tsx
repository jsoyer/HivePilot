import { type FormEvent, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ApiForbiddenError } from '@/lib/api'
import { useT } from '@/lib/i18n'
import { fetchMemories, type MemoriesResponse } from '@/lib/mirador-api'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

const LIMIT = 20

/**
 * Mem0 tab — `GET /v1/memories?query=&limit=20`, a search box + a table of
 * typed memories (category/project/task/ts + text).
 *
 * **The one non-obvious correctness point of this sprint:** `/v1/memories`
 * is `require_role("admin")` (see `api_service.py`'s `list_memories`
 * docstring for why), but the token gate only validates a `read`-role
 * endpoint. A perfectly valid `read`/`run`/`approve` token therefore gets a
 * 403 here — which must NOT be treated as "the token is bad" (that would
 * clear it and kick the user back to the gate, breaking every OTHER tab
 * they were legitimately using). `fetchMemories` calls `apiFetch` with
 * `{ on403: 'forbidden' }` for exactly this reason: a 403 throws
 * `ApiForbiddenError` and leaves the token untouched. This view then
 * special-cases that one error type into a graceful "requires an admin
 * token" message instead of the generic error card — rendered BEFORE
 * `AsyncSection` (which handles every other loading/error/empty case) since
 * `AsyncSection` has no concept of this one endpoint-specific 403 carve-out.
 */
export function Mem0View() {
  const t = useT()
  const [inputValue, setInputValue] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState<string | null>(null)

  // Only calls the real endpoint once the user has submitted a search — no
  // fetch (and so no premature 403) fires on mount.
  const state = useAsyncData<MemoriesResponse | null>(
    () => (submittedQuery === null ? Promise.resolve(null) : fetchMemories(submittedQuery, LIMIT)),
    [submittedQuery],
  )

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmed = inputValue.trim()
    if (!trimmed) return
    setSubmittedQuery(trimmed)
  }

  const hasSearched = submittedQuery !== null
  const isForbidden = state.status === 'error' && state.error instanceof ApiForbiddenError

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('mem0.title')}</CardTitle>
        <CardDescription>{t('mem0.description')}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <form className="flex gap-2" onSubmit={handleSubmit}>
          <Input
            value={inputValue}
            onChange={(event) => setInputValue(event.target.value)}
            placeholder={t('mem0.searchPlaceholder')}
            aria-label={t('mem0.searchAriaLabel')}
          />
          <Button type="submit" disabled={!inputValue.trim()}>
            {t('mem0.searchButton')}
          </Button>
        </form>

        {!hasSearched && <p className="text-sm text-muted-foreground">{t('mem0.searchHint')}</p>}

        {hasSearched && isForbidden && (
          <div
            data-testid="mem0-forbidden"
            className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
          >
            {t('mem0.requiresTokenLead')} <span className="font-medium text-foreground">admin</span>{' '}
            {t('mem0.requiresTokenTail')} {t('mem0.requiresTokenNote')}
          </div>
        )}

        {hasSearched && !isForbidden && (
          <AsyncSection
            state={state}
            isEmpty={(data) => data !== null && data.configured && data.memories.length === 0}
            emptyMessage={t('mem0.noResults')}
          >
            {(data) =>
              data === null ? null : !data.configured ? (
                <div className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground">
                  {data.detail ?? t('mem0.notConfigured')}
                </div>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t('mem0.category')}</TableHead>
                      <TableHead>{t('common.project')}</TableHead>
                      <TableHead>{t('common.task')}</TableHead>
                      <TableHead>{t('mem0.timestamp')}</TableHead>
                      <TableHead>{t('mem0.memory')}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.memories.map((item, index) => (
                      <TableRow key={item.id ?? index}>
                        <TableCell>{item.metadata?.category ?? '—'}</TableCell>
                        <TableCell>{item.metadata?.project ?? '—'}</TableCell>
                        <TableCell>{item.metadata?.task ?? '—'}</TableCell>
                        <TableCell>{item.metadata?.ts ?? '—'}</TableCell>
                        <TableCell className="whitespace-normal">{item.memory}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )
            }
          </AsyncSection>
        )}
      </CardContent>
    </Card>
  )
}
