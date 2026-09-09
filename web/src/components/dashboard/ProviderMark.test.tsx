import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it } from 'vitest'
import { ProviderMark, providerKey } from './ProviderMark'

describe('providerKey', () => {
  it('keeps an explicit provider slug', () => {
    expect(providerKey('anthropic')).toBe('anthropic')
    expect(providerKey('openai')).toBe('openai')
  })

  it('infers from a model name', () => {
    expect(providerKey('claude-opus-5')).toBe('anthropic')
    expect(providerKey('gpt-5.6-sol')).toBe('openai')
  })
})

describe('ProviderMark', () => {
  afterEach(() => {
    document.body.replaceChildren()
  })

  it('renders a letter mark for the inferred provider', () => {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    act(() => {
      root.render(<ProviderMark name="claude-sonnet-5" />)
    })
    expect(container.querySelector('[data-testid="provider-mark-anthropic"]')?.textContent).toBe('A')
    act(() => {
      root.unmount()
    })
  })
})
