import { Phone, PhoneOff } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { canSpeak } from '@/lib/browser-speech'
import { useT } from '@/lib/i18n'
import { usePersistedState } from '@/lib/use-persisted-state'

export const VOICE_CALL_KEY = 'hivepilot.webui.voice-call'

/**
 * « Appeler un agent » (HP-62) — spoken replies on, ready for dictation.
 * Browser speechSynthesis; cloud TTS is an optional overlay when configured.
 */
export function CallToggle() {
  const t = useT()
  const [on, setOn] = usePersistedState(VOICE_CALL_KEY, false)

  if (!canSpeak()) return null

  return (
    <Button
      type="button"
      variant={on ? 'default' : 'outline'}
      size="icon-sm"
      className="touch-target"
      data-testid="voice-call"
      aria-pressed={on}
      aria-label={on ? t('voice.hangUp') : t('voice.call')}
      title={on ? t('voice.hangUp') : t('voice.call')}
      onClick={() => setOn((v) => !v)}
    >
      {on ? <Phone className="size-4" /> : <PhoneOff className="size-4" />}
    </Button>
  )
}

export function useVoiceCall(): boolean {
  const [on] = usePersistedState(VOICE_CALL_KEY, false)
  return on
}
