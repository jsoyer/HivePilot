import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LanguageProvider } from '@/lib/i18n'

class FakeRec {
  lang = ''
  interimResults = false
  continuous = false
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null =
    null
  onerror: ((event: { error?: string }) => void) | null = null
  onend: (() => void) | null = null
  start = vi.fn()
  stop = vi.fn()
}

const { FakeRec: Rec } = { FakeRec }

vi.stubGlobal('SpeechRecognition', Rec)

import { ComposerMic } from './ComposerMic'

let container: HTMLDivElement
let root: Root

describe('ComposerMic', () => {
  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => root.unmount())
    container.remove()
  })

  it('appends a transcript to the composer', async () => {
    const onTranscript = vi.fn()
    await act(async () => {
      root.render(
        <LanguageProvider>
          <ComposerMic onTranscript={onTranscript} />
        </LanguageProvider>,
      )
      await Promise.resolve()
    })
    const btn = container.querySelector('[data-testid="composer-mic"]') as HTMLButtonElement
    expect(btn).not.toBeNull()
    await act(async () => {
      btn.click()
      await Promise.resolve()
    })
    expect(btn.getAttribute('aria-pressed')).toBe('true')
  })
})
