import { describe, expect, it } from 'vitest'
import { en } from './en'
import { fr } from './fr'

describe('fr dictionary', () => {
  it('has exactly the same key set as the en dictionary (1:1 parity)', () => {
    expect(Object.keys(fr).sort()).toEqual(Object.keys(en).sort())
  })

  it('has no empty string values', () => {
    for (const [key, value] of Object.entries(fr)) {
      expect(value.length, `key "${key}" has an empty value`).toBeGreaterThan(0)
    }
  })

  it('translates a representative sample of shell + view keys to French', () => {
    expect(fr['nav.overview']).toBe("Vue d'ensemble")
    expect(fr['nav.system']).toBe('Système')
    expect(fr['nav.memory']).toBe('Mémoire')
    expect(fr['analytics.totalRuns']).not.toBe(en['analytics.totalRuns'])
    expect(fr['cost.totalCost']).not.toBe(en['cost.totalCost'])
    expect(fr['health.title']).not.toBe(en['health.title'])
    expect(fr['graph.title']).not.toBe(en['graph.title'])
  })

  it('preserves {param} placeholders exactly where en has them', () => {
    for (const [key, value] of Object.entries(en)) {
      const enParams = value.match(/\{(\w+)\}/g) ?? []
      const frParams = fr[key as keyof typeof fr].match(/\{(\w+)\}/g) ?? []
      expect(frParams.sort(), `placeholder mismatch for "${key}"`).toEqual(enParams.sort())
    }
  })
})
