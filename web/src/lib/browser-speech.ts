/**
 * Browser STT/TTS (HP-62). The default path needs no API key — Web Speech
 * Recognition + speechSynthesis. Cloud providers are an optional overlay
 * via `POST /v1/voice/tts` when the operator set a BYO key.
 */

export type SpeechRecognitionLike = {
  lang: string
  interimResults: boolean
  continuous: boolean
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null
  onerror: ((event: { error?: string }) => void) | null
  onend: (() => void) | null
  start: () => void
  stop: () => void
}

type RecognitionCtor = new () => SpeechRecognitionLike

export function speechRecognitionCtor(): RecognitionCtor | null {
  const w = window as Window & {
    SpeechRecognition?: RecognitionCtor
    webkitSpeechRecognition?: RecognitionCtor
  }
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null
}

export function canDictate(): boolean {
  return speechRecognitionCtor() !== null
}

export function canSpeak(): boolean {
  return typeof window !== 'undefined' && 'speechSynthesis' in window
}

export function startDictation(opts: {
  lang: string
  onTranscript: (text: string) => void
  onEnd: () => void
}): SpeechRecognitionLike | null {
  const Ctor = speechRecognitionCtor()
  if (!Ctor) return null
  const rec = new Ctor()
  rec.lang = opts.lang
  rec.interimResults = false
  rec.continuous = false
  rec.onresult = (event) => {
    const last = event.results[event.results.length - 1]
    const text = last?.[0]?.transcript?.trim()
    if (text) opts.onTranscript(text)
  }
  rec.onerror = () => opts.onEnd()
  rec.onend = () => opts.onEnd()
  rec.start()
  return rec
}

export function speak(text: string, lang: string): void {
  if (!canSpeak() || !text.trim()) return
  const Utterance = (
    window as Window & { SpeechSynthesisUtterance?: typeof SpeechSynthesisUtterance }
  ).SpeechSynthesisUtterance
  if (!Utterance) return
  window.speechSynthesis.cancel()
  const utterance = new Utterance(text)
  utterance.lang = lang
  window.speechSynthesis.speak(utterance)
}

export function stopSpeaking(): void {
  if (canSpeak()) window.speechSynthesis.cancel()
}
