import { Button } from '@/components/ui/button'
import { useLanguage } from '@/lib/i18n'

/**
 * FR/EN language toggle for the header (Mirador -> "Vigie" upgrade, P1a) —
 * mirrors `ThemeToggle`'s shape (a single icon-size button, aria-label
 * describes the NEXT action) so the two toggles read as one family in the
 * header, but shows the target language as text ("EN"/"FR") rather than an
 * icon — there's no unambiguous icon for "language", while a two-letter
 * code is immediately legible to a bilingual operator.
 */
export function LanguageToggle() {
  const { language, toggle, t } = useLanguage()
  const isFrench = language === 'fr'

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      onClick={toggle}
      aria-label={isFrench ? t('common.switchToEnglish') : t('common.switchToFrench')}
      title={isFrench ? t('common.switchToEnglish') : t('common.switchToFrench')}
      className="text-xs font-semibold uppercase"
    >
      {isFrench ? 'FR' : 'EN'}
    </Button>
  )
}
