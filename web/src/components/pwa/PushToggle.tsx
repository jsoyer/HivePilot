import { Bell, BellOff } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { useT } from '@/lib/i18n'
import { fetchPushConfig, subscribePush, unsubscribePush } from '@/lib/pollen-api'
import { useAsyncData } from '@/lib/use-async-data'
import { usePersistedState } from '@/lib/use-persisted-state'

const ENDPOINT_KEY = 'hivepilot.webui.push-endpoint'

function urlBase64ToUint8Array(base64: string): BufferSource {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4)
  const raw = atob(base64.replace(/-/g, '+').replace(/_/g, '/') + padding)
  const out = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i)
  return out
}

/**
 * Opt-in Web Push (HP-63). Hidden when the operator has not set VAPID keys
 * (`GET /v1/push/config` → `enabled: false`) or the browser has no SW.
 */
export function PushToggle() {
  const t = useT()
  const config = useAsyncData(() => fetchPushConfig(), [])
  const [endpoint, setEndpoint] = usePersistedState<string | null>(ENDPOINT_KEY, null)
  const [busy, setBusy] = useState(false)

  const enabled = config.status === 'success' && config.data.enabled && Boolean(config.data.vapid_public_key)
  const vapidKey = config.status === 'success' ? config.data.vapid_public_key : null

  useEffect(() => {
    if (!enabled || !('serviceWorker' in navigator)) return
    void navigator.serviceWorker.ready.then(async (reg) => {
      const sub = await reg.pushManager.getSubscription()
      if (sub) setEndpoint(sub.endpoint)
    })
  }, [enabled, setEndpoint])

  if (!enabled || vapidKey === null) return null
  if (typeof window === 'undefined' || !('serviceWorker' in navigator) || !('PushManager' in window)) {
    return null
  }

  const subscribed = Boolean(endpoint)
  const applicationServerKey = vapidKey

  async function toggle() {
    if (busy) return
    setBusy(true)
    try {
      const reg = await navigator.serviceWorker.ready
      if (subscribed && endpoint) {
        const sub = await reg.pushManager.getSubscription()
        await sub?.unsubscribe()
        await unsubscribePush(endpoint)
        setEndpoint(null)
        return
      }
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(applicationServerKey),
      })
      const json = sub.toJSON()
      const p256dh = json.keys?.p256dh
      const auth = json.keys?.auth
      if (!p256dh || !auth) throw new Error('incomplete push subscription')
      await subscribePush({ endpoint: sub.endpoint, keys: { p256dh, auth } })
      setEndpoint(sub.endpoint)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      className="touch-target"
      data-testid="pwa-push"
      disabled={busy}
      onClick={() => void toggle()}
      aria-label={subscribed ? t('pwa.pushEnabled') : t('pwa.pushEnable')}
      title={subscribed ? t('pwa.pushEnabled') : t('pwa.pushEnable')}
    >
      {subscribed ? <Bell className="size-4" /> : <BellOff className="size-4" />}
    </Button>
  )
}
