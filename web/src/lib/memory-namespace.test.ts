import { describe, expect, it } from 'vitest'
import { groupGapsByScope, scopeOf, type MemoryGap } from './memory-namespace'

/**
 * Some memory namespaces carry the run id as their first segment, so every
 * run mints a new namespace and the same role can never accumulate.
 *
 * On real data that turned 4 gaps for `noxys-developer` into four
 * indistinguishable 8% slivers next to seven other 8% slivers — eleven
 * segments of identical size, which is exactly the shape that carries no
 * information at all.
 */
describe('scopeOf', () => {
  it('strips a leading run id', () => {
    expect(scopeOf('af10fde4-94d9-49fb-897f-b9b7f2423382:noxys-developer:developer')).toBe(
      'noxys-developer:developer',
    )
  })

  it('keeps a leading project segment', () => {
    // `noxys` is a project, not a run. Stripping by POSITION rather than by
    // shape would merge namespaces that are genuinely distinct.
    expect(scopeOf('noxys:noxys-cto-review:cto')).toBe('noxys:noxys-cto-review:cto')
  })

  it('leaves a namespace with no prefix alone', () => {
    expect(scopeOf('noxys:groomer-scan')).toBe('noxys:groomer-scan')
  })
})

describe('groupGapsByScope', () => {
  const runScoped = (id: string, count = 1): MemoryGap => ({
    namespace: `${id}:noxys-developer:developer`,
    no_result_count: count,
    top_queries: ['noxys-developer implementation'],
  })

  it('collapses the same role seen across several runs', () => {
    const grouped = groupGapsByScope([
      runScoped('af10fde4-94d9-49fb-897f-b9b7f2423382'),
      runScoped('02e8acdf-08aa-40ae-ba68-a370bba64624'),
      runScoped('df1beab0-a436-4cf7-99ed-ef28bebfbadc'),
      runScoped('209a6fd7-4eaf-4b9f-acb4-90eba8b6c7c4'),
    ])

    expect(grouped).toHaveLength(1)
    expect(grouped[0].scope).toBe('noxys-developer:developer')
    expect(grouped[0].count).toBe(4)
    expect(grouped[0].runs).toBe(4)
  })

  it('reports how many runs a scope spans', () => {
    // A gap that recurs run after run is a different problem from one that
    // happened once, and the count alone cannot tell them apart.
    const grouped = groupGapsByScope([
      runScoped('af10fde4-94d9-49fb-897f-b9b7f2423382', 3),
      { namespace: 'noxys:groomer-scan', no_result_count: 3, top_queries: [] },
    ])

    expect(grouped.find((g) => g.scope === 'noxys-developer:developer')?.runs).toBe(1)
    expect(grouped.find((g) => g.scope === 'noxys:groomer-scan')?.runs).toBe(1)
  })

  it('orders by gap count, most first', () => {
    const grouped = groupGapsByScope([
      { namespace: 'a:one', no_result_count: 1, top_queries: [] },
      { namespace: 'b:two', no_result_count: 5, top_queries: [] },
      { namespace: 'c:three', no_result_count: 3, top_queries: [] },
    ])

    expect(grouped.map((g) => g.count)).toEqual([5, 3, 1])
  })

  it('breaks ties on the name so the order is stable between renders', () => {
    // A list that reshuffles on every refresh is one nobody can read twice.
    const grouped = groupGapsByScope([
      { namespace: 'z:last', no_result_count: 2, top_queries: [] },
      { namespace: 'a:first', no_result_count: 2, top_queries: [] },
    ])

    expect(grouped.map((g) => g.scope)).toEqual(['a:first', 'z:last'])
  })

  it('merges queries without repeating them', () => {
    const grouped = groupGapsByScope([
      { namespace: 'x:1', no_result_count: 1, top_queries: ['same', 'one'] },
      { namespace: 'x:1', no_result_count: 1, top_queries: ['same', 'two'] },
    ])

    expect(grouped[0].topQueries).toEqual(['same', 'one', 'two'])
  })

  it('returns nothing for no gaps', () => {
    expect(groupGapsByScope([])).toEqual([])
  })
})
