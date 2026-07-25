import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MetricReadout } from '@/components/dashboard/MetricReadout'
import { Sparkline } from '@/components/dashboard/Sparkline'

// Note: this is a mount-only smoke test, not a computed-style assertion —
// vitest stubs CSS imports by default (`test.css` is false), so
// `getComputedStyle` can't observe the real `:root`/`.dark` cascade from
// `index.css` here. The actual token VALUES are exercised for real by the
// mandatory `npm run build` + `git diff --exit-code` reproducibility gate
// (Tailwind resolves `@theme`/`:root`/`.dark` at build time), and visually
// by the light/dark toggle in the running app. This test's job is narrower:
// prove every themed viz primitive mounts cleanly, with no runtime error,
// under BOTH the light (no `.dark` class) and dark (`.dark` class present)
// states `useTheme()`/`ThemeToggle` actually drive.
let container: HTMLDivElement
let root: Root

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  document.documentElement.classList.remove('dark')
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
  document.documentElement.classList.remove('dark')
})

describe('instrument theme — token remap smoke test', () => {
  it('mounts themed components without error in light mode (no .dark class)', () => {
    expect(document.documentElement.classList.contains('dark')).toBe(false)
    expect(() => {
      act(() => {
        root.render(
          <>
            <MetricReadout label="Total runs" value={42} tone="good" />
            <Sparkline points={[1, 4, 2, 8]} tone="crit" />
          </>,
        )
      })
    }).not.toThrow()
    expect(container.querySelector('[data-slot="metric-readout"]')).not.toBeNull()
    expect(container.querySelector('[data-slot="sparkline"]')).not.toBeNull()
  })

  it('mounts themed components without error in dark mode (.dark class present)', () => {
    document.documentElement.classList.add('dark')
    expect(() => {
      act(() => {
        root.render(
          <>
            <MetricReadout label="Total runs" value={42} tone="crit" />
            <Sparkline points={[1, 4, 2, 8]} tone="good" />
          </>,
        )
      })
    }).not.toThrow()
    expect(container.querySelector('[data-slot="metric-readout"]')).not.toBeNull()
    expect(container.querySelector('[data-slot="sparkline"]')).not.toBeNull()
  })

  it('re-uses the SAME token/class names in both themes (values differ via .dark, not class swapping)', () => {
    act(() => {
      root.render(<MetricReadout label="Total runs" value={42} tone="good" />)
    })
    const lightClasses = container.querySelector('[data-slot="metric-readout"]')?.className
    act(() => {
      root.unmount()
    })
    document.documentElement.classList.add('dark')
    root = createRoot(container)
    act(() => {
      root.render(<MetricReadout label="Total runs" value={42} tone="good" />)
    })
    const darkClasses = container.querySelector('[data-slot="metric-readout"]')?.className
    // Same component, same tone, same className output in both theme
    // states — the remap is a `:root`/`.dark` VALUE change, never a
    // component-level class swap.
    expect(lightClasses).toBe(darkClasses)
  })
})
