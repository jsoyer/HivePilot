import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { StatusGlyph } from './StatusGlyph'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
})

function render(status: string, label?: string) {
  act(() => root.render(<StatusGlyph status={status} label={label} />))
  return container.querySelector('[data-testid="status-glyph"]') as SVGElement
}

describe('StatusGlyph', () => {
  it.each([
    ['running', 'working'],
    ['failed', 'needs_you'],
    ['approval', 'needs_you'],
    ['review', 'in_review'],
    ['success', 'ready'],
    ['new', 'queued'],
    ['cancelled', 'other'],
  ])('renders the %s status in the %s zone', (status, zone) => {
    const glyph = render(status)
    expect(glyph).not.toBeNull()
    expect(glyph.getAttribute('data-zone')).toBe(zone)
  })

  it('spins while working and pulses when it needs you', () => {
    expect(render('running').getAttribute('class')).toMatch(/animate-spin/)
    expect(render('failed').getAttribute('class')).toMatch(/animate-pulse/)
  })

  it('is decorative by default (adjacent text is the accessible source)', () => {
    const glyph = render('running')
    expect(glyph.getAttribute('aria-hidden')).toBe('true')
    expect(glyph.getAttribute('role')).toBeNull()
  })

  it('is announced as an img when given a label', () => {
    const glyph = render('failed', 'Failed')
    expect(glyph.getAttribute('role')).toBe('img')
    expect(glyph.getAttribute('aria-label')).toBe('Failed')
    expect(glyph.getAttribute('aria-hidden')).toBeNull()
  })
})
