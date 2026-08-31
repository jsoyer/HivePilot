import { Send } from 'lucide-react'
import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react'
import { EmptyState } from '@/components/dashboard/EmptyState'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ApiForbiddenError } from '@/lib/api'
import { describeApiError } from '@/lib/format-error'
import { formatAge, formatTimestamp } from '@/lib/format-time'
import { useT } from '@/lib/i18n'
import {
  fetchSpaceMessages,
  fetchSpaces,
  postSpaceMessage,
  type SpaceMessage,
  type SpaceSummary,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { useEventStream } from '@/lib/use-event-stream'
import { cn } from '@/lib/utils'

const NO_SPACES: SpaceSummary[] = []
const NO_MESSAGES: SpaceMessage[] = []

function spaceLabel(space: SpaceSummary, you: string): string {
  if (space.title) return space.title
  const names = space.participants.map((p) => (p.type === 'role' ? (p.id ?? 'agent') : you))
  return names.join(' & ') || `#${space.id}`
}

/**
 * Espaces (HP-45) — conversation rooms. Left: the room list (recency-ordered,
 * with unread-agnostic message counts). Right: the selected room's transcript
 * + a composer. Messages update live over the realtime bus (HP-41 SSE): a
 * `space.message` event for the open room refetches its thread, and any space
 * event refreshes the list order/counts. Posting is `run`-gated (the composer
 * hides for a read-only token; the server enforces it regardless).
 */
export function EspacesView() {
  const t = useT()
  const { can } = useRole()
  const canPost = can('run')

  const [spacesKey, setSpacesKey] = useState(0)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [messagesKey, setMessagesKey] = useState(0)

  const spacesState = useAsyncData(() => fetchSpaces(), [spacesKey])
  const spaces = spacesState.status === 'success' ? spacesState.data : NO_SPACES

  // Auto-select the most recent room once the list arrives.
  useEffect(() => {
    if (selectedId === null && spaces.length > 0) setSelectedId(spaces[0].id)
  }, [spaces, selectedId])

  const messagesState = useAsyncData(
    () => (selectedId === null ? Promise.resolve(NO_MESSAGES) : fetchSpaceMessages(selectedId)),
    [selectedId, messagesKey],
  )
  const messages = messagesState.status === 'success' ? messagesState.data : NO_MESSAGES

  // Roles currently "working…" in the open room (the dépose/relève battement,
  // HP-46) — driven by space.typing / space.typing_stop events.
  const [typing, setTyping] = useState<string[]>([])
  useEffect(() => setTyping([]), [selectedId])

  // Live: a message in the open room refetches its thread; any space event
  // refreshes the list (recency + counts); typing events drive the indicator.
  useEventStream((event) => {
    if (event.entity_type !== 'space') return
    const role = ((event.payload ?? {}) as { role?: string }).role
    const forSelected = selectedId !== null && event.entity_id === String(selectedId)
    if (event.kind === 'space.typing') {
      if (forSelected && role) setTyping((t) => (t.includes(role) ? t : [...t, role]))
      return
    }
    if (event.kind === 'space.typing_stop') {
      if (forSelected && role) setTyping((t) => t.filter((r) => r !== role))
      return
    }
    setSpacesKey((k) => k + 1)
    if (forSelected) {
      setMessagesKey((k) => k + 1)
      if (role) setTyping((t) => t.filter((r) => r !== role))
    }
  })

  const threadRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    const el = threadRef.current
    // Guard: jsdom (tests) doesn't implement Element.scrollTo.
    if (el && typeof el.scrollTo === 'function') {
      el.scrollTo({ top: el.scrollHeight })
    }
  }, [messages])

  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function send() {
    const body = draft.trim()
    if (selectedId === null || !body || sending) return
    setSending(true)
    setError(null)
    try {
      await postSpaceMessage(selectedId, body)
      setDraft('')
      setMessagesKey((k) => k + 1)
      setSpacesKey((k) => k + 1)
    } catch (err) {
      setError(err instanceof ApiForbiddenError ? t('spaces.postForbidden') : describeApiError(err))
    } finally {
      setSending(false)
    }
  }

  function onComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      void send()
    }
  }

  const selected = useMemo(
    () => spaces.find((s) => s.id === selectedId) ?? null,
    [spaces, selectedId],
  )

  return (
    <Card data-testid="espaces-view">
      <CardHeader>
        <CardTitle>{t('nav.spaces')}</CardTitle>
        <CardDescription>{t('spaces.description')}</CardDescription>
      </CardHeader>
      <CardContent>
        {spacesState.status === 'loading' && (
          <div role="status" className="animate-pulse text-sm text-muted-foreground">
            {t('common.loading')}
          </div>
        )}

        {spacesState.status === 'error' && (
          <div
            role="alert"
            className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
          >
            {describeApiError(spacesState.error)}
          </div>
        )}

        {spacesState.status === 'success' && spaces.length === 0 && (
          <EmptyState
            data-testid="espaces-empty"
            title={t('spaces.emptyTitle')}
            body={t('spaces.emptyBody')}
            className="max-w-xl"
          />
        )}

        {spacesState.status === 'success' && spaces.length > 0 && (
          <div className="grid gap-4 sm:grid-cols-[16rem_1fr]">
            {/* Room list */}
            <ul data-testid="espaces-list" className="flex max-h-[32rem] flex-col gap-1 overflow-y-auto">
              {spaces.map((space) => (
                <li key={space.id}>
                  <button
                    type="button"
                    data-testid={`espaces-space-${space.id}`}
                    aria-pressed={space.id === selectedId}
                    onClick={() => setSelectedId(space.id)}
                    className={cn(
                      'flex w-full flex-col gap-0.5 rounded-lg border px-3 py-2 text-left transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
                      space.id === selectedId
                        ? 'border-primary bg-primary/10'
                        : 'border-border hover:bg-muted/50',
                    )}
                  >
                    <span className="truncate text-sm font-medium">
                      {spaceLabel(space, t('spaces.you'))}
                    </span>
                    <span className="metric-mono text-xs text-muted-foreground">
                      {t('spaces.messageCount', { count: space.message_count ?? 0 })}
                      {space.last_message_at
                        ? ` · ${t('home.ageAgo', { age: formatAge(space.last_message_at) })}`
                        : ''}
                    </span>
                  </button>
                </li>
              ))}
            </ul>

            {/* Thread + composer */}
            <div className="flex min-h-[24rem] flex-col rounded-lg border border-border">
              <div
                ref={threadRef}
                data-testid="espaces-thread"
                className="flex flex-1 flex-col gap-2 overflow-y-auto p-3"
              >
                {selected === null ? (
                  <p className="text-sm text-muted-foreground">{t('spaces.noSelection')}</p>
                ) : messages.length === 0 ? (
                  <p className="text-sm text-muted-foreground">{t('spaces.noMessages')}</p>
                ) : (
                  messages.map((message) => {
                    const mine = message.sender_type === 'human'
                    return (
                      <div
                        key={message.id}
                        data-testid={`espaces-message-${message.id}`}
                        className={cn('flex flex-col gap-0.5', mine ? 'items-end' : 'items-start')}
                      >
                        <span className="metric-mono text-[10px] text-muted-foreground">
                          {mine ? t('spaces.you') : (message.sender_id ?? 'agent')}
                          {' · '}
                          <span title={formatTimestamp(message.created_at)}>
                            {formatAge(message.created_at)}
                          </span>
                        </span>
                        <span
                          className={cn(
                            'max-w-[85%] rounded-lg px-3 py-1.5 text-sm whitespace-pre-wrap',
                            mine ? 'bg-primary text-primary-foreground' : 'bg-muted',
                          )}
                        >
                          {message.body}
                        </span>
                      </div>
                    )
                  })
                )}
                {typing.length > 0 && (
                  <div
                    data-testid="espaces-typing"
                    className="text-xs text-muted-foreground italic"
                  >
                    {t('spaces.typing', { role: typing.join(', ') })}
                  </div>
                )}
              </div>

              {canPost && selected !== null && (
                <div className="flex flex-col gap-1 border-t border-border p-2">
                  {error && (
                    <div role="alert" className="text-xs text-destructive">
                      {error}
                    </div>
                  )}
                  <div className="flex items-end gap-2">
                    <textarea
                      data-testid="espaces-composer"
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={onComposerKeyDown}
                      rows={2}
                      placeholder={t('spaces.composerPlaceholder', {
                        space: spaceLabel(selected, t('spaces.you')),
                      })}
                      className="flex-1 resize-none rounded-md border border-border bg-background px-2.5 py-1.5 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                    />
                    <Button
                      data-testid="espaces-send"
                      size="sm"
                      className="gap-1.5"
                      disabled={sending || !draft.trim()}
                      onClick={() => void send()}
                    >
                      <Send className="size-4" />
                      {sending ? t('spaces.sending') : t('spaces.send')}
                    </Button>
                  </div>
                </div>
              )}

              {!canPost && selected !== null && (
                <div className="border-t border-border p-2 text-xs text-muted-foreground">
                  {t('spaces.readOnly')}
                </div>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
