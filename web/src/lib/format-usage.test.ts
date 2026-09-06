import { describe, expect, it } from 'vitest'
import {
  formatCompactCount,
  formatCostUsd,
  formatGibPair,
  formatPct,
  inferProviderFromModel,
} from './format-usage'

describe('formatCompactCount', () => {
  it('uses B / M / k in English', () => {
    expect(formatCompactCount(8_200_000_000, 'en')).toBe('8.2B')
    expect(formatCompactCount(502_000_000, 'en')).toBe('502M')
    expect(formatCompactCount(88_000, 'en')).toBe('88k')
    expect(formatCompactCount(42, 'en')).toBe('42')
  })

  it('uses Md / M / k in French with a comma decimal', () => {
    expect(formatCompactCount(8_200_000_000, 'fr')).toBe('8,2 Md')
    expect(formatCompactCount(502_000_000, 'fr')).toBe('502 M')
    expect(formatCompactCount(88_000, 'fr')).toBe('88k')
  })
})

describe('formatCostUsd', () => {
  it('keeps three decimal places like the rest of Pollen', () => {
    expect(formatCostUsd(1586.54)).toBe('$1586.540')
    expect(formatCostUsd(0.07)).toBe('$0.070')
  })
})

describe('formatGibPair / formatPct', () => {
  it('renders used/total in GB or Go', () => {
    const used = 11 * 1024 ** 3
    const total = 16 * 1024 ** 3
    expect(formatGibPair(used, total, 'en')).toBe('11/16 GB')
    expect(formatGibPair(used, total, 'fr')).toBe('11/16 Go')
  })

  it('rounds a percent', () => {
    expect(formatPct(46.2)).toBe('46%')
  })
})

describe('inferProviderFromModel', () => {
  it('maps known slugs', () => {
    expect(inferProviderFromModel('claude-opus-5')).toBe('anthropic')
    expect(inferProviderFromModel('gpt-5.6-sol')).toBe('openai')
    expect(inferProviderFromModel('gemini-2.5-pro')).toBe('google')
    expect(inferProviderFromModel('mistral-large')).toBe('mistral')
    expect(inferProviderFromModel('mystery-box')).toBe('unknown')
  })
})
