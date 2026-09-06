import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import {
  acceptSkillProposal,
  fetchSkillProposals,
  rejectSkillProposal,
  type SkillProposal,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { AsyncSection } from './AsyncSection'

const EMPTY: SkillProposal[] = []

export function SkillsWorkshopView() {
  const t = useT()
  const { can } = useRole()
  const canApprove = can('approve')
  const [tick, setTick] = useState(0)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const state = useAsyncData(() => fetchSkillProposals('proposed'), [tick])

  async function decide(id: string, accept: boolean) {
    setBusyId(id)
    setError(null)
    try {
      if (accept) await acceptSkillProposal(id)
      else await rejectSkillProposal(id)
      setTick((n) => n + 1)
    } catch (err) {
      setError(describeApiError(err))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>{t('workshop.title')}</CardTitle>
          <CardDescription>{t('workshop.subtitle')}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {error ? (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}
          <AsyncSection
            state={state}
            isEmpty={(rows) => rows.length === 0}
            emptyMessage={t('workshop.empty')}
          >
            {(rows) => (
              <ul className="flex flex-col gap-4" data-testid="workshop-list">
                {(rows ?? EMPTY).map((row) => (
                  <li
                    key={row.id}
                    data-testid={`workshop-row-${row.id}`}
                    className="flex flex-col gap-2 rounded-md border border-border p-3"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{row.skill_name}</span>
                      <Badge variant="outline">{row.status}</Badge>
                      {row.step ? (
                        <span className="text-xs text-muted-foreground">{row.step}</span>
                      ) : null}
                    </div>
                    {row.rationale ? (
                      <p className="text-sm text-muted-foreground">{row.rationale}</p>
                    ) : null}
                    <pre
                      className="max-h-64 overflow-auto rounded bg-muted p-2 text-xs"
                      data-testid={`workshop-diff-${row.id}`}
                    >
                      {row.diff_text}
                    </pre>
                    {canApprove ? (
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          disabled={busyId === row.id}
                          onClick={() => void decide(row.id, true)}
                        >
                          {t('workshop.accept')}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busyId === row.id}
                          onClick={() => void decide(row.id, false)}
                        >
                          {t('workshop.reject')}
                        </Button>
                      </div>
                    ) : (
                      <p className="text-xs text-muted-foreground">{t('workshop.needApprove')}</p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </AsyncSection>
        </CardContent>
      </Card>
    </div>
  )
}
