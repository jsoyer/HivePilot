/**
 * Lightweight custom i18n mechanism (Mirador -> "Vigie" upgrade, P1a) —
 * a React context + a `t(key, params?)` lookup function over two bundled TS
 * dictionaries (`./en.ts` / `./fr.ts`, no network/locale files). English is
 * the default AND the fallback for any key missing from a non-English
 * dictionary (see `t()` below).
 *
 * Persistence reuses `usePersistedState` (same `hivepilot.webui.*`
 * localStorage convention as `SidebarNav`'s collapse state) rather than
 * `useTheme`'s bespoke localStorage handling — one fewer pattern to
 * maintain.
 *
 * Follows `role-context.tsx`'s convention: a context with a sane DEFAULT
 * value (English, key-fallback `t()`, no-op `toggle`/`setLanguage`) rather
 * than throwing when used outside a provider — a stray `useT()` call must
 * never crash the shell.
 */
import { createContext, type ReactNode, useCallback, useContext, useMemo } from 'react'
import { usePersistedState } from '@/lib/use-persisted-state'
import { en } from './en'
import { fr } from './fr'

export type { TranslationKey } from './en'

/** Supported UI languages. English is the default and the fallback for any
 * key missing from a non-English dictionary. */
export type Language = 'en' | 'fr'

/** Interpolation params for a translated string's `{name}` placeholders --
 * e.g. `t('common.lastDays', { days: 30 })` against the template
 * `"Last {days} days"`. */
export type TranslationParams = Record<string, string | number>

/** A lookup function: given a dictionary key (dot-namespaced, e.g.
 * `'nav.overview'`) and optional interpolation params, returns the
 * translated string for the CURRENT language -- falling back to English,
 * then to the key itself, if the key is missing from a dictionary. */
export type TFunction = (key: string, params?: TranslationParams) => string

export interface LanguageContextValue {
  language: Language
  setLanguage: (language: Language) => void
  /** Flips between 'en' and 'fr'. */
  toggle: () => void
  t: TFunction
}

export const LANG_STORAGE_KEY = 'hivepilot.webui.lang'

const DICTIONARIES: Record<Language, Record<string, string>> = { en, fr }

/** Replaces every `{name}` placeholder in `template` with `params[name]`,
 * stringified — a placeholder with no matching param is left untouched
 * (never throws over a caller forgetting a param). */
function interpolate(template: string, params?: TranslationParams): string {
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    Object.hasOwn(params, name) ? String(params[name]) : match,
  )
}

/** Builds a `t()` bound to `language` — English fallback for a key missing
 * from a non-English dictionary, then the raw key itself if even English is
 * missing it (never throws, never renders `undefined`). */
function makeT(language: Language): TFunction {
  return (key, params) => {
    const dict = DICTIONARIES[language]
    const template = dict[key] ?? en[key as keyof typeof en] ?? key
    return interpolate(template, params)
  }
}

const DEFAULT_CONTEXT: LanguageContextValue = {
  language: 'en',
  setLanguage: () => {},
  toggle: () => {},
  t: makeT('en'),
}

const LanguageContext = createContext<LanguageContextValue>(DEFAULT_CONTEXT)

interface LanguageProviderProps {
  children: ReactNode
}

export function LanguageProvider({ children }: LanguageProviderProps) {
  const [language, setLanguage] = usePersistedState<Language>(LANG_STORAGE_KEY, 'en')

  const toggle = useCallback(() => {
    setLanguage((prev) => (prev === 'en' ? 'fr' : 'en'))
  }, [setLanguage])

  const value = useMemo<LanguageContextValue>(
    () => ({ language, setLanguage, toggle, t: makeT(language) }),
    [language, setLanguage, toggle],
  )

  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>
}

export function useLanguage(): LanguageContextValue {
  return useContext(LanguageContext)
}

export function useT(): TFunction {
  return useLanguage().t
}
