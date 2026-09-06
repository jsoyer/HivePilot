import { useEffect, useRef } from 'react'
import { getToken } from './api'
import { type ChangeEvent, streamEvents } from './event-stream'

/**
 * Subscribe to the realtime change bus (`GET /v1/events/stream`, HP-41) for the
 * lifetime of the calling component. `onEvent` fires for every change; the hook
 * owns connection lifecycle, automatic resume via `Last-Event-ID` (so a
 * reconnect replays only what was missed), and capped exponential backoff on a
 * dropped connection. Aborts cleanly on unmount or when `enabled` goes false.
 *
 * `onEvent` is held in a ref, so a caller passing a fresh closure every render
 * never re-opens the stream — only `enabled` does.
 */
export function useEventStream(
  onEvent: (event: ChangeEvent) => void,
  opts: { enabled?: boolean } = {},
): void {
  const enabled = opts.enabled ?? true
  const onEventRef = useRef(onEvent)
  onEventRef.current = onEvent

  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    let stopped = false
    let lastEventId: string | null = null
    let backoff = 1000

    async function loop() {
      while (!stopped) {
        try {
          await streamEvents({
            signal: controller.signal,
            getToken,
            lastEventId,
            onEvent: (event, rawId) => {
              if (rawId) lastEventId = rawId
              onEventRef.current(event)
            },
          })
          backoff = 1000 // clean end (idle recycle) — reconnect promptly
        } catch {
          if (stopped || controller.signal.aborted) return
        }
        if (stopped) return
        await new Promise((resolve) => setTimeout(resolve, backoff))
        backoff = Math.min(backoff * 2, 15000)
      }
    }

    void loop()
    return () => {
      stopped = true
      controller.abort()
    }
  }, [enabled])
}
