import { Mic, MicOff } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { canDictate, startDictation, type SpeechRecognitionLike } from '@/lib/browser-speech'
import { useLanguage, useT } from '@/lib/i18n'

/**
 * Dictation button for a composer (HP-62). Uses the browser Speech
 * Recognition API — no cloud key. Hidden when the engine is missing.
 */
export function ComposerMic({
  onTranscript,
  disabled = false,
}: {
  onTranscript: (text: string) => void
  disabled?: boolean
}) {
  const t = useT()
  const { language } = useLanguage()
  const [listening, setListening] = useState(false)
  const recRef = useRef<SpeechRecognitionLike | null>(null)

  useEffect(() => {
    return () => {
      recRef.current?.stop()
      recRef.current = null
    }
  }, [])

  if (!canDictate()) return null

  function toggle() {
    if (disabled) return
    if (listening) {
      recRef.current?.stop()
      recRef.current = null
      setListening(false)
      return
    }
    const rec = startDictation({
      lang: language === 'fr' ? 'fr-FR' : 'en-US',
      onTranscript,
      onEnd: () => {
        recRef.current = null
        setListening(false)
      },
    })
    if (!rec) return
    recRef.current = rec
    setListening(true)
  }

  return (
    <Button
      type="button"
      variant={listening ? 'default' : 'outline'}
      size="icon-sm"
      className="touch-target"
      data-testid="composer-mic"
      disabled={disabled}
      aria-pressed={listening}
      aria-label={listening ? t('voice.stopDictation') : t('voice.dictate')}
      title={listening ? t('voice.stopDictation') : t('voice.dictate')}
      onClick={toggle}
    >
      {listening ? <MicOff className="size-4" /> : <Mic className="size-4" />}
    </Button>
  )
}
