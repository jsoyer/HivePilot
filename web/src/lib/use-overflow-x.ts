import { useEffect, useState, type RefObject } from 'react'

/**
 * How much hidden width counts as "actually scrollable".
 *
 * Sub-pixel layout rounding routinely makes `scrollWidth` exceed
 * `clientWidth` by a fraction on content that visually fits. Measured in a
 * real browser, Cost's by-project table overhangs by exactly 2px at 390px —
 * no perceptible content is cut, so turning it into a focusable, announced
 * scroll region would be pure noise for a screen-reader or keyboard user.
 * The tables that genuinely need the treatment overhang by 250-383px, so the
 * threshold is nowhere near them.
 */
const OVERFLOW_TOLERANCE_PX = 4

/**
 * Reports whether `ref`'s element currently has horizontally-scrollable
 * overflow, re-measuring on element resize and on window resize.
 *
 * Used by `ui/table.tsx` to decide, per render, whether a table container is
 * actually a scroll region. The mobile audit measured the wide metric tables
 * hiding 43-53% of their own content at 390px (Agents: 717px of table in a
 * 334px box) with no affordance and no keyboard route to the hidden columns.
 * Fixing that means marking the container as a focusable, labelled scroll
 * region — but doing so UNCONDITIONALLY would add a useless tab stop to every
 * table on every desktop, where nothing overflows. Hence the measurement.
 *
 * Falls back to a window-resize listener when `ResizeObserver` is missing
 * (jsdom, older engines), so the hook degrades rather than throwing.
 */
export function useOverflowX(ref: RefObject<HTMLElement | null>): boolean {
  const [overflowing, setOverflowing] = useState(false)

  useEffect(() => {
    const element = ref.current
    if (!element) return

    function measure() {
      const el = ref.current
      if (!el) return
      setOverflowing(el.scrollWidth - el.clientWidth > OVERFLOW_TOLERANCE_PX)
    }

    measure()

    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure)
      return () => window.removeEventListener('resize', measure)
    }

    const observer = new ResizeObserver(measure)
    observer.observe(element)
    // The container can stay the same size while its CONTENT changes width
    // (a new row with a longer model name), so observe the row box too.
    if (element.firstElementChild) observer.observe(element.firstElementChild)
    window.addEventListener('resize', measure)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [ref])

  return overflowing
}
