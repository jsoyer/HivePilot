import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useT } from '@/lib/i18n'
import {
  fetchConversationRuns,
  fetchConversationThread,
  replyToRole,
  type ConversationMessage,
  type ConversationRun,
  type ConversationThread,
} from '@/lib/pollen-api'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

/**
 * The agents' exchanges, read as a conversation rather than as a log.
 *
 * Every stage's output has been persisted as an `interactions` row carrying its
 * role key all along — that is how the blocked-gate report finds each role's
 * reasoning. Nothing ever presented those rows as what they are: one thread per
 * run, one voice per role. So this adds no capture; it is a surface.
 *
 * **Replying is not a chat.** By the time a thread is readable its agents have
 * exited, so a reply addresses the ROLE, appending to the corrections file that
 * feeds that role's next run. The panel says so out loud, because a reply box
 * that silently changed nothing would be worse than none — it would look like
 * it had worked.
 */

function Speaker({ message }: { message: ConversationMessage }) {
  return (
    <div className="flex flex-col gap-1" data-testid={`message-${message.interaction_id}`}>
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-sm font-semibold">{message.actor}</span>
        {message.role && (
          <Badge variant="outline" className="text-xs">
            {message.role}
          </Badge>
        )}
        {message.at && (
          <span className="text-xs tabular-nums text-muted-foreground">{message.at}</span>
        )}
      </div>
      <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-md bg-muted/40 p-3 text-sm">
        {message.body}
      </pre>
    </div>
  )
}

function RunList({
  runs,
  selected,
  onSelect,
}: {
  runs: ConversationRun[]
  selected: number | null
  onSelect: (runId: number) => void
}) {
  const t = useT()
  return (
    <div className="flex max-h-[70vh] flex-col gap-2 overflow-y-auto">
      {runs.map((run) => (
        <button
          key={run.run_id}
          type="button"
          onClick={() => onSelect(run.run_id)}
          className={`flex flex-col items-start gap-1 rounded-md border p-3 text-left transition-colors hover:bg-muted/50 ${
            selected === run.run_id ? 'border-primary bg-muted/60' : 'border-border'
          }`}
        >
          <div className="flex w-full items-baseline justify-between gap-2">
            <span className="font-medium tabular-nums">#{run.run_id}</span>
            <span className="text-xs text-muted-foreground">
              {run.message_count} {t('conversations.messages')}
            </span>
          </div>
          {run.project && <span className="text-xs text-muted-foreground">{run.project}</span>}
          <div className="flex flex-wrap gap-1">
            {run.roles.map((role) => (
              <Badge key={role} variant="secondary" className="text-[10px]">
                {role}
              </Badge>
            ))}
          </div>
        </button>
      ))}
    </div>
  )
}

function ReplyBox({ roles }: { roles: string[] }) {
  const t = useT()
  const [role, setRole] = useState(roles[0] ?? '')
  const [text, setText] = useState('')
  const [state, setState] = useState<'idle' | 'sending' | 'sent' | 'failed'>('idle')

  useEffect(() => {
    if (!role && roles.length) setRole(roles[0])
  }, [roles, role])

  const send = useCallback(async () => {
    setState('sending')
    try {
      await replyToRole(role, text.trim())
      setText('')
      setState('sent')
    } catch {
      setState('failed')
    }
  }, [role, text])

  return (
    <div className="flex flex-col gap-2 border-t pt-4">
      {/* Stated before the controls, not after: this does not reach the run
          above, and a reader who assumes otherwise has already been misled. */}
      <p className="text-xs text-muted-foreground">{t('conversations.replyReachesNextRun')}</p>
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label={t('conversations.role')}
          value={role}
          onChange={(e) => setRole(e.target.value)}
          className="rounded-md border border-border bg-background px-2 py-1 text-sm"
        >
          {roles.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        {state === 'sent' && <span className="text-xs text-green-600">{t('conversations.sent')}</span>}
        {state === 'failed' && (
          <span className="text-xs text-destructive">{t('conversations.failed')}</span>
        )}
      </div>
      <textarea
        aria-label={t('conversations.reply')}
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={3}
        placeholder={t('conversations.replyPlaceholder')}
        className="w-full rounded-md border border-border bg-background p-2 text-sm"
      />
      <div>
        <Button onClick={send} disabled={!text.trim() || !role || state === 'sending'}>
          {t('conversations.send')}
        </Button>
      </div>
    </div>
  )
}

function Panel({ runs }: { runs: ConversationRun[] }) {
  const t = useT()
  // The newest run opens without a click: an empty right-hand pane on load
  // reads as "there is nothing here".
  const [selected, setSelected] = useState<number | null>(runs[0]?.run_id ?? null)
  const [thread, setThread] = useState<ConversationThread | null>(null)

  useEffect(() => {
    if (selected === null) return
    let live = true
    fetchConversationThread(selected)
      .then((t) => {
        if (live) setThread(t)
      })
      .catch(() => {
        if (live) setThread(null)
      })
    return () => {
      live = false
    }
  }, [selected])

  const roles = thread?.roles ?? []

  return (
    <div className="grid gap-4 lg:grid-cols-[18rem_1fr]">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t('conversations.runs')}</CardTitle>
          <CardDescription>{t('conversations.runsHint')}</CardDescription>
        </CardHeader>
        <CardContent>
          <RunList runs={runs} selected={selected} onSelect={setSelected} />
        </CardContent>
      </Card>

      <Card className="flex flex-col">
        <CardHeader>
          <CardTitle className="text-base">
            {selected === null ? t('conversations.noRun') : `#${selected}`}
          </CardTitle>
          <CardDescription>{t('conversations.threadHint')}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex max-h-[55vh] flex-col gap-4 overflow-y-auto">
            {(thread?.messages ?? []).map((m) => (
              <Speaker key={m.interaction_id} message={m} />
            ))}
            {thread && thread.messages.length === 0 && (
              <p className="text-sm text-muted-foreground">{t('conversations.emptyThread')}</p>
            )}
          </div>
          {roles.length > 0 && <ReplyBox roles={roles} />}
        </CardContent>
      </Card>
    </div>
  )
}

export function ConversationsView() {
  const data = useAsyncData(() => fetchConversationRuns(25), [])

  return (
    <AsyncSection state={data} isEmpty={(d) => d.runs.length === 0}>
      {(loaded) => <Panel runs={loaded.runs} />}
    </AsyncSection>
  )
}
