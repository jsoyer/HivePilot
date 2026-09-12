import { describe, expect, it } from 'vitest'
import { LayoutGrid } from 'lucide-react'
import {
  buildNavGroups,
  FALLBACK_GROUP_LABEL,
  NAV_GROUP_ORDER,
  PLUS_NAV,
  PRIMARY_NAV,
  pickNavItems,
  type NavItem,
} from './nav-config'

function item(value: string): NavItem {
  return { value, label: value, Icon: LayoutGrid }
}

describe('PRIMARY_NAV / PLUS_NAV', () => {
  it('sidebar doors are Inbox, Approvals, Runs, Alerts — no duplicates', () => {
    expect(PRIMARY_NAV.map((entry) => entry.value)).toEqual(['inbox', 'approvals', 'runs', 'alerts'])
    const seen = new Set<string>()
    for (const entry of [...PRIMARY_NAV, ...PLUS_NAV]) {
      expect(seen.has(entry.value)).toBe(false)
      seen.add(entry.value)
    }
  })

  it('Plus lists Rooms, Orchestrator, Spend, Memory, System destinations', () => {
    expect(PLUS_NAV.map((entry) => entry.value)).toEqual([
      'spaces',
      'orchestrator',
      'cost',
      'memory',
      'health',
    ])
    expect(PLUS_NAV.map((entry) => entry.labelKey)).toEqual([
      'nav.rooms',
      'nav.orchestrator',
      'nav.spend',
      'nav.memory',
      'nav.system',
    ])
  })
})

describe('pickNavItems', () => {
  it('resolves specs in order and applies the caller label', () => {
    const items = [item('cost'), item('inbox'), item('memory')]
    const picked = pickNavItems(items, PRIMARY_NAV, (key) => key.toUpperCase())
    expect(picked.map((entry) => entry.value)).toEqual(['inbox'])
    expect(picked[0]?.label).toBe('NAV.INBOX')
  })
})

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
  it('never drops an item — every input item appears in exactly one output group', () => {
    const items = [
      item('inbox'),
      item('analytics'),
      item('cost'),
      item('health'),
      item('memory'),
      item('approvals'),
      item('runs'),
      item('alerts'),
      item('graph'),
    ]
    const groups = buildNavGroups(items)
    const outputValues = groups.flatMap((g) => g.items.map((i) => i.value)).sort()
    expect(outputValues).toEqual(items.map((i) => i.value).sort())
  })

  it('falls back items with no configured group into a trailing "Panels" group', () => {
    const items = [item('analytics'), item('panel-rtk-status'), item('panel-secure-panel')]
    const groups = buildNavGroups(items)
    const panelsGroup = groups.find((g) => g.label === FALLBACK_GROUP_LABEL)
    expect(panelsGroup).toBeDefined()
    expect(panelsGroup?.items.map((i) => i.value)).toEqual(['panel-rtk-status', 'panel-secure-panel'])
  })

  it('omits groups that have no items for the given input', () => {
    const items = [item('memory')]
    const groups = buildNavGroups(items)
    expect(groups).toHaveLength(1)
    expect(groups[0]?.items.map((i) => i.value)).toEqual(['memory'])
  })

  it('returns an empty array for an empty input', () => {
    expect(buildNavGroups([])).toEqual([])
  })

  it('keeps Inbox in the operate group so ⌘K still lists it with Approvals/Runs', () => {
    const items = [item('inbox'), item('runs'), item('approvals'), item('alerts')]
    const groups = buildNavGroups(items)
    expect(groups).toHaveLength(1)
    expect(groups[0]?.label).toBe('nav.operate')
    expect(groups[0]?.items.map((i) => i.value)).toEqual(['inbox', 'runs', 'approvals', 'alerts'])
  })
})
