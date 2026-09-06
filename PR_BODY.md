## Summary

HP-63: Pollen as an installable PWA with optional Web Push.

- Manifest + service worker (`vite-plugin-pwa` injectManifest) + 192/512 icons.
- Home-screen install button (`beforeinstallprompt`) and iOS/safe-area meta.
- FastAPI serves `/manifest.webmanifest`, `/sw.js`, `/registerSW.js`, and PWA icons at the API root (same pattern as `/favicon.svg`).
- Optional push: operator VAPID keys → `GET /v1/push/config`, `POST /v1/push/subscribe|unsubscribe`, `webpush` notifier channel. Private key never leaves the server. `pywebpush` is lazy-imported.

Linear: [HP-63](https://linear.app/js-workspace/issue/HP-63/pwa-mobile-installable-responsive-push).

Does not implement HP-62 voice. iOS push only after home-screen install.

## Testing

- [x] `cd web && npm test -- --run src/components/pwa/InstallPrompt.test.tsx src/components/pwa/PushToggle.test.tsx src/components/Pollen.test.tsx src/lib/i18n/fr.test.ts`
- [x] `cd web && npm run build` (Node 26.5.0 → `index-BK8wKdye.js` + `sw.js`)
- [x] `pytest tests/test_web_push.py tests/test_webui.py`
- [x] `ruff check` on touched Python

Replay: `HIVEPILOT_ENABLE_WEBUI=1 hivepilot api serve`; open `/ui/` on a phone-sized viewport; Install appears after `beforeinstallprompt`. Set VAPID keys + `webpush` in `HIVEPILOT_NOTIFICATION_CHANNELS` to show the bell.
