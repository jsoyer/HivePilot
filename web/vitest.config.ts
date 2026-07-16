import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config'

// Separate from vite.config.ts (kept build-only) so `vite build` never pulls
// in vitest's config surface — mirrors the split used by most Vite+Vitest
// setups. Reuses the same plugins/aliases as the app build.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      environmentOptions: {
        jsdom: {
          // jsdom disables Web Storage on the default `about:blank` origin —
          // give it a real origin so localStorage (used by the token gate /
          // api client) works in tests.
          url: 'http://localhost/',
        },
      },
      include: ['src/**/*.test.{ts,tsx}'],
      setupFiles: ['./vitest.setup.ts'],
      globals: false,
    },
  }),
)
