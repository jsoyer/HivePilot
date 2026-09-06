// Node 22+ ships an experimental global `localStorage` gated behind
// `--localstorage-file` (throws/warns without it). It shadows jsdom's own
// window.localStorage in vitest's environment population (jsdom's copy is
// filtered out because `localStorage in global` is already true — see
// vitest's populateGlobal). Replace it with a minimal, spec-shaped, in-memory
// implementation so tests can exercise real Storage read/write/remove/clear
// behavior without relying on Node's file-backed experimental API.
class MemoryStorage implements Storage {
  #store = new Map<string, string>()

  get length(): number {
    return this.#store.size
  }

  clear(): void {
    this.#store.clear()
  }

  getItem(key: string): string | null {
    return this.#store.has(key) ? this.#store.get(key)! : null
  }

  key(index: number): string | null {
    return Array.from(this.#store.keys())[index] ?? null
  }

  removeItem(key: string): void {
    this.#store.delete(key)
  }

  setItem(key: string, value: string): void {
    this.#store.set(key, String(value))
  }
}

for (const target of [globalThis, window]) {
  Object.defineProperty(target, 'localStorage', {
    value: new MemoryStorage(),
    configurable: true,
    writable: true,
  })
}

// jsdom does not implement `matchMedia`. The procedural avatar renderer
// (`@bible-strong/avatar-react`) queries `prefers-reduced-motion` on mount, so
// provide a minimal, always-"no-match" stub to keep it renderable under test.
if (typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string): MediaQueryList =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  })
}

// React 19's `act()` requires this flag in non-testing-library environments
// (see https://react.dev/warnings/react-dom-test-utils) — without it, act()
// still runs but React emits a spurious "not configured to support act"
// warning on every update.
;(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true
