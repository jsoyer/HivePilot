/**
 * Spoken concierge replies (HP-62). Prefers the operator's BYO cloud TTS
 * (`POST /v1/voice/tts`) when configured; otherwise uses the browser
 * speechSynthesis engine so a stock install needs no keys.
 */

import { speak } from './browser-speech'
import { fetchVoiceConfig, synthesizeSpeech } from './pollen-api'

export async function speakReply(text: string, lang: string): Promise<void> {
  const body = text.trim()
  if (!body) return
  try {
    const cfg = await fetchVoiceConfig()
    if (cfg.cloud_tts) {
      const blob = await synthesizeSpeech(body)
      await playBlob(blob)
      return
    }
  } catch {
    // 403/404/network — fall back to the browser engine.
  }
  speak(body, lang)
}

export function playBlob(blob: Blob): Promise<void> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob)
    const audio = new Audio(url)
    const cleanup = () => URL.revokeObjectURL(url)
    audio.onended = () => {
      cleanup()
      resolve()
    }
    audio.onerror = () => {
      cleanup()
      reject(new Error('audio play failed'))
    }
    void audio.play().then(undefined, (err: unknown) => {
      cleanup()
      reject(err)
    })
  })
}
