import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { RoleAvatar } from './RoleAvatar'
import { roleAvatarDefinition, roleStateAnimation } from '@/lib/role-avatars'

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

describe('RoleAvatar', () => {
  it('renders a procedural avatar (svg) for a known role', () => {
    act(() => root.render(<RoleAvatar role="developer" label="Gustave" />))
    const wrapper = container.querySelector('[data-testid="agent-avatar-developer"]')
    expect(wrapper).not.toBeNull()
    expect(wrapper?.querySelector('svg')).not.toBeNull()
  })

  it('falls back to a coloured initial for an unknown role', () => {
    act(() => root.render(<RoleAvatar role="mystery" label="Zebra" />))
    const wrapper = container.querySelector('[data-testid="agent-avatar-mystery"]')
    expect(wrapper).not.toBeNull()
    expect(wrapper?.querySelector('svg')).toBeNull()
    expect(wrapper?.textContent).toBe('Z')
  })
})

describe('role-avatars', () => {
  it('gives each mapped role a distinct body colour', () => {
    const ceo = roleAvatarDefinition('ceo')
    const dev = roleAvatarDefinition('developer')
    expect(ceo?.colors?.body).toBeTruthy()
    expect(dev?.colors?.body).toBeTruthy()
    expect(ceo?.colors?.body).not.toBe(dev?.colors?.body)
  })

  it('returns null for an unmapped role', () => {
    expect(roleAvatarDefinition('nope')).toBeNull()
  })

  it('maps run state onto base animation keys', () => {
    expect(roleStateAnimation('idle')).toBe('idle')
    expect(roleStateAnimation('running')).toBe('working')
    expect(roleStateAnimation('success')).toBe('happy')
    expect(roleStateAnimation('failed')).toBe('sad')
    expect(roleStateAnimation('blocked')).toBe('suspicious')
  })
})
