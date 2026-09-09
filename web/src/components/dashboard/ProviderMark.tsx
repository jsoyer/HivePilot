import { inferProviderFromModel } from '@/lib/format-usage'
import { cn } from '@/lib/utils'

const MARK: Record<string, { bg: string; fg: string; letter: string }> = {
  anthropic: { bg: '#c15f3c', fg: '#fff', letter: 'A' },
  openai: { bg: '#111111', fg: '#fff', letter: 'O' },
  google: { bg: '#1a73e8', fg: '#fff', letter: 'G' },
  mistral: { bg: '#fa520f', fg: '#fff', letter: 'M' },
  meta: { bg: '#0668e1', fg: '#fff', letter: 'L' },
  deepseek: { bg: '#4d6bfe', fg: '#fff', letter: 'D' },
  xai: { bg: '#1a1a1a', fg: '#fff', letter: 'X' },
  nous: { bg: '#5b21b6', fg: '#fff', letter: 'N' },
  unknown: { bg: 'var(--muted)', fg: 'var(--muted-foreground)', letter: '?' },
}

export function providerKey(name: string): string {
  const raw = name.trim().toLowerCase()
  if (raw in MARK) return raw
  return inferProviderFromModel(name)
}

export function ProviderMark({
  name,
  className,
}: {
  name: string
  className?: string
}) {
  const key = providerKey(name)
  const mark = MARK[key] ?? MARK.unknown
  return (
    <span
      aria-hidden="true"
      data-testid={`provider-mark-${key}`}
      className={cn(
        'inline-flex size-6 shrink-0 items-center justify-center rounded-md text-[11px] font-semibold',
        className,
      )}
      style={{ backgroundColor: mark.bg, color: mark.fg }}
    >
      {mark.letter}
    </span>
  )
}
