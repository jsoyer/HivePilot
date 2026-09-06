import { afterEach, describe, expect, it, vi } from 'vitest'

const { fetchVoiceConfig, synthesizeSpeech } = vi.hoisted(() => ({
  fetchVoiceConfig: vi.fn(),
  synthesizeSpeech: vi.fn(),
}))

vi.mock('./pollen-api', () => ({ fetchVoiceConfig, synthesizeSpeech }))

import { speakReply } from './voice-reply'

describe('speakReply', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    fetchVoiceConfig.mockReset()
    synthesizeSpeech.mockReset()
  })

  it('falls back to speechSynthesis when cloud TTS is off', async () => {
    const speakFn = vi.fn()
    class FakeUtterance {
      lang = ''
      text: string
      constructor(text: string) {
        this.text = text
      }
    }
    vi.stubGlobal('speechSynthesis', { speak: speakFn, cancel: vi.fn() })
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    fetchVoiceConfig.mockResolvedValue({
      stt: 'browser',
      tts: 'browser',
      cloud_tts: false,
      cloud_stt: false,
    })
    await speakReply('hello', 'en-US')
    expect(synthesizeSpeech).not.toHaveBeenCalled()
    expect(speakFn).toHaveBeenCalled()
  })

  it('plays the proxied MPEG when cloud TTS is on', async () => {
    class FakeAudio {
      onended: (() => void) | null = null
      onerror: (() => void) | null = null
      play = vi.fn().mockImplementation(() => {
        queueMicrotask(() => this.onended?.())
        return Promise.resolve()
      })
    }
    vi.stubGlobal('Audio', FakeAudio)
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:tts')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    fetchVoiceConfig.mockResolvedValue({
      stt: 'browser',
      tts: 'openai',
      cloud_tts: true,
      cloud_stt: false,
    })
    synthesizeSpeech.mockResolvedValue(new Blob([new Uint8Array([1, 2, 3])], { type: 'audio/mpeg' }))
    await speakReply('bonjour', 'fr-FR')
    expect(synthesizeSpeech).toHaveBeenCalledWith('bonjour')
  })

  it('falls back to the browser engine when the proxy is unavailable', async () => {
    const speakFn = vi.fn()
    class FakeUtterance {
      lang = ''
      text: string
      constructor(text: string) {
        this.text = text
      }
    }
    vi.stubGlobal('speechSynthesis', { speak: speakFn, cancel: vi.fn() })
    vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
    fetchVoiceConfig.mockRejectedValue(new Error('offline'))
    await speakReply('hello', 'en-US')
    expect(speakFn).toHaveBeenCalled()
  })
})
