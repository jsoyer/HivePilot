import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { createRun, fetchProjectNames, fetchTaskNames } = vi.hoisted(() => ({
  createRun: vi.fn(),
  fetchProjectNames: vi.fn(),
  fetchTaskNames: vi.fn(),
}))

vi.mock('@/lib/pollen-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/pollen-api')>()
  return { ...actual, createRun, fetchProjectNames, fetchTaskNames }
})

import { NewRunDrawer } from './NewRunDrawer'

let container: HTMLDivElement
let root: Root

function setSelectValue(select: HTMLSelectElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value')!.set!
  setter.call(select, value)
  select.dispatchEvent(new Event('change', { bubbles: true }))
}

function setInputValue(input: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement : HTMLInputElement
  const setter = Object.getOwnPropertyDescriptor(window[proto.name as 'HTMLInputElement'].prototype, 'value')!
    .set!
  setter.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

async function mount(onCreated = vi.fn(), onClose = vi.fn()) {
  await act(async () => {
    root.render(<NewRunDrawer onCreated={onCreated} onClose={onClose} />)
    await Promise.resolve()
    await Promise.resolve()
  })
  return { onCreated, onClose }
}

beforeEach(() => {
  createRun.mockReset()
  createRun.mockResolvedValue({ run_id: 1, status: 'running' })
  fetchProjectNames.mockReset()
  fetchTaskNames.mockReset()
  fetchProjectNames.mockResolvedValue(['acme-web', 'api'])
  fetchTaskNames.mockResolvedValue(['audit', 'deploy'])
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => {
    root.unmount()
  })
  container.remove()
  vi.restoreAllMocks()
})

describe('NewRunDrawer', () => {
  it('CRITICAL: offers project and task as enumerable pick-lists, not free-text boxes', async () => {
    await mount()
    const task = container.querySelector('#new-run-task') as HTMLSelectElement
    const project = container.querySelector('#new-run-project') as HTMLSelectElement
    expect(task.tagName).toBe('SELECT')
    expect(project.tagName).toBe('SELECT')
    expect(Array.from(task.querySelectorAll('option')).map((o) => o.value)).toEqual([
      '',
      'audit',
      'deploy',
    ])
    expect(Array.from(project.querySelectorAll('option')).map((o) => o.value)).toEqual([
      '',
      'acme-web',
      'api',
    ])
  })

  it('submits the selected project/task through createRun', async () => {
    const { onCreated } = await mount()
    act(() => {
      setSelectValue(container.querySelector('#new-run-task') as HTMLSelectElement, 'deploy')
    })
    act(() => {
      setSelectValue(container.querySelector('#new-run-project') as HTMLSelectElement, 'acme-web')
    })
    await act(async () => {
      ;(container.querySelector('form') as HTMLFormElement).dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }),
      )
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(createRun).toHaveBeenCalledWith({
      task: 'deploy',
      project: 'acme-web',
      extra_prompt: undefined,
      auto_git: false,
    })
    expect(onCreated).toHaveBeenCalled()
  })

  it('keeps submit disabled until both required values are chosen', async () => {
    await mount()
    const submit = container.querySelector('button[type="submit"]') as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    act(() => {
      setSelectValue(container.querySelector('#new-run-task') as HTMLSelectElement, 'deploy')
    })
    expect((container.querySelector('button[type="submit"]') as HTMLButtonElement).disabled).toBe(true)
    act(() => {
      setSelectValue(container.querySelector('#new-run-project') as HTMLSelectElement, 'acme-web')
    })
    expect((container.querySelector('button[type="submit"]') as HTMLButtonElement).disabled).toBe(false)
  })

  it('CRITICAL: falls back to a free-text field (never a locked-out form) when a catalogue cannot be listed', async () => {
    fetchProjectNames.mockRejectedValue(new Error('boom'))
    await mount()
    const project = container.querySelector('#new-run-project') as HTMLInputElement
    expect(project.tagName).toBe('INPUT')
    expect(container.querySelector('[data-testid="new-run-project-fallback"]')).not.toBeNull()
    // The task catalogue resolved fine, so that field stays a pick-list.
    expect((container.querySelector('#new-run-task') as HTMLElement).tagName).toBe('SELECT')
  })

  it('falls back to free text when a catalogue is legitimately empty', async () => {
    fetchTaskNames.mockResolvedValue([])
    await mount()
    expect((container.querySelector('#new-run-task') as HTMLElement).tagName).toBe('INPUT')
  })

  it('surfaces a create failure inline instead of closing silently', async () => {
    createRun.mockRejectedValue(new Error('nope'))
    const { onCreated } = await mount()
    act(() => {
      setSelectValue(container.querySelector('#new-run-task') as HTMLSelectElement, 'deploy')
    })
    act(() => {
      setSelectValue(container.querySelector('#new-run-project') as HTMLSelectElement, 'acme-web')
    })
    await act(async () => {
      ;(container.querySelector('form') as HTMLFormElement).dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }),
      )
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(container.querySelector('[role="alert"]')).not.toBeNull()
    expect(onCreated).not.toHaveBeenCalled()
  })

  it('renders inside a dialog so it never occupies the space where content belongs', async () => {
    await mount()
    expect(container.querySelector('[role="dialog"]')).not.toBeNull()
  })

  it('passes the optional extra prompt through only when non-blank', async () => {
    await mount()
    act(() => {
      setSelectValue(container.querySelector('#new-run-task') as HTMLSelectElement, 'deploy')
    })
    act(() => {
      setSelectValue(container.querySelector('#new-run-project') as HTMLSelectElement, 'acme-web')
    })
    act(() => {
      setInputValue(container.querySelector('#new-run-extra-prompt') as HTMLTextAreaElement, '  ')
    })
    await act(async () => {
      ;(container.querySelector('form') as HTMLFormElement).dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }),
      )
      await Promise.resolve()
    })
    expect(createRun.mock.calls[0][0].extra_prompt).toBeUndefined()
  })
})
