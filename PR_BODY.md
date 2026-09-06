## Summary

HP-62: voice mode in Pollen — dictation, “call an agent”, and optional BYO cloud TTS.

- Browser Web Speech is the default (no keys): mic on Chat + Espaces composers, `speechSynthesis` for spoken replies.
- Chat “Call an agent” toggle (`hivepilot.webui.voice-call`) speaks concierge `answer_text`.
- Optional cloud TTS (OpenAI / ElevenLabs / Cartesia): operator BYO key stays on the server. `GET /v1/voice/config` advertises engines; `POST /v1/voice/tts` proxies MPEG. Keys never reach the browser.
- Cloud STT is advertised only; upload is a later slice.

Linear: [HP-62](https://linear.app/js-workspace/issue/HP-62/mode-voix-stttts-byo-elevenlabsopenaicartesia-micro-composer-appeler).

Does not implement HP-18 (concierge OSS runner).

## Testing

- [x] `cd web && npm test -- --run src/lib/browser-speech.test.ts src/lib/voice-reply.test.ts src/lib/api.test.ts src/lib/pollen-api.test.ts src/components/voice/ComposerMic.test.tsx src/components/views/ChatView.test.tsx src/lib/i18n/fr.test.ts src/components/Pollen.test.tsx`
- [x] `cd web && npm run build` (Node 26.5.0 → `index-D4xc0GnV.js` + `sw.js`)
- [x] `pytest tests/test_voice_service.py tests/test_settings_secret_repr.py`
- [x] `ruff check` on touched Python

Replay: `HIVEPILOT_ENABLE_WEBUI=1 hivepilot api serve`; open Chat; click the mic (Chrome) to dictate; toggle the phone icon to hear answers. Set `HIVEPILOT_VOICE_TTS_PROVIDER=openai` + `HIVEPILOT_VOICE_TTS_API_KEY` to use the proxy instead of `speechSynthesis`.
