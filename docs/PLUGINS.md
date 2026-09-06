# Plugins

A plugin is a Python module that exposes a single function, `register() -> dict[str, Any]`. Each key in the returned dict is a contribution type (`runners`, `notifiers`, `secrets`, `health`, `panels`, `skills`) or, if the key isn't one of those, the name of a lifecycle hook (`before_step`, `after_step`, `on_pipeline_end`, `on_error`, …).

Registration is applied **atomically per plugin**: everything a single plugin stages — runners, notifiers, hooks, whatever — is committed together, or none of it is. If any part of a plugin's registration collides with an existing name, the entire plugin's staged contributions are rolled back. One misbehaving plugin cannot partially register.

## Contribution types

| Type | Key | Contract | Collision |
|---|---|---|---|
| Runners | `runners` | `{kind: RunnerClass(BaseRunner)}` → merged into `RUNNER_MAP` | Hard error |
| Notifiers | `notifiers` | `{name: Callable[[str], None]}` → merged into `NOTIFIER_MAP` | Hard error |
| Secrets | `secrets` | `{name: SecretsBackend}`, a Protocol with `resolve(ref, settings) -> str` → merged into `SECRETS_MAP` | Hard error |
| Health | `health` | `{name: Callable -> HealthStatus}`; result is normalized and the check never raises past the framework | Hard error on name collision |
| Panels | `panels` | `list[PanelSpec]`, each `{name, title, fetch, min_role?}`; contributes tabs to the Pollen dashboard | Invalid `min_role` (not in `ROLE_RANKS`) is a fail-closed registration error |
| Skills | `skills` | `list[SkillSpec]`, each `{name, description, provider, files, system_prompt?, applies_to?, min_role?}` | See [SKILLS.md](./SKILLS.md) |
| Graph sources | `graph_sources` | `list[GraphSourceSpec]`, each `{name, data, node_detail?, title?, min_role?, params?}` → registered into `hivepilot.graph`'s module-global source registry via `register_graph_source()` | Name collision with a built-in or another plugin → `GraphSourceNameCollisionError`, rolled back atomically |
| Capabilities | `capabilities` | `list[str]` from the closed `hivepilot.plugin_capabilities.PLUGIN_CAPABILITIES` vocabulary — an advisory manifest of what the plugin intends to do | Unknown token → `PluginCapabilityInvalidError`; token not in `settings.plugins_capability_policy` → `PluginCapabilityDeniedError`, rolled back atomically. See "Capability manifest & policy gate" below. |
| Lifecycle hooks | any other key | a callable (`before_step`, `after_step`, `on_pipeline_end`, `on_error`, …) | No collision check — every plugin's hook for a given name runs |

Secrets contributions are covered in more depth in [SECURITY.md](./SECURITY.md); panels and the graph view in [DASHBOARD.md](./DASHBOARD.md); skills in [SKILLS.md](./SKILLS.md).

## How plugins load

Plugins load from three sources. All three are gated by the master switch `plugins_enabled` (default `True`) and a per-plugin `plugins_disabled` skip-list that is checked **before the module executes** — a disabled plugin's code never runs.

- **local-file** (`source="local-file"`): every `*.py` file under `<base_dir>/plugins/` except files prefixed with `_`. Files are compiled directly from source, bypassing the `.pyc` cache, so hot-reload picks up edits immediately.
- **entry-point** (`source="entry-point"`): any installed package that declares an entry point in the `hivepilot.plugins` group, discovered via `importlib.metadata.entry_points(group="hivepilot.plugins")`.
- **explicit-entry** (`source="explicit-entry"`): a single pinned `module:attr` target set via the `HIVEPILOT_PLUGINS_ENTRY` environment variable.

### Multi-directory local-file search (`plugins_extra_dirs`)

`<base_dir>/plugins/` is always scanned first. Additional directories can be
scanned afterward via `settings.plugins_extra_dirs` (env `HIVEPILOT_PLUGINS_EXTRA_DIRS`,
an `os.pathsep`-separated list of directory paths — `:` on POSIX, `;` on
Windows). This is the fix for a specific deployment gap: a config repo that
sets `HIVEPILOT_BASE_DIR` to its own directory (to load its own
`plugins/vendored_skills.py`, say) can no longer see the engine's shipped
`plugins/*.py` — they live under the engine repo, which is no longer
`base_dir`. `plugins_extra_dirs` lets that same deployment point back at the
engine's `plugins/` directory too, so it gets BOTH sets of plugins instead of
having to choose one.

```bash
# .env
HIVEPILOT_PLUGINS_EXTRA_DIRS=/opt/hivepilot/plugins:/srv/config-repo/plugins
```

Rules:

- Directories are scanned in order: `base_dir/plugins` first, then each
  `plugins_extra_dirs` entry in the order listed.
- **Dedup by module stem, first-wins**: if the same `<name>.py` stem appears
  in more than one scanned directory, only the FIRST occurrence loads — later
  ones are skipped (logged at info level), never a collision error. Since
  `base_dir/plugins` is always scanned first, it always wins over any extra
  directory, so a deployment's own `base_dir/plugins` can deliberately
  override a same-named shipped plugin.
- A `plugins_extra_dirs` entry that doesn't exist on disk is silently
  skipped — same as a missing `base_dir/plugins` always has been.
- `plugins_enabled` and `plugins_disabled` apply identically across every
  scanned directory — a disabled plugin is skipped regardless of which
  directory it was found in.
- Additive/opt-in: empty (the default) is byte-identical to the
  single-directory scan that existed before this setting.

Entry-point declaration in `pyproject.toml`:

```toml
[project.entry-points."hivepilot.plugins"]
myplugin = "my_package.plugin:register"
```

Minimal local plugin, `plugins/myplugin.py`:

```python
def register() -> dict:
    def my_notifier(message: str) -> None:
        print(f"[myplugin] {message}")

    return {
        "notifiers": {"myplugin": my_notifier},
    }
```

## Trust model (fail-closed)

Plugin code reaches the process from exactly three trust sources:

1. Local files under `plugins/` — the same trust boundary as editing `tasks.yaml`. Anyone who can write to the repo can write a plugin.
2. Installed packages — the `pip install` trust boundary. If you trust what you installed, you trust its registered plugin.
3. `hivepilot plugins install <name>...` — the ONE narrow, curated exception to "no network fetch of plugin code": it fetches a maintainer-vetted `plugins/<name>.py` from a fixed, in-repo allow-list (`hivepilot.services.plugin_installer.KNOWN_EXAMPLE_PLUGINS`) over HTTPS and writes it into the managed plugins dir. No arbitrary URL or path is ever reachable, and the fetched file is written to disk only — it is picked up by the same local-file loader (source 1 above) on the next process start, subject to every gate below exactly like a hand-copied plugin file. See "Installing built-in example plugins" for the full walkthrough and its own trust discussion.

A plugin runs with full process privileges. There is no sandbox, no permission model, and no capability restriction on what a plugin's code can do once loaded.

Gating happens at three independent layers:

1. `plugins_enabled` — master on/off switch for the whole plugin system.
2. `plugins_disabled` — a per-name skip list checked before a plugin's module is even imported.
3. Per-plugin `<name>_enabled` flags — read inside each plugin's own `register()`, so a plugin can no-op itself out even when its file is present and not globally disabled.

If a plugin's `register()` call or its module import raises, the failure is logged and that plugin is **skipped** — it never kills the host process. The one exception is a name or kind **collision**, which is a hard, propagating failure for that plugin's registration (see Contribution types above).

Hot-reload (`PluginManager.reload()`) is staging-then-commit: a full re-scan of all plugin sources builds a candidate state without touching the live global maps, and that candidate is only committed if the whole re-scan succeeds. Reload is only effective when explicitly invoked (a scheduler tick or `SIGHUP`) — flipping a flag or dropping a new binary on `PATH` otherwise takes effect at the next process start, not live.

### Capability manifest & policy gate (Phase 26b)

**This is advisory admission control, NOT runtime sandboxing.** A plugin declaring `capabilities = []` (or nothing at all) still runs with full process privileges once loaded — nothing in this repo can interpreter-level-enforce what a plugin's code actually does. True process isolation (subprocess sandboxing, seccomp, OS-level capability dropping) is future work, not shipped here.

What IS shipped: a plugin MAY declare `register()["capabilities"] = [...]` from a closed vocabulary (`hivepilot.plugin_capabilities.PLUGIN_CAPABILITIES`): `network`, `filesystem`, `subprocess`, `secrets_access`, `env`. This is checked at LOAD TIME against `settings.plugins_capability_policy` (env `HIVEPILOT_PLUGINS_CAPABILITY_POLICY`, comma-separated or JSON array, e.g. `network,env` or `["network","env"]`, same convention as `plugins_disabled`) — the set of capability tokens the operator is willing to ALLOW a plugin to declare. Empty (default) = fail-closed, deny every declared capability.

- **Default `[]` (fail-closed, deny-declared):** ANY plugin declaring ANY capability is denied at load until the operator explicitly opts that token in.
- **A plugin declaring NO capabilities at all is completely unaffected**, regardless of policy — this is purely additive; every plugin shipped before this manifest existed keeps working exactly as before. Note this is an *exemption*, not a clean bill of health: a plugin that declares nothing may still do anything (see the sandboxing caveat above). `plugins audit` exists to find exactly that gap.
- A denied or malformed (`PluginCapabilityDeniedError` / `PluginCapabilityInvalidError`) capability manifest fails that plugin's **whole registration**, atomically rolling back its OTHER contributions — the same fail-closed, atomic-per-plugin-rollback shape as an invalid panel/skill `min_role` (see Contribution types above). Be aware the error then **propagates out of `PluginManager` construction**: unlike a plugin whose `register()` raises (logged and skipped), a capability denial is not survivable per-plugin, so one denied plugin means plugin loading fails as a whole.

#### Operators: `gh` and `rtk` require `subprocess`

`plugins/gh.py` and `plugins/rtk.py` each declare `capabilities: ["subprocess"]`, because each genuinely spawns a child process (`subprocess.run(["gh", ...])`; `subprocess.run(["rtk", "proxy", "bash", "-lc", ...])`, falling back to a bare `bash -lc`). They previously declared nothing and so shelled out from entirely outside this gate.

**Both are enabled by default** (`HIVEPILOT_GH_ENABLED` / `HIVEPILOT_RTK_ENABLED` are opt-OUT; `gh` additionally requires the `gh` binary on `PATH`). So on the fail-closed default policy they are denied — and per the bullet above that denial takes plugin loading down with it. Any deployment using them must either allowlist the token:

```bash
HIVEPILOT_PLUGINS_CAPABILITY_POLICY=subprocess
```

or disable the plugins (`HIVEPILOT_GH_ENABLED=false`, `HIVEPILOT_RTK_ENABLED=false`).

Neither declares `env`, `filesystem`, `secrets_access` or `network`. Those would all be *transitive*: the child process may reach the network and the filesystem, and the shared `merge_environments` helper (used by every built-in runner) copies `os.environ` and the engine-resolved secrets into the child's environment — but none of it is first-party behaviour of these two modules, and making `env` an automatic co-requisite of `subprocess` for every command runner would cost operators an allowlist entry while discriminating nothing. Each `register()` docstring records the full reasoning, including why declaring `network` on `gh` was rejected here: it would additionally enrol the `gh` runner kind in the runtime outward-consent gate in `hivepilot.registry.resolve_runner_class`, which changes dispatch rather than load admission and deserves its own decision.

> **Parsing trap — prefer the plain/CSV form.** The policy value is decoded as a JSON array *or* a comma-separated list *or* a single plain value. Both `source .env` in a shell and systemd's `EnvironmentFile=` strip the double quotes from `HIVEPILOT_PLUGINS_CAPABILITY_POLICY=["subprocess"]`, leaving the literal text `[subprocess]`. That is not valid JSON, so it falls through to CSV parsing and yields the single nonsense token `"[subprocess]"`, which matches no real capability — everything is silently denied, with no parse error to point at it. The quote-free forms `subprocess` and `subprocess,network` survive both shell sourcing and systemd intact. This fails closed (a malformed value can only ever *deny*, never widen: the policy side is intentionally not validated against the vocabulary, so an unrecognised allow-entry simply matches nothing), but the failure is quiet, so use the plain form.

`hivepilot plugins audit` is a companion **read-only static scanner**: it `ast`-parses every local plugin's source TEXT (never imports/execs it, never calls `register()`) to flag risky imports/calls — `subprocess`, network sockets, `os.system`, `eval`/`exec`, write-mode `open`, `ctypes`, and similar — and cross-references them against that plugin's own declared `capabilities` manifest (itself extracted statically, best-effort, from a literal `"capabilities": [...]` entry in its `register()` source) to surface **under-declaration**: code that appears to use a capability the plugin didn't declare. This is advisory, not exhaustive — a dynamically constructed capabilities list, or a risky call the scanner doesn't recognize, won't be caught. Pass `--strict` to exit non-zero (CI-friendly) when any under-declaration is found; the default is a 0-exit report.

The scanner covers **every directory the loader can load from** — it derives its root list from the same `plugin_scan_dirs` helper `PluginManager` itself iterates, so `base_dir/plugins`, the config repo clone's `plugins/`, the managed `plugins install` destination (`$XDG_DATA_HOME/hivepilot/plugins`) and every `HIVEPILOT_PLUGINS_EXTRA_DIRS` entry are all audited, with the same first-directory-wins stem dedup. Two deliberate differences from the loader, both widening coverage: a plugin disabled via `plugins_disabled`, or on a host with the master `plugins_enabled=false` kill switch, is **still audited** — auditing is what you do *before* deciding to enable something.

Finding **zero** source files always prints the directories that were searched, so an empty report can be told apart from a mis-aimed scan. Under `--strict` an empty scan is an **error (exit 1)**: `--strict`'s contract is "fail CI on any under-declaration", and a scan that examined no files has not established there are none. If you run `plugins audit --strict` in CI on a host that legitimately has no local-file plugins, drop `--strict` (or point `HIVEPILOT_PLUGINS_EXTRA_DIRS` at the sources you meant to scan) — a gate over zero files was only ever providing false assurance.

## Shipped plugins (inventory)

33 modules ship under `plugins/*.py`. The tables below group the main ones by what they contribute.

**Agent runners** (PATH-gated: flag AND binary must both be present):

| Plugin | Contributes | Default |
|---|---|---|
| `gemini` | runner | ON, PATH-gated |
| `opencode` | runner | ON, PATH-gated |
| `ollama` | runner | ON, PATH-gated |
| `pi` | runner | ON, PATH-gated |
| `qwen_code` (kind `qwen-code`) | runner | ON, PATH-gated |
| `kimi_cli` (kind `kimi-cli`) | runner | ON, PATH-gated |
| `antigravity` | runner | ON, PATH-gated |
| `codex` | runner | ON, PATH-gated |
| `cursor` | runner | ON, PATH-gated |

**Infra runners** (runner + health):

| Plugin | Contributes | Default |
|---|---|---|
| `rtk` | runner + health | ON |
| `herdr` | runner + health | ON |
| `token_savior` | `before_step` hook + health | OFF (opt-in) |
| `hugo` | runner + health | ON, PATH-gated |
| `tmux` | runner + health | ON |
| `gh` | runner + health | ON, PATH-gated |

**Secrets** (secrets backend + health) — see [SECURITY.md](./SECURITY.md):

| Plugin | Contributes | Default |
|---|---|---|
| `bitwarden` | secrets + health | ON |
| `vaultwarden` | secrets + health | ON |
| `infisical` | secrets + health | ON |
| `onepassword` | secrets + health | ON (Connect + direct service-account) |
| `kms` | secrets + health | ON (cloud-KMS envelope/direct; `hivepilot[kms]`) |

**Notifier + hooks:**

| Plugin | Contributes | Default |
|---|---|---|
| `obsidian` | notifier + `before_step`/`after_step`/`on_pipeline_end`/`on_error` hooks | ON |

**Detection only (health, no runner):**

| Plugin | Contributes | Default |
|---|---|---|


**Opt-in / default OFF:**

| Plugin | Contributes | Default |
|---|---|---|
| `headroom` | `before_step` context compression | OFF |
| `hindsight` | `before_step`/`after_step` Hindsight recall/retain (HTTP client) | OFF |
| `sample` | hooks + panel demo | OFF |
| `sample_skill` | skill demo | OFF |
| `example_graph_source` | graph source `run-lineage` (demo) | `example_graph_source_enabled`, OFF (opt-in) |

`gh`, `hugo`, and the seven agent-runner kinds are PATH-gated — they only activate when their flag is on **and** the corresponding binary is found on `PATH`. Everything else in the table is flag-gated only.

### token-savior (MCP symbol index)

[token-savior](https://github.com/mibayy/token-savior) (MIT) is an MCP server
that indexes a codebase into symbols, so an agent can fetch `get(symbol)`
instead of reading whole files.

```bash
pip install "token-savior-recall[mcp]"
export HIVEPILOT_TOKEN_SAVIOR_ENABLED=1
export HIVEPILOT_PLUGINS_CAPABILITY_POLICY=filesystem   # plus whatever you already allow
```

The claude runner already knows how to pass `--mcp-config` and
`--allowed-tools`. What this plugin adds is the wiring, and the wiring is not
boilerplate: the vendor's stanza carries `WORKSPACE_ROOTS`, so the config is
**per project**. Hand-maintaining one JSON file per project is what drifts —
a stale `WORKSPACE_ROOTS` indexes the wrong repository and the agent gives
confident answers about somebody else's code.

On each step the `before_step` hook writes
`<base_dir>/token-savior/<project>.mcp.json` and points the step at it, then
appends `mcp__token-savior-recall` to `allowed_tools` — appends, because
`--allowed-tools` is a whitelist that *restricts*, so replacing it would
silently revoke grants the role depends on.

**It never takes the wheel.** A step that already declares `mcp_config` is
left exactly as it is, and its tools are not pre-approved onto someone else's
server, where they would advertise availability that does not exist.

| Setting | Default | Meaning |
|---|---|---|
| `HIVEPILOT_TOKEN_SAVIOR_ENABLED` | `false` | Opt-in. The server indexes your repository and keeps a memory database — that is the code owner's call. |
| `HIVEPILOT_TOKEN_SAVIOR_PROFILE` | `optimized` | Tool manifest the server exposes (`tiny`/`full` are the vendor's other values). |

Declares `filesystem` and deliberately **not** `subprocess`: a token-savior
process is spawned, but by the agent CLI from the config stanza, under
authority the runner already holds. This plugin writes one JSON file and
reads `PATH`. Over-declaring would cost you an allowlist entry for the most
load-bearing token there is and discriminate nothing.

**On the published numbers.** The README claims 97.9% against 78.3% plain, at
−80% active tokens, measured by its author on its author's benchmark. This
plugin reports no savings of its own, and its health check quotes none: a
vendor ratio measures the vendor's baseline, which is how another tool's
claimed 99.2% turned out to be ~10% here. HivePilot already records
`input_tokens`, `cache_read_tokens` and `cost_usd` per step — run the same
review twice, with the flag off and on, and compare what *your* pipeline
recorded.

## Plugin CLI

```bash
hivepilot plugins list
```

Prints loaded plugins plus breakdown tables: agent runners (built-in vs. plugin, active vs. inactive vs. API-only), other runner kinds, notifiers, secrets backends, and health checks.

```bash
hivepilot plugins health
```

Runs every registered health check and prints a table. Exits non-zero if any check errors — safe to wire into CI.

```bash
hivepilot plugins audit [--strict]
```

Read-only static scan of every local plugin's source, across every directory the loader can load from (see "Capability manifest & policy gate" above). Prints a per-plugin table of risky findings, declared capabilities, and under-declared capabilities. Exits 0 by default (advisory report); `--strict` exits 1 if any plugin under-declares a capability it appears to use, **or if the scan found no files at all** — safe to wire into CI. There is deliberately no `--dir` flag: narrowing a security scan to one directory is how a host's installed plugins go un-vetted.

```bash
hivepilot plugins tui
```

Textual-based interactive browser for inspecting plugins. Pressing `space` toggles the selected plugin in `plugins_disabled`, persisted to `.env`; the change takes effect on the next process start, not live. Requires `HIVEPILOT_ENABLE_TEXTUAL_UI`.

```bash
hivepilot plugins search <query>
```

Metadata-only search against a plugin index. No code is fetched — only name/description/install-command metadata.

```bash
hivepilot plugins info <name>
```

Prints index metadata for a plugin plus the exact `pip`/`git` command to install it. HivePilot never executes that command itself — you run it yourself. Install targets returned from the index are validated and control characters are stripped before display.

```bash
hivepilot plugins available
```

Read-only table of every CURATED built-in example plugin `plugins install` can fetch — name, description, `<NAME>_ENABLED` flag, prerequisite, and whether it's currently installed + enabled. Never fetches anything over the network. See "Installing built-in example plugins" below.

```bash
hivepilot plugins install <name>... [--enable/--no-enable] [--ref REF] [--yes]
```

Fetches one or more curated built-in example plugins into the managed plugins dir and, by default, persists each `<NAME>_ENABLED=true`. See "Installing built-in example plugins" below for the full walkthrough.

For the optional **agent-CLI plugin kinds** (`codex`, `cursor`, `gemini`, `opencode`, `ollama`, `pi`, `qwen-code`, `kimi-cli`, `antigravity`, `vibe` — `_OPTIONAL_AGENT_PLUGIN_KINDS` in `hivepilot/registry.py`), `plugins enable <kind>`/`plugins disable <kind>` toggle them directly: `enable` installs the CLI binary if missing (with consent), places the plugin file, sets the `<KIND>_ENABLED` flag, and verifies the kind actually resolves; `disable` only flips the flag off. For everything else — the CURATED example plugins `plugins install` fetches (`rtk`, `herdr`, `headroom`, …) — there is still no dedicated enable/disable subcommand once installed: toggle via `plugins tui` or by editing `HIVEPILOT_PLUGINS_DISABLED`/`HIVEPILOT_<NAME>_ENABLED` in `.env`, or set the flag at fetch time with `plugins install --enable`/`--no-enable` (see above).

```bash
hivepilot plugins enable codex      # installs the codex CLI if missing, places plugins/codex.py, sets CODEX_ENABLED=true, verifies it resolves
hivepilot plugins disable codex     # flips CODEX_ENABLED=false — does not uninstall the codex binary
```

## Installing built-in example plugins

Before this command existed, trying one of the built-in example plugins (`rtk`, `herdr`, `headroom`, `hugo`, `obsidian`, `kms`, …) meant manually downloading its `plugins/<name>.py` into your config repo, committing, `hivepilot config sync`-ing, and setting its `<NAME>_ENABLED` flag by hand. `hivepilot plugins install` collapses that into one command.

```bash
# See what's available
hivepilot plugins available

# Install one or more, review the confirm-then-run prompt, then confirm
hivepilot plugins install rtk herdr headroom

# Skip the confirmation prompt (e.g. scripted setup)
hivepilot plugins install rtk --yes

# Fetch without persisting the enable flag
hivepilot plugins install kms --no-enable

# Fetch from a pinned ref instead of HIVEPILOT_PLUGINS_SOURCE_REF
hivepilot plugins install hugo --ref v0.3.0
```

**Where it fetches from and where it writes to.** Each name is resolved to `{HIVEPILOT_PLUGINS_SOURCE_REPO}/{HIVEPILOT_PLUGINS_SOURCE_REF}/plugins/<name>.py` (default: this project's own `raw.githubusercontent.com/jsoyer/HivePilot` host, ref `main`) and written into the managed directory `xdg_data_home/plugins` — `~/.local/share/hivepilot/plugins/` by default (`$XDG_DATA_HOME/hivepilot/plugins` if set). That directory is auto-added to the local-file plugin scan path (`hivepilot.plugins._installed_plugins_dir`, scanned right after the config repo's own `plugins/` dir, before `plugins_extra_dirs`) — no manual `HIVEPILOT_PLUGINS_EXTRA_DIRS` needed.

**Only curated names, never an arbitrary fetch.** `plugins install`/`plugins available` only ever know about the fixed allow-list in `hivepilot.services.plugin_installer.KNOWN_EXAMPLE_PLUGINS` — an unknown name is rejected before any network call, with the full list of valid names printed. There is no way to point this command at an arbitrary URL or path.

**Still gated exactly like any other plugin.** Fetching (and even enabling) a plugin this way does **not** bypass any of the three gating layers in "Trust model" above: the fetched file only becomes live Python the next time HivePilot starts, when it's picked up by the normal local-file loader — still subject to `plugins_enabled`, `plugins_disabled`, and its own `<name>_enabled` check inside `register()`. And most of these plugins additionally no-op at runtime until their prerequisite is actually present:

| Plugin | Prerequisite |
|---|---|
| `rtk`, `herdr`, `hugo`, `gh`, `tmux` | the matching binary on `PATH` |
| `bitwarden`, `vaultwarden` | the official Bitwarden `bw` CLI on `PATH` |
| `hindsight` | `pip install hindsight-client` (or `hivepilot[hindsight]`) plus a running Hindsight server |
| `headroom` | `pip install "headroom-ai[all]"` |
| `infisical` | `pip install infisicalsdk` (+ `HIVEPILOT_INFISICAL_TOKEN`/`_WORKSPACE_ID`/`_ENVIRONMENT`) |
| `onepassword` | `pip install onepassword-sdk` for service-account mode (Connect mode needs no extra package) |
| `kms` | `boto3` (AWS) / `google-cloud-kms` (GCP) / `azure-keyvault-keys`+`azure-identity` (Azure), matching `HIVEPILOT_KMS_PROVIDER` |
| `obsidian` | none — just set `HIVEPILOT_OBSIDIAN_VAULT` to your vault's directory |

### hindsight (HP-51)

[Hindsight](https://github.com/vectorize-io/hindsight) is the world-fact memory engine (retain / recall / reflect) on Postgres + pgvector. HivePilot does **not** embed `MemoryEngine`. The bundled plugin is an HTTP client (`hindsight-client`) pointed at a server you deploy:

```bash
pip install "hivepilot[hindsight]"   # or: pip install hindsight-client
# Docker (local Postgres is bundled unless you pass HINDSIGHT_API_DATABASE_URL):
docker run --name hindsight -p 8888:8888 \
  -e HINDSIGHT_API_LLM_PROVIDER=openrouter \
  -e HINDSIGHT_API_LLM_API_KEY=$OPENROUTER_API_KEY \
  ghcr.io/vectorize-io/hindsight:latest
export HIVEPILOT_HINDSIGHT_ENABLED=true
export HIVEPILOT_HINDSIGHT_BASE_URL=http://127.0.0.1:8888
```

`before_step` calls `recall`; `after_step` calls `retain` with a redacted step output. `GET /v1/orchestrator/missions/{id}` calls `reflect()` (HP-54) against the first spawned task's episodic bank, with the engine numeric status as context; the prose is cached on the mission row until the status snapshot changes. mem0 is retired (HP-53): `hivepilot memory migrate-mem0` copies existing memories into the same `{project}:{task}:{role}` Hindsight banks; the bundled plugin and `GET /v1/memories` are gone. honcho (role model) and obsidian (vault) compose.

Two bank namespaces (do not collapse them):

| Bank id | Owner | Contents |
|---|---|---|
| `{project}:{task}:{role}` | HP-51 retain/recall | episodic step facts |
| `role:{name}` | HP-52 role sync | Mission = full role prompt; Directives = `get_rules_for_role()` |

`refresh_roles()` (API CRUD, SIGHUP, daemon adopt) pushes Mission + Directives into each `role:{name}` bank. Disposition (skepticism / literalism / empathy) is **not** a Role field — HivePilot never sends it, so Hindsight keeps its defaults. Path-like rules become `MUST read before acting: {path}`; prose cross-cutting rules are copied as-is. `allowed_tools` / `permission_mode` stay execution gates, not directives. Dormant unless `HIVEPILOT_HINDSIGHT_ENABLED`.

Pollen Memory → Knowledge (HP-55) reads that same `role:{name}` bank: Mental Models (create / edit query / refresh) and Observations (quotes, proof count, optional confidence). Observations are derived — a human edit goes to the listed world/experience facts, which re-consolidates the observation. `GET /v1/hindsight/status` and `GET /v1/hindsight/roles/{role}` require `read`; writes require `run`.

`plugins install` prints each plugin's exact prerequisite after fetching; it never installs a binary or `pip install`s anything on your behalf — that stays an explicit operator decision, same as `hivepilot agents install`'s guided (never automatic) posture for agent CLIs. Restart HivePilot's services after installing for the plugin (and, if `--enable` persisted a flag, the flag) to take effect.

**Trust note.** This is the one narrow exception to "no network fetch of plugin code" in the Trust model above — see that section for the honest accounting of what it does and doesn't change about the security posture.

## Writing a plugin

```python
# plugins/slack_ping.py
import os


def register() -> dict:
    if not os.getenv("SLACK_PING_ENABLED"):
        return {}

    def slack_notifier(message: str) -> None:
        # send `message` to Slack
        ...

    def before_step(context: dict) -> None:
        # runs before every pipeline step
        ...

    return {
        "notifiers": {"slack_ping": slack_notifier},
        "before_step": before_step,
    }
```

Notes:

- Gate optional behavior with a `<name>_enabled`-style flag read inside `register()` — returning `{}` is a clean no-op.
- The `register()` contract is fixed: no arguments in, a `dict[str, Any]` out.
- A name collision on `runners`, `notifiers`, `secrets`, `health`, or an invalid `min_role` on a `panels`/`skills` entry aborts the load for that plugin — nothing it contributes gets registered, and other plugins are unaffected.

## Graph sources

A plugin can contribute a node/edge graph to Pollen's Graph tab (see
[DASHBOARD.md](./DASHBOARD.md#graph-view)) the same way it contributes a
panel — via `register()["graph_sources"] = [GraphSourceSpec, ...]`.
`GraphSourceSpec` is a frozen dataclass defined once in `hivepilot/graph.py`
(reused by plugins, never redefined) with fields `name`, `data`,
`node_detail?`, `title?`, `min_role?` (default `"read"`), `params?`.

A plugin's staged `graph_sources` are committed under the SAME
`_owned_*` ownership model `runners`/`notifiers`/`secrets` already use:
disabling and reloading the plugin removes the source it contributed, and
reloading a still-enabled plugin does not self-collide with its own
previous registration.

Collision and fail-closed behavior:

- A name collision with a built-in source or another plugin's source raises
  `GraphSourceNameCollisionError` — the plugin's ENTIRE registration is
  rolled back atomically (same all-or-nothing rule as every other
  contribution type).
- Unlike `panels`/`skills`, `min_role` is **not** validated at registration
  time. It is resolved fail-closed at fetch time by
  `_resolve_graph_min_role_rank` (`hivepilot/services/api_service.py`): an
  unrecognized role name is treated as the highest possible bar, so it is
  unsatisfiable by any caller — including `admin` — rather than
  accidentally failing open.
- A disabled plugin contributes nothing; its module is never executed.
- A `data()`/`node_detail()` call that raises is caught by
  `run_graph_fetch`/`run_graph_node_detail` and normalized into a single
  `kind="error"` node (or an error `GraphDetail`) — it never surfaces as a
  500, and only the exception TYPE name is ever included, never its
  message.

`plugins list`'s per-plugin "contributes" column enumerates each plugin's
graph sources too — `_CONTRIBUTION_RENDER_ORDER` includes `graph_sources`,
and `plugin_index.graph_source_contributions(plugin_manager)` returns
`{plugin_name: [source_name, ...]}` for the plugins that registered one.

Reference implementation: `plugins/example_graph_source.py` contributes
`run-lineage` (opt-in, default OFF via `example_graph_source_enabled`) — a
`?run=<id>` query renders one run's lineage (run → steps → verdicts) as a
DAG, read-only, tenant-scoped via `state_service` membership checks, and
never includes a secret value.

## See also

- [RUNNERS.md](./RUNNERS.md)
- [SKILLS.md](./SKILLS.md)
- [SECURITY.md](./SECURITY.md)
- [DASHBOARD.md](./DASHBOARD.md)
