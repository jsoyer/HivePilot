import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { describeApiError } from '@/lib/format-error'
import { useT } from '@/lib/i18n'
import {
  acceptSkillEvolution,
  acceptSkillProposal,
  approveSkillEvolution,
  fetchSkillEvolutions,
  fetchSkillProposals,
  rejectSkillEvolution,
  rejectSkillProposal,
  type SkillEvolutionCard,
  type SkillProposal,
} from '@/lib/pollen-api'
import { useRole } from '@/lib/role-context'
import { useAsyncData } from '@/lib/use-async-data'
import { EvolutionLineage } from './EvolutionLineage'
import { AsyncSection } from './AsyncSection'

const EMPTY_PATCHES: SkillProposal[] = []
const EMPTY_EVOLUTIONS: SkillEvolutionCard[] = []

export function SkillsWorkshopView() {
  const t = useT()
  const { can } = useRole()
  const canApprove = can('approve')
  const [tick, setTick] = useState(0)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const patches = useAsyncData(() => fetchSkillProposals('proposed'), [tick])
  const evolutions = useAsyncData(() => fetchSkillEvolutions(), [tick])

  async function decidePatch(id: string, accept: boolean) {
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

  async function decideEvolution(row: SkillEvolutionCard, action: 'approve' | 'reject' | 'accept') {
    setBusyId(row.id)
    setError(null)
    try {
      if (action === 'approve') await approveSkillEvolution(row.id)
      else if (action === 'reject') await rejectSkillEvolution(row.id)
      else await acceptSkillEvolution(row.id, row.content_hash)
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
          <CardTitle>{t('workshop.evolutionTitle')}</CardTitle>
          <CardDescription>{t('workshop.evolutionSubtitle')}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {error ? (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}
          <AsyncSection
            state={evolutions}
            isEmpty={(rows) => rows.length === 0}
            emptyMessage={t('workshop.evolutionEmpty')}
          >
            {(rows) => (
              <ul className="flex flex-col gap-4" data-testid="evolution-list">
                {(rows ?? EMPTY_EVOLUTIONS).map((row) => (
                  <li
                    key={row.id}
                    data-testid={`evolution-row-${row.id}`}
                    className="flex flex-col gap-3 rounded-md border border-border p-3"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{row.name || row.id}</span>
                      <Badge variant="outline">{row.evolution_type || row.origin}</Badge>
                      <Badge variant="secondary">{row.status}</Badge>
                      {row.validation && typeof row.validation.result === 'string' ? (
                        <span className="text-xs text-muted-foreground">
                          {t('workshop.validation')}: {row.validation.result}
                        </span>
                      ) : null}
                    </div>
                    <div>
                      <p className="mb-1 text-xs font-medium">{t('workshop.lineage')}</p>
                      <EvolutionLineage lineage={row.lineage} />
                    </div>
                    <div>
                      <p className="mb-1 text-xs font-medium">{t('workshop.files')}</p>
                      <ul className="flex flex-col gap-2">
                        {row.diffs.map((diff) => (
                          <li key={diff.path}>
                            <p className="font-mono text-xs">{diff.path}</p>
                            <pre
                              className="max-h-48 overflow-auto rounded bg-muted p-2 text-xs"
                              data-testid={`evolution-diff-${row.id}-${diff.path}`}
                            >
                              {diff.unified}
                            </pre>
                          </li>
                        ))}
                      </ul>
                    </div>
                    {canApprove ? (
                      <div className="flex flex-col gap-2">
                        <div className="flex flex-wrap gap-2">
                          {row.status === 'PENDING' ? (
                            <>
                              <Button
                                size="sm"
                                disabled={busyId === row.id}
                                onClick={() => void decideEvolution(row, 'approve')}
                              >
                                {t('workshop.approve')}
                              </Button>
                              <Button
                                size="sm"
                                variant="outline"
                                disabled={busyId === row.id}
                                onClick={() => void decideEvolution(row, 'reject')}
                              >
                                {t('workshop.reject')}
                              </Button>
                            </>
                          ) : null}
                          <Button
                            size="sm"
                            disabled={busyId === row.id || row.status !== 'APPROVED' || row.applied}
                            onClick={() => void decideEvolution(row, 'accept')}
                            data-testid={`evolution-accept-${row.id}`}
                          >
                            {t('workshop.accept')}
                          </Button>
                        </div>
                        <p className="text-xs text-muted-foreground">
                          {row.status === 'APPROVED' && !row.applied
                            ? t('workshop.acceptReady')
                            : t('workshop.acceptBlocked')}
                        </p>
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
      <Card>
        <CardHeader>
          <CardTitle>{t('workshop.title')}</CardTitle>
          <CardDescription>{t('workshop.subtitle')}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <AsyncSection
            state={patches}
            isEmpty={(rows) => rows.length === 0}
            emptyMessage={t('workshop.empty')}
          >
            {(rows) => (
              <ul className="flex flex-col gap-4" data-testid="workshop-list">
                {(rows ?? EMPTY_PATCHES).map((row) => (
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
                          onClick={() => void decidePatch(row.id, true)}
                        >
                          {t('workshop.accept')}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busyId === row.id}
                          onClick={() => void decidePatch(row.id, false)}
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
