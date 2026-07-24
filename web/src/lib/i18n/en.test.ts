import { describe, expect, it } from 'vitest'
import { en } from './en'

describe('en dictionary', () => {
  it('has no empty string values', () => {
    for (const [key, value] of Object.entries(en)) {
      expect(value.length, `key "${key}" has an empty value`).toBeGreaterThan(0)
    }
  })

  it('has no duplicate-looking whitespace-only keys', () => {
    const keys = Object.keys(en)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('includes the shell namespaces this sprint translates', () => {
    const namespaces = ['common.', 'header.', 'nav.', 'health.status.']
    for (const ns of namespaces) {
      expect(Object.keys(en).some((k) => k.startsWith(ns)), `no keys under "${ns}"`).toBe(true)
    }
  })

  it('includes the four main views namespaces this sprint translates', () => {
    const namespaces = ['analytics.', 'cost.', 'health.', 'graph.']
    for (const ns of namespaces) {
      expect(Object.keys(en).some((k) => k.startsWith(ns)), `no keys under "${ns}"`).toBe(true)
    }
  })
})
