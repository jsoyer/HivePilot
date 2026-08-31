import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SpaceMessage, SpaceSummary } from '@/lib/pollen-api'
import type { Role } from '@/lib/role-context'

const { fetchSpaces, fetchSpaceMessages, postSpaceMessage, useRoleMock, useEventStreamMock } =
  vi.hoisted(() => ({
    fetchSpaces: vi.fn(),
    fetchSpaceMessages: vi.fn(),
    postSpaceMessage: vi.fn(),
    useRoleMock: vi.fn(),
    useEventStreamMock: vi.fn(),
  }))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, fetchSpaces, fetchSpaceMessages, postSpaceMessage }
})

vi.mock('@/lib/role-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/role-context')>()
  return { ...actual, useRole: useRoleMock }
})

vi.mock('@/lib/use-event-stream', () => ({ useEventStream: useEventStreamMock }))

import { EspacesView } from './EspacesView'

function space(overrides: Partial<SpaceSummary>): SpaceSummary {
  return {
    id: 1,
    kind: 'dm',
    title: 'Camille & CEO',
    participants: [{ type: 'human' }, { type: 'role', id: 'ceo' }],
    message_count: 1,
    last_message_at: '2026-07-18T10:00:00Z',
    ...overrides,
  }
}

function message(overrides: Partial<SpaceMessage>): SpaceMessage {
  return {
    id: 1,
    space_id: 1,
    sender_type: 'role',
    sender_id: 'ceo',
    body: 'Bonjour',
    created_at: '2026-07-18T10:00:00Z',
    ...overrides,
  }
}

function mockRole(role: Role) {
  useRoleMock.mockReturnValue({
    role,
    rank: 0,
    can: (required: Role) => {
      const order: Role[] = ['read', 'run', 'approve', 'admin']
      return order.indexOf(role) >= order.indexOf(required)
    },
  })
}

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  fetchSpaces.mockReset()
  fetchSpaceMessages.mockReset()
  postSpaceMessage.mockReset()
  useRoleMock.mockReset()
  useEventStreamMock.mockReset()
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.restoreAllMocks()
})

async function mountResolved() {
  await act(async () => {
    root.render(<EspacesView />)
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('EspacesView', () => {
  it('lists spaces, auto-selects the first, and shows its messages', async () => {
    fetchSpaces.mockResolvedValue([space({ id: 7, title: 'Le Pont' })])
    fetchSpaceMessages.mockResolvedValue([message({ id: 3, space_id: 7, body: 'Salut' })])
    mockRole('run')
    await mountResolved()

    expect(container.querySelector('[data-testid="espaces-space-7"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="espaces-message-3"]')?.textContent).toContain(
      'Salut',
    )
    expect(fetchSpaceMessages).toHaveBeenCalledWith(7)
  })

  it('posts a message and refetches the thread', async () => {
    fetchSpaces.mockResolvedValue([space({ id: 7 })])
    fetchSpaceMessages.mockResolvedValue([])
    postSpaceMessage.mockResolvedValue({ id: 99 })
    mockRole('run')
    await mountResolved()

    const composer = container.querySelector('[data-testid="espaces-composer"]') as HTMLTextAreaElement
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!
    setter.call(composer, 'on démarre ?')
    composer.dispatchEvent(new Event('input', { bubbles: true }))

    const send = container.querySelector('[data-testid="espaces-send"]') as HTMLButtonElement
    await act(async () => {
      send.click()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(postSpaceMessage).toHaveBeenCalledWith(7, 'on démarre ?')
    // one initial fetch + one refetch after posting
    expect(fetchSpaceMessages.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('hides the composer for a read-only token', async () => {
    fetchSpaces.mockResolvedValue([space({ id: 7 })])
    fetchSpaceMessages.mockResolvedValue([])
    mockRole('read')
    await mountResolved()

    expect(container.querySelector('[data-testid="espaces-composer"]')).toBeNull()
    expect(container.textContent).toMatch(/read-only|Lecture seule/i)
  })

  it('shows an empty state when there are no spaces', async () => {
    fetchSpaces.mockResolvedValue([])
    mockRole('run')
    await mountResolved()

    expect(container.querySelector('[data-testid="espaces-empty"]')).not.toBeNull()
  })
})
