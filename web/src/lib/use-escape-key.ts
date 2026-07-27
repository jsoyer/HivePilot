import { useEffect, useRef } from 'react'

/**
 * Close-on-Escape for any dismissible overlay.
 *
 * Extracted from `ui/drawer.tsx`, which already had exactly this effect, so
 * the mobile navigation drawer in `nav/SidebarNav.tsx` could get the same
 * behavior without a second hand-rolled copy of it. The mobile audit found
 * the nav drawer was the one overlay in Pollen that Escape did NOT close —
 * it could only be dismissed by tapping the backdrop or a nav item, which is
 * undiscoverable and unusable by keyboard.
 *
 * `enabled` matters: a CLOSED overlay must not listen, or it would swallow
 * Escape from whatever else is on screen (the ⌘K command palette, a native
 * `<select>` popup, the browser's own find bar).
 *
 * The handler is held in a ref so callers can pass an inline arrow — the
 * listener is bound once per `enabled` flip rather than being torn down and
 * re-added on every render.
 */
export function useEscapeKey(enabled: boolean, onEscape: () => void): void {
  const handlerRef = useRef(onEscape)
  handlerRef.current = onEscape

  useEffect(() => {
    if (!enabled) return
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') handlerRef.current()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [enabled])
}
