/**
 * Compact usage formatters for Pollen spend lists (Paul-style density).
 *
 * Tokens: k / M / B (en) or k / M / Md (fr). Costs stay USD envelopes —
 * the same `steps.cost_usd` figures the rest of Pollen already shows.
 * Provider marks are inferred from the model slug when the API row has
 * no `provider` field (`GET /v1/models` today).
 */

export type UsageLocale = 'en' | 'fr'

export function formatCompactCount(n: number, locale: UsageLocale = 'en'): string {
  const abs = Math.abs(n)
  const sign = n < 0 ? '-' : ''
  const fmt = (value: number, digits: number): string => {
    const trimmed = value.toFixed(digits).replace(/\.0$/, '')
    return locale === 'fr' ? trimmed.replace('.', ',') : trimmed
  }
  if (abs >= 1_000_000_000) {
    return `${sign}${fmt(abs / 1_000_000_000, 1)}${locale === 'fr' ? ' Md' : 'B'}`
  }
  if (abs >= 1_000_000) {
    return `${sign}${fmt(abs / 1_000_000, 1)}${locale === 'fr' ? ' M' : 'M'}`
  }
  if (abs >= 1_000) {
    return `${sign}${fmt(abs / 1_000, 1)}k`
  }
  return `${sign}${Math.round(abs).toLocaleString(locale === 'fr' ? 'fr-FR' : 'en-US')}`
}

export function formatCostUsd(n: number): string {
  return `$${n.toFixed(3)}`
}

const GIB = 1024 ** 3

export function formatGibPair(usedBytes: number, totalBytes: number, locale: UsageLocale = 'en'): string {
  const used = usedBytes / GIB
  const total = totalBytes / GIB
  const usedStr = used >= 10 ? used.toFixed(0) : used.toFixed(1)
  const totalStr = total >= 10 ? total.toFixed(0) : total.toFixed(1)
  const unit = locale === 'fr' ? 'Go' : 'GB'
  return `${usedStr}/${totalStr} ${unit}`
}

export function formatPct(n: number): string {
  return `${Math.round(n)}%`
}

export function inferProviderFromModel(model: string): string {
  const m = model.trim().toLowerCase()
  if (!m) return 'unknown'
  if (m.includes('claude') || m.includes('anthropic')) return 'anthropic'
  if (m.startsWith('gpt') || m.includes('openai') || m.includes('codex') || /^o[1-9]/.test(m)) {
    return 'openai'
  }
  if (m.includes('gemini') || m.includes('gemma')) return 'google'
  if (m.includes('mistral') || m.includes('mixtral') || m.includes('codestral') || m.includes('pixtral')) {
    return 'mistral'
  }
  if (m.includes('llama') || m.includes('meta-')) return 'meta'
  if (m.includes('deepseek')) return 'deepseek'
  if (m.includes('grok')) return 'xai'
  if (m.includes('hermes') || m.includes('nous')) return 'nous'
  return 'unknown'
}
