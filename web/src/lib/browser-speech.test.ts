import { afterEach, describe, expect, it, vi } from 'vitest'
import { canDictate, canSpeak, speak, speechRecognitionCtor, startDictation } from './browser-speech'

describe('browser-speech', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('reports dictation unavailable without a recognition ctor', () => {
    expect(canDictate()).toBe(false)
    expect(speechRecognitionCtor()).toBeNull()
  })

  it('starts a recognition session and forwards the transcript', () => {
    const start = vi.fn()
    const stop = vi.fn()
    class FakeRec {
      lang = ''
      interimResults = true
      continuous = true
      onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null =
        null
      onerror: ((event: { error?: string }) => void) | null = null
      onend: (() => void) | null = null
      start = start
      stop = stop
    }
    vi.stubGlobal('SpeechRecognition', FakeRec)
    const onTranscript = vi.fn()
    const onEnd = vi.fn()
    const rec = startDictation({ lang: 'fr-FR', onTranscript, onEnd })
    expect(rec).not.toBeNull()
    expect(start).toHaveBeenCalled()
    rec!.onresult?.({ results: [[{ transcript: '  bonjour  ' }]] })
    expect(onTranscript).toHaveBeenCalledWith('bonjour')
  })

  it('speaks via speechSynthesis when available', () => {
    const speakFn = vi.fn()
    const cancel = vi.fn()
    class FakeUtterance {
      lang = ''
      text: string
      constructor(text: string) {
        this.text = text
      }
    }
    vi.stubGlobal('speechSynthesis', { speak: speakFn, cancel })
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    expect(canSpeak()).toBe(true)
    speak('hello', 'en-US')
    expect(cancel).toHaveBeenCalled()
    expect(speakFn).toHaveBeenCalled()
  })
})
