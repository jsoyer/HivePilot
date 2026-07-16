import { type FormEvent, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { fetchMemories, type MemoriesResponse } from '@/lib/mirador-api'
import { useAsyncData } from '@/lib/use-async-data'

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
 * token" message instead of the generic error card.
 */
export function Mem0View() {
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

  return (
    <Card>
      <CardHeader>
        <CardTitle>Mem0 memory search</CardTitle>
        <CardDescription>Semantic search over the mem0 store — requires an admin token</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <form className="flex gap-2" onSubmit={handleSubmit}>
          <Input
            value={inputValue}
            onChange={(event) => setInputValue(event.target.value)}
            placeholder="Search memories…"
            aria-label="Search memories"
          />
          <Button type="submit" disabled={!inputValue.trim()}>
            Search
          </Button>
        </form>

        {!hasSearched && (
          <p className="text-sm text-muted-foreground">Enter a search query above to look up memories.</p>
        )}

        {hasSearched && state.status === 'loading' && (
          <div role="status" className="animate-pulse text-sm text-muted-foreground">
            Searching…
          </div>
        )}

        {hasSearched && state.status === 'error' && (
          <>
            {state.error instanceof ApiForbiddenError ? (
              <div
                data-testid="mem0-forbidden"
                className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
              >
                This view requires an <span className="font-medium text-foreground">admin</span> token.
                Your current token can still use the other Mirador tabs — only Mem0 search needs a
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

        {hasSearched && state.status === 'success' && state.data && !state.data.configured && (
          <div className="rounded-lg border border-border bg-muted/50 p-3 text-sm text-muted-foreground">
            {state.data.detail ?? 'mem0 is not configured.'}
          </div>
        )}

        {hasSearched && state.status === 'success' && state.data?.configured && state.data.memories.length === 0 && (
          <p className="text-sm text-muted-foreground">No memories found for that query.</p>
        )}

        {hasSearched && state.status === 'success' && state.data?.configured && state.data.memories.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Category</TableHead>
                <TableHead>Project</TableHead>
                <TableHead>Task</TableHead>
                <TableHead>Timestamp</TableHead>
                <TableHead>Memory</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {state.data.memories.map((item, index) => (
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
        )}
      </CardContent>
    </Card>
  )
}
