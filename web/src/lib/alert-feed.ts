/**
 * Worst-first Alerts feed (redesign PR3).
 *
 * One list, three kinds — Failed runs, then Degraded plugins/probes, then
 * Classifier. The header chip counts the same items (`countsAsIssue`), so
 * "N issues" and this page cannot drift.
 *
 * Does NOT dump System/Health: no service checks, no OTel card, no never-ran
 * contradictions. Those stay under Plus → System.
 *
 * `RunSummary.detail` is untrusted — never copied onto a row.
 */

import type {
  AgentSurfaceProbe,
  PluginDenied,
  PluginHealthEntry,
  RunSummary,
} from './pollen-api'
import { FAILED_STATUSES } from './status-contract'

export type AlertKind = 'failed' | 'degraded' | 'classifier'

export type AlertMetaKey = 'installedDisabled' | 'degradedFallback'

export interface AlertItem {
  id: string
  kind: AlertKind
  /** Display title for Failed/Degraded. Classifier titles are i18n'd in the view. */
  title: string
  /** Server-owned meta (plugin health detail, project name). Empty when `metaKey` applies. */
  meta: string
  metaKey?: AlertMetaKey
  /** Instant for `formatAge`. Null → em dash, never "now". */
  at: string | null
  countsAsIssue: boolean
  classifierState?: 'ok' | 'down'
  backend?: string | null
}

export interface AlertFeedInput {
  plugins?: ReadonlyArray<Pick<PluginHealthEntry, 'name' | 'status' | 'detail' | 'activity'>>
  disabled?: readonly string[]
  denied?: ReadonlyArray<Pick<PluginDenied, 'name' | 'error'>>
  notInstalled?: readonly string[]
  runs?: ReadonlyArray<
    Pick<RunSummary, 'id' | 'project' | 'task' | 'status' | 'started_at' | 'finished_at' | 'last_activity_at'> & {
      /** Present on the wire — this feed must never copy it onto a row. */
      detail?: string | null
    }
  >
  agentSurface?: Pick<AgentSurfaceProbe, 'state' | 'backend'> | null
}

const HAS_ZONE = /([zZ]|[+-]\d{2}:?\d{2})$/

function instantMs(iso: string | null | undefined): number {
  if (!iso) return 0
  const normalised = HAS_ZONE.test(iso) ? iso : `${iso.trim().replace(' ', 'T')}Z`
  const date = new Date(normalised)
  return Number.isNaN(date.getTime()) ? 0 : date.getTime()
}

function runInstant(run: Pick<RunSummary, 'started_at' | 'finished_at' | 'last_activity_at'>): string | null {
  return run.finished_at ?? run.last_activity_at ?? run.started_at ?? null
}

function pluginInstant(plugin: Pick<PluginHealthEntry, 'activity'>): string | null {
  return plugin.activity?.last_used ?? null
}

function failedRows(
  runs: NonNullable<AlertFeedInput['runs']>,
): AlertItem[] {
  return runs
    .filter((run) => FAILED_STATUSES.has(run.status.trim().toLowerCase()))
    .map((run) => ({
      id: `failed-${run.id}`,
      kind: 'failed' as const,
      title: `${run.task} #${run.id}`,
      meta: run.project,
      at: runInstant(run),
      countsAsIssue: true,
    }))
    .sort((a, b) => instantMs(b.at) - instantMs(a.at) || a.id.localeCompare(b.id))
}

function degradedFromPlugins(
  plugins: NonNullable<AlertFeedInput['plugins']>,
): AlertItem[] {
  return plugins
    .filter((plugin) => plugin.status === 'degraded' || plugin.status === 'error')
    .map((plugin) => ({
      id: `plugin-${plugin.name}`,
      kind: 'degraded' as const,
      title: plugin.name,
      meta: plugin.detail.trim(),
      metaKey: plugin.detail.trim() ? undefined : ('degradedFallback' as const),
      at: pluginInstant(plugin),
      countsAsIssue: true,
    }))
}

function degradedFromDenied(
  denied: NonNullable<AlertFeedInput['denied']>,
): AlertItem[] {
  return denied.map((entry) => ({
    id: `denied-${entry.name}`,
    kind: 'degraded' as const,
    title: entry.name,
    meta: entry.error.trim(),
    metaKey: entry.error.trim() ? undefined : ('degradedFallback' as const),
    at: null,
    countsAsIssue: true,
  }))
}

function degradedFromDisabled(
  disabled: readonly string[],
  loadedNames: ReadonlySet<string>,
  notInstalled: ReadonlySet<string>,
): AlertItem[] {
  return disabled
    .filter((name) => !loadedNames.has(name) && !notInstalled.has(name))
    .map((name) => ({
      id: `disabled-${name}`,
      kind: 'degraded' as const,
      title: name,
      meta: '',
      metaKey: 'installedDisabled' as const,
      at: null,
      countsAsIssue: true,
    }))
}

function classifierRow(
  surface: Pick<AgentSurfaceProbe, 'state' | 'backend'> | null | undefined,
): AlertItem | null {
  if (!surface) return null
  if (surface.state === 'ok') {
    return {
      id: 'classifier',
      kind: 'classifier',
      title: '',
      meta: '',
      at: null,
      countsAsIssue: false,
      classifierState: 'ok',
      backend: surface.backend,
    }
  }
  if (surface.state === 'unreachable') {
    return {
      id: 'classifier',
      kind: 'classifier',
      title: '',
      meta: '',
      at: null,
      countsAsIssue: true,
      classifierState: 'down',
      backend: surface.backend,
    }
  }
  return null
}

function sortDegraded(items: AlertItem[]): AlertItem[] {
  return [...items].sort((a, b) => {
    const byTime = instantMs(b.at) - instantMs(a.at)
    if (byTime !== 0) return byTime
    return a.title.localeCompare(b.title)
  })
}

/** Compose the Alerts list: Failed → Degraded → Classifier. */
export function buildAlertFeed(input: AlertFeedInput): AlertItem[] {
  const plugins = input.plugins ?? []
  const failed = failedRows(input.runs ?? [])
  const loaded = new Set(plugins.map((plugin) => plugin.name))
  const notInstalled = new Set(input.notInstalled ?? [])
  const degraded = sortDegraded([
    ...degradedFromPlugins(plugins),
    ...degradedFromDenied(input.denied ?? []),
    ...degradedFromDisabled(input.disabled ?? [], loaded, notInstalled),
  ])
  const classifier = classifierRow(input.agentSurface)
  return classifier ? [...failed, ...degraded, classifier] : [...failed, ...degraded]
}

export function countAlertIssues(items: readonly AlertItem[]): number {
  return items.filter((item) => item.countsAsIssue).length
}
