## Summary

Completes **HP-18** (pluggable concierge classifier over an OpenAI-compatible API) and **HP-19** (selectable OSS model-profile columns + roster overlay). Continues stale #596 onto current `main`.

### HP-18 — concierge runner is configurable

The classifier was hardwired to `RunnerDefinition(kind="claude")`. It is now selected by settings:

- `HIVEPILOT_CHATOPS_CONCIERGE_RUNNER` — `claude` (default), `openai`, or `openrouter`
- `HIVEPILOT_CHATOPS_CONCIERGE_MODEL` — endpoint model id (unchanged)
- `HIVEPILOT_CHATOPS_CONCIERGE_API_BASE` — threaded as `OPENAI_BASE_URL` so only the key is a secret

New built-in **`openai` runner** (`OpenAiCompatRunner`): API-only, mirrors `OpenRouterRunner`, fail-closed on a missing `OPENAI_API_KEY`, masks the key at the runner. The Claude path (including the hard CLI no-tools invariant on untrusted chat text) is unchanged. `HIVEPILOT_CHATOPS_CONCIERGE_MODE` / Anthropic key fallback apply only when the runner is `claude`.

```bash
HIVEPILOT_CHATOPS_CONCIERGE_RUNNER=openai
HIVEPILOT_CHATOPS_CONCIERGE_MODEL=glm-5.3-flash
HIVEPILOT_CHATOPS_CONCIERGE_API_BASE=https://opencode.ai/zen/go/v1
OPENAI_API_KEY=<Zen key>
```

### HP-19 — OSS preset is selectable, not the new default

`model_profiles.yaml` grows `opencode:` / `openai:` columns beside `grok` / `cursor` for `coding` / `architecture` / `automation`. Shipped `roles.yaml` vendors are untouched. Select the overlay with `HIVEPILOT_ROSTER_PRESET=oss` (`roster-presets/oss.yaml`).

| Profile | OpenCode Go / `openai:` | Ollama Cloud (comment) |
| -- | -- | -- |
| automation | `glm-5.3-flash` | `gpt-oss:20b`, `nemotron-3-nano:30b` |
| coding | `kimi-k2.7-code` | `qwen3.5:397b`, `kimi-k2.7-code` |
| architecture | `deepseek-v4-pro` | `deepseek-v4-flash`, `deepseek-v4-pro` |

Linear: [HP-18](https://linear.app/js-workspace/issue/HP-18/concierge-pluggable-runner-oss-model-ollamaopenrouter-also-fixes), [HP-19](https://linear.app/js-workspace/issue/HP-19/model-profiles-add-an-oss-preset-ollamaopenrouter). Supersedes #596.

Replay: `hivepilot run example-api docs --dry-run`

## Testing

- [ ] `pytest` on touched suites (recorded after the run)
- [ ] `hivepilot lint`
- [ ] `hivepilot run example-api docs --dry-run`
