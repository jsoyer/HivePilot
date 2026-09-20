import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { formatAge } from '@/lib/format-time'
import { useT } from '@/lib/i18n'
import {
  fetchConversationRuns,
  fetchConversationThread,
  replyToRole,
  type ConversationRun,
  type ConversationThread,
} from '@/lib/pollen-api'
import { cn } from '@/lib/utils'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'
import { ConversationFil } from './ConversationMessageRow'
import { RoleBadge } from './RoleBadge'

/**
 * The agents' exchanges, read as a conversation rather than as a log.
 *
 * Nested under Runs as the third surface (Board | History | Conversations),
 * not a fifth sidebar door (HP-119 / Aphrodite). Every stage output is
 * already an `interactions` row with `metadata.role` and `target`.
 *
 * **Replying is not a chat.** By the time a thread is readable its agents
 * have exited, so a reply addresses the ROLE, appending to the corrections
 * file that feeds that role's next run.
 */

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
          data-testid={`conversation-run-${run.run_id}`}
          onClick={() => onSelect(run.run_id)}
          className={cn(
            'flex flex-col items-start rounded-[10px] border bg-background px-3 py-2.5 text-left',
            selected === run.run_id ? 'border-border bg-muted/40' : 'border-border',
          )}
        >
          <span className="text-sm font-medium tabular-nums">#{run.run_id}</span>
          <span className="mt-0.5 text-[11px] text-muted-foreground">
            {[run.project, `${run.message_count} ${t('conversations.messages')}`, formatAge(run.started_at)]
              .filter(Boolean)
              .join(' · ')}
          </span>
          <div className="mt-2 flex flex-wrap gap-1">
            {run.roles.map((role) => (
              <RoleBadge key={role} role={role} />
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
    <div className="mt-auto flex flex-col gap-2 border-t border-border pt-3">
      <p className="text-[11px] text-muted-foreground">{t('conversations.replyReachesNextRun')}</p>
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label={t('conversations.role')}
          value={role}
          onChange={(e) => setRole(e.target.value)}
          className="rounded-lg border border-border bg-background px-2.5 py-2 text-xs"
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
      <div className="flex flex-wrap gap-2">
        <textarea
          aria-label={t('conversations.reply')}
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={2}
          placeholder={t('conversations.replyPlaceholder')}
          className="min-w-0 flex-1 rounded-lg border border-border bg-background p-2 text-sm"
        />
        <Button onClick={send} disabled={!text.trim() || !role || state === 'sending'}>
          {t('conversations.send')}
        </Button>
      </div>
    </div>
  )
}

function Panel({ runs }: { runs: ConversationRun[] }) {
  const t = useT()
  const [selected, setSelected] = useState<number | null>(runs[0]?.run_id ?? null)
  const [thread, setThread] = useState<ConversationThread | null>(null)

  useEffect(() => {
    if (selected === null) return
    let live = true
    fetchConversationThread(selected)
      .then((next) => {
        if (live) setThread(next)
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
    <div
      className="grid min-h-[460px] gap-3 lg:grid-cols-[240px_1fr]"
      data-testid="runs-conversations"
    >
      <aside className="flex flex-col rounded-xl border border-border bg-card p-3">
        <h3 className="mb-2.5 text-xs font-medium text-muted-foreground">
          {t('conversations.runsWithSpeech')}
        </h3>
        <RunList runs={runs} selected={selected} onSelect={setSelected} />
      </aside>

      <section className="flex flex-col rounded-xl border border-border bg-card p-3">
        <div className="mb-3 flex flex-wrap items-baseline gap-2 border-b border-border pb-2.5">
          <b className="text-sm font-medium">
            {selected === null ? t('conversations.noRun') : `#${selected}`}
          </b>
          <span className="text-xs text-muted-foreground">{t('conversations.filHint')}</span>
          <div className="ml-auto flex flex-wrap gap-1">
            {roles.map((role) => (
              <RoleBadge key={role} role={role} />
            ))}
          </div>
        </div>
        <div className="flex max-h-[55vh] flex-col gap-2 overflow-y-auto">
          {thread && <ConversationFil messages={thread.messages} />}
        </div>
        {roles.length > 0 && <ReplyBox roles={roles} />}
      </section>
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
