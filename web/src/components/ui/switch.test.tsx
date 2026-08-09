import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { Switch } from './switch'

/**
 * A switch is the one control on the plugins page that changes the system.
 * The assertions here are all about it being a real control rather than a
 * styled div: reachable by keyboard, announcing its state, and refusing the
 * click when the caller says it must.
 */
describe('Switch', () => {
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

  function mount(node: React.ReactNode) {
    act(() => root.render(node))
    return container.querySelector('button[role="switch"]') as HTMLButtonElement
  }

  it('announces its state through aria-checked', () => {
    // Colour alone would leave the state invisible to a screen reader and to
    // anyone reading a greyscale screenshot.
    const on = mount(<Switch checked onCheckedChange={() => {}} aria-label="rtk" />)
    expect(on.getAttribute('aria-checked')).toBe('true')

    const off = mount(<Switch checked={false} onCheckedChange={() => {}} aria-label="rtk" />)
    expect(off.getAttribute('aria-checked')).toBe('false')
  })

  it('is a real button, so it is keyboard reachable', () => {
    const el = mount(<Switch checked={false} onCheckedChange={() => {}} aria-label="rtk" />)

    expect(el.tagName).toBe('BUTTON')
    expect(el.getAttribute('type')).toBe('button')
  })

  it('calls back on click', () => {
    const onChange = vi.fn()
    const el = mount(<Switch checked={false} onCheckedChange={onChange} aria-label="rtk" />)

    act(() => el.click())

    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('does not call back when disabled', () => {
    // `disabled` carries both "your token may not do this" and "a request is
    // already in flight". Clicking again must be inert in both.
    const onChange = vi.fn()
    const el = mount(
      <Switch checked={false} onCheckedChange={onChange} disabled aria-label="rtk" />,
    )

    act(() => el.click())

    expect(onChange).not.toHaveBeenCalled()
    expect(el.disabled).toBe(true)
  })

  it('carries the accessible label it was given', () => {
    const el = mount(<Switch checked onCheckedChange={() => {}} aria-label="enable rtk" />)

    expect(el.getAttribute('aria-label')).toBe('enable rtk')
  })
})
