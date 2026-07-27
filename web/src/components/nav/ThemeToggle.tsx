import { Moon, Sun } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useTheme } from '@/lib/use-theme'

/**
 * Light/dark toggle for the header (P0b). Thin UI wrapper around
 * `useTheme()` — see that hook for the actual mechanism (the existing
 * `.dark` class on `<html>`, no new theming lib).
 */
export function ThemeToggle() {
  const { theme, toggle } = useTheme()
  const isDark = theme === 'dark'

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      // `icon-sm` is 28x28 — under the comfortable touch target. `touch-target`
      // expands only the hit area, and only on a coarse pointer (see index.css).
      className="touch-target"
      onClick={toggle}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
    >
      {isDark ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </Button>
  )
}
