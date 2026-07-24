import { describe, expect, it } from 'vitest'
import { LayoutGrid } from 'lucide-react'
import { buildNavGroups, FALLBACK_GROUP_LABEL, NAV_GROUP_ORDER, type NavItem } from './nav-config'

function item(value: string): NavItem {
  return { value, label: value, Icon: LayoutGrid }
}

describe('NAV_GROUP_ORDER', () => {
  it('has no duplicate values across groups', () => {
    const seen = new Set<string>()
    for (const group of NAV_GROUP_ORDER) {
      for (const value of group.values) {
        expect(seen.has(value)).toBe(false)
        seen.add(value)
      }
    }
  })
})

describe('buildNavGroups', () => {
  it('places items into their configured group, in NAV_GROUP_ORDER order', () => {
    const items = [item('analytics'), item('cost'), item('health'), item('mem0')]
    const groups = buildNavGroups(items)

    const labels = groups.map((g) => g.label)
    expect(labels).toEqual(NAV_GROUP_ORDER.filter((g) => g.values.some((v) => items.some((i) => i.value === v))).map((g) => g.label))
  })

  it('never drops an item — every input item appears in exactly one output group', () => {
    const items = [item('analytics'), item('cost'), item('health'), item('mem0'), item('approvals'), item('runs'), item('graph')]
    const groups = buildNavGroups(items)
    const outputValues = groups.flatMap((g) => g.items.map((i) => i.value)).sort()
    expect(outputValues).toEqual(items.map((i) => i.value).sort())
  })

  it('falls back items with no configured group into a trailing "Panels" group (e.g. dynamic plugin panels)', () => {
    const items = [item('analytics'), item('panel-rtk-status'), item('panel-secure-panel')]
    const groups = buildNavGroups(items)

    const panelsGroup = groups.find((g) => g.label === FALLBACK_GROUP_LABEL)
    expect(panelsGroup).toBeDefined()
    expect(panelsGroup?.items.map((i) => i.value)).toEqual(['panel-rtk-status', 'panel-secure-panel'])
  })

  it('omits groups that have no items for the given input', () => {
    const items = [item('mem0')]
    const groups = buildNavGroups(items)
    expect(groups).toHaveLength(1)
    expect(groups[0]?.items.map((i) => i.value)).toEqual(['mem0'])
  })

  it('returns an empty array for an empty input', () => {
    expect(buildNavGroups([])).toEqual([])
  })
})
