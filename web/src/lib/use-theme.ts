import { useCallback, useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'hivepilot.webui.theme'

function readStoredTheme(): Theme | null {
  try {
    const raw = window.localStorage.getItem(THEME_STORAGE_KEY)
    return raw === 'light' || raw === 'dark' ? raw : null
  } catch {
    return null
  }
}

function applyThemeClass(theme: Theme): void {
  document.documentElement.classList.toggle('dark', theme === 'dark')
}

export interface UseThemeResult {
  theme: Theme
  toggle: () => void
}

/**
 * Light/dark theme state for the Mirador shell (P0b). Reuses the app's
 * EXISTING theming mechanism — the `.dark` class on `<html>` that
 * `src/index.css`'s `@custom-variant dark (&:is(.dark *))` already keys off
 * (see `index.html`, which hardcodes `class="dark"` as the default
 * aesthetic) — no new theming library.
 *
 * Initial value precedence: persisted `localStorage` choice > whatever class
 * is already on `<html>` right now (so a first render never fights the
 * class `index.html` painted before React mounted). Every change is both
 * applied to the DOM and persisted, so a reload keeps the caller's choice.
 */
export function useTheme(): UseThemeResult {
  const [theme, setTheme] = useState<Theme>(() => {
    const stored = readStoredTheme()
    if (stored) return stored
    return document.documentElement.classList.contains('dark') ? 'dark' : 'light'
  })

  useEffect(() => {
    applyThemeClass(theme)
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, theme)
    } catch {
      // Storage unavailable — theme still applies for this session.
    }
  }, [theme])

  const toggle = useCallback(() => {
    setTheme((prev) => (prev === 'dark' ? 'light' : 'dark'))
  }, [])

  return { theme, toggle }
}
