/**
 * Collapse run-scoped memory namespaces so a trend can actually form.
 *
 * Some namespaces carry the run id as their first segment:
 *
 *   af10fde4-94d9-49fb-897f-b9b7f2423382:noxys-developer:developer
 *
 * Every run therefore mints a *new* namespace, so the same role shows up as
 * N separate entries with one gap each and nothing ever accumulates. On real
 * data that turned 4 gaps for `noxys-developer` into four indistinguishable
 * 8% slivers, alongside seven other 8% slivers — eleven segments of identical
 * size, which is precisely the shape that carries no information.
 *
 * Only a leading **UUID** segment is stripped. `noxys:noxys-cto-review:cto`
 * keeps its `noxys` prefix, because that is a project, not a run — dropping
 * segments by position rather than by shape would merge namespaces that are
 * genuinely distinct.
 */

export interface MemoryGap {
  namespace: string
  no_result_count: number
  top_queries: string[]
}

export interface GroupedGap {
  /** The namespace with any leading run id removed — the grouping key. */
  scope: string
  count: number
  /** How many distinct namespaces collapsed into this one. Greater than 1
   *  means the same scope was seen across that many runs, which is worth
   *  showing: a gap that recurs run after run is a different problem from
   *  one that happened once, and the count alone cannot tell them apart. */
  runs: number
  topQueries: string[]
}

/** 8-4-4-4-12 hex, the shape run-id namespaces use. */
const LEADING_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:/i

export function scopeOf(namespace: string): string {
  return namespace.replace(LEADING_UUID, '')
}

/**
 * Group gaps by scope, most gaps first.
 *
 * Ties break on the scope name so the order is stable between renders — a
 * list that reshuffles on refresh is one nobody can read twice.
 */
export function groupGapsByScope(gaps: MemoryGap[]): GroupedGap[] {
  const byScope = new Map<string, GroupedGap>()

  for (const gap of gaps) {
    const scope = scopeOf(gap.namespace)
    const existing = byScope.get(scope)
    if (existing) {
      existing.count += gap.no_result_count
      existing.runs += 1
      for (const query of gap.top_queries) {
        if (!existing.topQueries.includes(query)) existing.topQueries.push(query)
      }
    } else {
      byScope.set(scope, {
        scope,
        count: gap.no_result_count,
        runs: 1,
        topQueries: [...gap.top_queries],
      })
    }
  }

  return [...byScope.values()].sort((a, b) => b.count - a.count || a.scope.localeCompare(b.scope))
}
