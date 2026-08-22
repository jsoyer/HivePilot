"""Tests for hivepilot.services.config_doctor (`hivepilot config doctor` +
`hivepilot plugins verify`).

Each check is covered with BOTH a passing and a failing fixture, asserting
the failing message names the problem AND the fix -- per the sprint's
Anti-Goodhart requirement, these assert on the actual detection LOGIC (a
real dangling reference / a real cwd-relative path / a real missing
plugin), never merely on "the function returns a list".
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hivepilot.config import Settings, settings
from hivepilot.services import config_doctor


def _write_minimal_valid_config(base_dir: Path) -> None:
    """Six required files + prompts/agents, matching test_cli.py's helper --
    keeps validate_config() (called by check_dangling_references) silent so
    each test can isolate the ONE new check it's exercising."""
    (base_dir / "projects.yaml").write_text(
        yaml.dump({"projects": {"demo": {"path": "~/dev/demo"}}})
    )
    (base_dir / "roles.yaml").write_text(
        yaml.dump({"roles": [{"name": "planner", "prompt_file": "planner.md"}]})
    )
    (base_dir / "policies.yaml").write_text(yaml.dump({"policies": {}}))
    (base_dir / "groups.yaml").write_text(yaml.dump({"groups": {}}))
    (base_dir / "tasks.yaml").write_text(yaml.dump({"tasks": {}}))
    (base_dir / "pipelines.yaml").write_text(yaml.dump({"pipelines": {}}))
    (base_dir / "prompts" / "agents").mkdir(parents=True)
    (base_dir / "prompts" / "agents" / "planner.md").write_text("# planner")


def _currently_enabled_plugin_stems() -> list[str]:
    """Every `*_enabled` flag that config_doctor treats as plugin-backed
    (i.e. NOT a builtin-runner or non-plugin feature flag) that is
    currently True -- used to build a fake PluginManager.loaded that
    matches whatever this codebase's *_enabled defaults happen to be,
    instead of hardcoding today's default set."""
    from hivepilot.registry import _BUILTIN_RUNNERS
    from hivepilot.services.config_provenance import all_keys

    builtin_stems = frozenset(_BUILTIN_RUNNERS)
    stems = []
    for key in all_keys():
        if not key.endswith("_enabled"):
            continue
        stem = key[: -len("_enabled")]
        if stem in config_doctor._NON_PLUGIN_ENABLED_FLAG_EXCEPTIONS or stem in builtin_stems:
            continue
        if getattr(settings, key, False):
            stems.append(stem)
    return stems


def _clear_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "HIVEPILOT_BASE_DIR",
        "HIVEPILOT_STATE_DB",
        "HIVEPILOT_PROMPTS_DIR",
        "HIVEPILOT_OBSIDIAN_VAULT",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# describe_resolved_paths / check_cwd_relative_paths (incident #1)
# ---------------------------------------------------------------------------


class TestResolvedPaths:
    def test_describe_resolved_paths_are_all_absolute(self) -> None:
        lines = config_doctor.describe_resolved_paths()
        assert lines, "expected at least one path line"
        for line in lines:
            _label, _sep, value = line.partition(":")
            assert Path(value.strip()).is_absolute(), f"non-absolute path line: {line}"

    def test_state_db_path_matches_settings_resolve_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H1 (kills the Goodhart test above): `describe_resolved_paths()`'s
        state-db line must be the SAME path `settings.resolve_path(settings.
        state_db)` produces -- i.e. THE SAME path the pre-existing
        `hivepilot doctor` command prints (cli.py). A wrong path (e.g. one
        built from a hardcoded "state_db" filename instead of the real
        `Path("state.db")` default) is still absolute, which is exactly why
        an `is_absolute()`-only assertion let this ship."""
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", "/srv/hivepilot")

        expected = str(settings.resolve_path(settings.state_db))
        lines = config_doctor.describe_resolved_paths()

        state_db_lines = [line for line in lines if line.strip().startswith("state_db")]
        assert state_db_lines, f"no state_db line in {lines}"
        _label, _sep, value = state_db_lines[0].partition(":")
        assert value.strip() == expected

    def test_obsidian_vault_path_matches_settings_resolve_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H1's other hardcoded-filename bug: 'obsidian_vault' must resolve
        through the real `settings.obsidian_vault` attribute (default
        `Path("obsidian-vault")`), not a hardcoded 'obsidian_vault' filename
        string (which has a different spelling: underscore vs hyphen)."""
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", "/srv/hivepilot")

        expected = str(settings.resolve_path(settings.obsidian_vault))
        lines = config_doctor.describe_resolved_paths()

        vault_lines = [line for line in lines if line.strip().startswith("obsidian_vault")]
        assert vault_lines, f"no obsidian_vault line in {lines}"
        _label, _sep, value = vault_lines[0].partition(":")
        assert value.strip() == expected

    def test_warns_when_base_dir_not_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAILING fixture: no HIVEPILOT_BASE_DIR, no per-field override --
        state_db resolves through settings.base_dir, which is cwd-dependent
        unless pinned. This is incident #1's exact failure mode."""
        _clear_path_env(monkeypatch)

        findings = config_doctor.check_cwd_relative_paths()

        state_db_findings = [f for f in findings if "state_db" in f.message]
        assert state_db_findings, "expected a cwd_relative_path finding for state_db"
        finding = state_db_findings[0]
        assert finding.severity == "warning"
        assert finding.check == "cwd_relative_path"
        assert "HIVEPILOT_BASE_DIR" in finding.fix

    def test_no_warning_when_base_dir_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PASSING fixture: HIVEPILOT_BASE_DIR set -- the documented remedy
        for incident #1 -- suppresses every base_dir-relative warning."""
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", "/srv/hivepilot")

        findings = config_doctor.check_cwd_relative_paths()

        assert findings == []

    def test_no_warning_when_field_explicitly_overridden_absolute(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """PASSING fixture (per-field): an absolute HIVEPILOT_STATE_DB alone
        (no HIVEPILOT_BASE_DIR) is enough to un-flag state_db specifically."""
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "state.db"))

        findings = config_doctor.check_cwd_relative_paths()

        assert not any("state_db" in f.message for f in findings)

    def test_relative_base_dir_still_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """H2: a RELATIVE `HIVEPILOT_BASE_DIR` (e.g. ".") must NOT suppress
        the cwd-relative warning -- `resolve_path` is `(self.base_dir /
        path).resolve()`, so a relative base_dir still anchors every path to
        the process's cwd at startup, which is exactly the failure mode this
        check exists to catch. The old `bool(os.environ.get(...))` check
        fail-OPEN'd on this."""
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", ".")

        findings = config_doctor.check_cwd_relative_paths()

        assert findings, "a relative HIVEPILOT_BASE_DIR must not suppress cwd-relative warnings"
        assert any(f.check == "cwd_relative_path" for f in findings)

    def test_base_dir_pinned_rejects_relative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", ".")
        assert config_doctor._base_dir_pinned() is False

    def test_base_dir_pinned_accepts_absolute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", "/srv/hivepilot")
        assert config_doctor._base_dir_pinned() is True


# ---------------------------------------------------------------------------
# check_sync_drift (incident #3)
# ---------------------------------------------------------------------------


class TestSyncDrift:
    def test_no_config_repo_configured_is_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "config_repo", None, raising=False)
        assert config_doctor.check_sync_drift() == []

    def test_clone_not_yet_cloned_is_clean(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            settings, "config_repo", "https://example.invalid/config.git", raising=False
        )
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        # No clone dir created -- nothing to compare against.
        assert config_doctor.check_sync_drift() == []

    def test_flags_drift_between_clone_and_active(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """FAILING fixture: the clone has a projects.yaml the active (XDG)
        config doesn't match -- exactly incident #3 (edited the clone,
        forgot `config sync`)."""
        monkeypatch.setattr(
            settings, "config_repo", "https://example.invalid/config.git", raising=False
        )
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        clone_dir = tmp_path / "data" / "hivepilot" / "config-repo"
        clone_dir.mkdir(parents=True)
        (clone_dir / "projects.yaml").write_text("projects:\n  new-project:\n    path: /tmp/x\n")

        active_dir = tmp_path / "config" / "hivepilot"
        active_dir.mkdir(parents=True)
        # active has nothing -- clone is strictly ahead.

        findings = config_doctor.check_sync_drift()

        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert findings[0].check == "config_repo_out_of_sync"
        assert "projects.yaml" in findings[0].message
        assert "hivepilot config sync" in findings[0].fix

    def test_clean_when_clone_matches_active(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """PASSING fixture: clone and active are byte-identical."""
        monkeypatch.setattr(
            settings, "config_repo", "https://example.invalid/config.git", raising=False
        )
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        clone_dir = tmp_path / "data" / "hivepilot" / "config-repo"
        clone_dir.mkdir(parents=True)
        (clone_dir / "projects.yaml").write_text("projects: {}\n")

        active_dir = tmp_path / "config" / "hivepilot"
        active_dir.mkdir(parents=True)
        (active_dir / "projects.yaml").write_text("projects: {}\n")

        assert config_doctor.check_sync_drift() == []


# ---------------------------------------------------------------------------
# check_enabled_plugins_loaded (incident #4)
# ---------------------------------------------------------------------------


class TestEnabledPluginsLoaded:
    def test_flags_enabled_but_not_loaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAILING fixture: mem0_enabled=True but PluginManager never loaded
        a 'mem0' plugin record -- incident #4 (env flag flipped, plugin FILE
        missing from the plugins dir; only symptom was an empty panel)."""
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        fake_manager = SimpleNamespace(loaded=[])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        mem0_findings = [f for f in findings if "'mem0'" in f.message]
        assert mem0_findings, "expected a plugin_enabled_not_loaded finding for mem0"
        assert mem0_findings[0].severity == "error"
        assert mem0_findings[0].check == "plugin_enabled_not_loaded"
        assert "plugins install mem0" in mem0_findings[0].fix

    def test_clean_when_enabled_plugin_is_loaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PASSING fixture: the plugin IS in PluginManager.loaded."""
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        fake_manager = SimpleNamespace(loaded=[SimpleNamespace(name="mem0")])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        assert not any("'mem0'" in f.message for f in findings)

    def test_clean_when_flag_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "mem0_enabled", False, raising=False)
        fake_manager = SimpleNamespace(loaded=[])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        assert not any("'mem0'" in f.message for f in findings)

    def test_the_remaining_builtin_runner_flag_is_never_flagged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`openrouter` is the one agent kind still built into the registry —
        its flag must never read as 'enabled but not loaded'."""
        monkeypatch.setattr(settings, "openrouter_enabled", True, raising=False)
        fake_manager = SimpleNamespace(loaded=[])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        assert not any("'openrouter'" in f.message for f in findings)

    def test_claude_enabled_but_not_loaded_IS_flagged_now(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This test used to assert the opposite, under the premise that
        claude was builtin. It is a plugin now, so enabled-but-not-loaded is
        REAL drift information — the herdr.py nine-days-stale family — and
        suppressing it would silence the exact signal the check exists for."""
        monkeypatch.setattr(settings, "claude_enabled", True, raising=False)
        fake_manager = SimpleNamespace(loaded=[])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        assert any("'claude'" in f.message for f in findings)


# ---------------------------------------------------------------------------
# check_enabled_plugins_loaded default-True signal-to-noise fix: on a real
# production box this check produced 19 ERRORs, 17 of which were flags that
# default to True as a PERMISSION GATE (herdr/infisical/kms/onepassword/
# gemini/codex/cursor/hugo/tmux/bitwarden/vaultwarden/opencode/ollama/pi/
# qwen_code/kimi_cli/antigravity), never touched by the operator. Only an
# EXPLICITLY-configured flag (env var / .env file / init kwarg) with a
# missing plugin is a real incident (the mem0/headroom case this check was
# built for) -- these tests pin that distinction.
# ---------------------------------------------------------------------------


def _fresh_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object
) -> Settings:
    """Build a `Settings` instance isolated from the real machine's env/.env
    file and XDG dirs, with a CLEAN `model_fields_set` -- unlike mutating the
    process-wide `settings` singleton via `monkeypatch.setattr(settings,
    ...)` (which permanently adds the field to that singleton's
    `model_fields_set`, even on monkeypatch teardown -- `MonkeyPatch.undo()`
    restores the old VALUE via a plain `setattr`, which pydantic v2 treats
    as an explicit set), this gives each test a provably clean slate for
    provenance assertions with zero cross-test pollution risk."""
    xdg_root = tmp_path / "xdg-home"
    xdg_root.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_root))
    kwargs: dict[str, object] = {"base_dir": tmp_path, "_env_file": None}
    kwargs.update(overrides)
    return Settings(**kwargs)  # type: ignore[arg-type, call-arg]


class _FakeFieldInfo:
    def __init__(self, default: object) -> None:
        self.default = default


class _FallbackProvenanceSettings:
    """Test double with class-level `model_fields` (mirroring pydantic's
    `FieldInfo.default` contract) but NO `model_fields_set` instance
    attribute at all -- a real `Settings` instance always has
    `model_fields_set` (it's a pydantic `BaseSettings`), so this exercises
    `_is_setting_explicit`'s value-vs-default FALLBACK path directly,
    decoupled from any real Settings instance."""

    model_fields = {
        "mem0_enabled": _FakeFieldInfo(default=False),
        "herdr_enabled": _FakeFieldInfo(default=True),
    }

    def __init__(self, *, mem0_enabled: bool, herdr_enabled: bool) -> None:
        self.mem0_enabled = mem0_enabled
        self.herdr_enabled = herdr_enabled


class _AmbiguousProvenanceSettings:
    """Test double simulating 'provenance genuinely unavailable' -- no
    `model_fields_set` AND no class-level `model_fields` at all (e.g. a
    hypothetical future non-pydantic config backend). Only defines the
    handful of attributes `check_enabled_plugins_loaded` actually touches.
    Proves the fail-closed guard: an `*_enabled` flag that's truthy but
    whose provenance can't be determined AT ALL must still produce a
    finding (a warning), never silence -- the recurring
    empty/unknown-treated-as-no-constraint bug class this codebase has hit
    repeatedly."""

    mem0_enabled = True

    def resolve_path(self, path: Path) -> Path:
        return Path("/fake") / path

    xdg_data_home = Path("/fake/xdg-data")


class TestIsSettingExplicit:
    """Direct unit coverage of the provenance helper, independent of
    `check_enabled_plugins_loaded`'s aggregation/severity logic above."""

    def test_env_var_source_is_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HIVEPILOT_MEM0_ENABLED", "true")
        cfg = _fresh_settings(tmp_path, monkeypatch)

        assert config_doctor._is_setting_explicit(cfg, "mem0_enabled") is True

    def test_env_file_source_is_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / "custom.env"
        env_file.write_text("HIVEPILOT_MEM0_ENABLED=true\n")
        cfg = _fresh_settings(tmp_path, monkeypatch, _env_file=str(env_file))

        assert config_doctor._is_setting_explicit(cfg, "mem0_enabled") is True

    def test_untouched_default_true_flag_is_not_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _fresh_settings(tmp_path, monkeypatch)  # herdr_enabled left at its True default

        assert config_doctor._is_setting_explicit(cfg, "herdr_enabled") is False

    def test_fallback_mismatch_is_treated_as_explicit(self) -> None:
        """`model_fields_set` unavailable, but the live value disagrees with
        the class default -- unambiguous evidence of an override."""
        cfg = _FallbackProvenanceSettings(mem0_enabled=True, herdr_enabled=True)

        assert config_doctor._is_setting_explicit(cfg, "mem0_enabled") is True

    def test_fallback_match_is_ambiguous_not_default(self) -> None:
        """`model_fields_set` unavailable AND the live value equals the
        class default -- genuinely ambiguous (could be an explicit override
        that happens to match the default, or truly untouched). Must return
        `None` (unknown), never `False` (confirmed default) -- the caller
        is responsible for treating `None` as fail-closed."""
        cfg = _FallbackProvenanceSettings(mem0_enabled=False, herdr_enabled=True)

        assert config_doctor._is_setting_explicit(cfg, "herdr_enabled") is None

    def test_no_model_fields_at_all_is_unknown(self) -> None:
        cfg = _AmbiguousProvenanceSettings()

        assert config_doctor._is_setting_explicit(cfg, "mem0_enabled") is None


class TestEnabledPluginsLoadedProvenance:
    def test_default_enabled_flag_missing_plugin_yields_info_not_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE 17-false-positive regression: `herdr_enabled` defaults to
        True as a permission gate. Left untouched, with no 'herdr' plugin
        loaded, this must NOT be an ERROR (or any per-plugin finding at
        all) -- at most a single aggregated info line."""
        cfg = _fresh_settings(tmp_path, monkeypatch)
        monkeypatch.setattr(config_doctor, "settings", cfg)
        fake_manager = SimpleNamespace(loaded=[])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        assert not any(
            f.check == "plugin_enabled_not_loaded" and "herdr" in f.message for f in findings
        )
        info_findings = [f for f in findings if f.check == "default_enabled_plugin_not_installed"]
        assert info_findings, "expected one aggregated info finding for default-enabled plugins"
        assert info_findings[0].severity == "info"
        assert "herdr" in info_findings[0].message

    def test_explicit_env_var_enabled_missing_plugin_is_still_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE REAL INCIDENT must stay caught: an operator who explicitly
        set `HIVEPILOT_MEM0_ENABLED=true` via an env var, with the plugin
        FILE missing, must still get an ERROR."""
        monkeypatch.setenv("HIVEPILOT_MEM0_ENABLED", "true")
        cfg = _fresh_settings(tmp_path, monkeypatch)
        monkeypatch.setattr(config_doctor, "settings", cfg)
        fake_manager = SimpleNamespace(loaded=[])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        mem0_findings = [f for f in findings if "'mem0'" in f.message]
        assert mem0_findings, "expected a plugin_enabled_not_loaded ERROR for mem0"
        assert mem0_findings[0].severity == "error"
        assert mem0_findings[0].check == "plugin_enabled_not_loaded"

    def test_explicit_env_file_enabled_missing_plugin_is_still_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same real incident, sourced from a `.env` config file instead of
        a live env var -- must also still be an ERROR."""
        env_file = tmp_path / "custom.env"
        env_file.write_text("HIVEPILOT_MEM0_ENABLED=true\n")
        cfg = _fresh_settings(tmp_path, monkeypatch, _env_file=str(env_file))
        monkeypatch.setattr(config_doctor, "settings", cfg)
        fake_manager = SimpleNamespace(loaded=[])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        mem0_findings = [f for f in findings if "'mem0'" in f.message]
        assert mem0_findings, "expected a plugin_enabled_not_loaded ERROR for mem0"
        assert mem0_findings[0].severity == "error"

    def test_explicit_false_missing_plugin_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _fresh_settings(tmp_path, monkeypatch, herdr_enabled=False)
        monkeypatch.setattr(config_doctor, "settings", cfg)
        fake_manager = SimpleNamespace(loaded=[])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        assert not any("herdr" in f.message for f in findings)

    def test_provenance_unavailable_and_truthy_is_reported_not_silenced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-closed guard: when provenance is genuinely unknown, a
        truthy flag with no matching plugin must still surface (degraded to
        a warning), never disappear silently."""
        monkeypatch.setattr(config_doctor, "settings", _AmbiguousProvenanceSettings())
        fake_manager = SimpleNamespace(loaded=[])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        mem0_findings = [f for f in findings if "'mem0'" in f.message]
        assert mem0_findings, "provenance-unknown + truthy must still emit a finding"
        assert mem0_findings[0].severity == "warning"

    def test_many_default_enabled_flags_yield_zero_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression pin for the real production incident: with a clean
        `Settings()` (all `*_enabled` flags at their class defaults) and NO
        plugins loaded at all, this check must yield ZERO error-severity
        findings -- against current `main` this produces 17 false-positive
        ERRORs (herdr/infisical/kms/onepassword/gemini/codex/cursor/hugo/
        tmux/bitwarden/vaultwarden/opencode/ollama/pi/qwen_code/kimi_cli/
        antigravity)."""
        cfg = _fresh_settings(tmp_path, monkeypatch)
        monkeypatch.setattr(config_doctor, "settings", cfg)
        fake_manager = SimpleNamespace(loaded=[])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        errors = [f for f in findings if f.severity == "error"]
        assert errors == [], f"expected zero ERROR findings, got: {[e.message for e in errors]}"


# ---------------------------------------------------------------------------
# check_plugin_health (incident #5)
# ---------------------------------------------------------------------------


class TestPluginHealth:
    def test_surfaces_error_health_with_pip_fix(self) -> None:
        fake_manager = SimpleNamespace(
            check_all=lambda: {"mem0": ("error", "mem0ai not installed")}
        )

        findings = config_doctor.check_plugin_health(fake_manager)

        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert findings[0].check == "plugin_health"
        assert "mem0" in findings[0].message
        assert "pip install mem0ai" in findings[0].fix

    def test_degraded_is_a_warning_not_an_error(self) -> None:
        fake_manager = SimpleNamespace(
            check_all=lambda: {"mem0": ("degraded", "installed but disabled (mem0_enabled=False)")}
        )

        findings = config_doctor.check_plugin_health(fake_manager)

        assert len(findings) == 1
        assert findings[0].severity == "warning"

    def test_clean_when_all_healthy(self) -> None:
        fake_manager = SimpleNamespace(check_all=lambda: {"rtk": ("ok", "rtk found on PATH")})

        assert config_doctor.check_plugin_health(fake_manager) == []


# ---------------------------------------------------------------------------
# fix/retry-queue-drain: retry_queue abnormal-backlog check. Real incident:
# 197 `groomer-scan` retries sat PENDING and past-due for 7 days -- nothing
# drained them, and `hivepilot schedule health` printed the raw count but
# never flagged it as abnormal. This check closes that gap in `config
# doctor`, deliberately conservative (per the check_enabled_plugins_loaded
# 17-false-positives lesson): a small, normal backlog waiting out its own
# backoff window must produce NO finding.
# ---------------------------------------------------------------------------


class TestRetryQueueBacklog:
    @pytest.fixture(autouse=True)
    def isolated_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        from hivepilot.services import state_service

        db_path = tmp_path / "doctor_retry.db"
        monkeypatch.setattr(state_service, "DB_PATH", db_path)
        return db_path

    def _insert(self, db_path: Path, **kwargs: object) -> None:
        import sqlite3

        from hivepilot.services import state_service

        state_service.init_db()
        defaults = {
            "schedule_name": "groomer",
            "task": "groomer-scan",
            "projects": "[]",
            "error": "[Errno 2] No such file or directory: '/root/noxys'",
            "attempt": 1,
            "max_attempts": 3,
            "status": "pending",
            "next_retry_at": "2020-01-01T00:00:00+00:00",
        }
        defaults.update(kwargs)
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO retry_queue "
                "(schedule_name, task, projects, error, attempt, max_attempts, status, "
                "next_retry_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(defaults.values()),
            )
            conn.commit()

    def test_no_findings_when_queue_empty(self, isolated_db: Path) -> None:
        assert config_doctor.check_retry_queue_backlog() == []

    def test_no_finding_for_a_row_still_within_its_own_backoff_window(
        self, isolated_db: Path
    ) -> None:
        """A row due only a few minutes ago (well under the stale-after
        threshold) is NORMAL -- must produce no finding at all."""
        from datetime import datetime, timedelta, timezone

        recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        self._insert(isolated_db, next_retry_at=recent)

        assert config_doctor.check_retry_queue_backlog() == []

    def test_fires_warning_on_abnormal_backlog(self, isolated_db: Path) -> None:
        """A handful of rows overdue by well over the stale-after threshold
        (default 24h) IS the incident shape -- must fire."""
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        for i in range(3):
            self._insert(isolated_db, schedule_name=f"groomer-{i}", next_retry_at=old)

        findings = config_doctor.check_retry_queue_backlog()

        assert len(findings) == 1
        assert findings[0].check == "retry_queue_backlog"
        assert findings[0].severity == "warning"
        assert "3" in findings[0].message

    def test_escalates_to_error_on_large_backlog(self, isolated_db: Path) -> None:
        """The real incident had 197 rows -- well past the default
        error-count threshold (20) -- must escalate to 'error'."""
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        for i in range(25):
            self._insert(isolated_db, schedule_name=f"groomer-{i}", next_retry_at=old)

        findings = config_doctor.check_retry_queue_backlog()

        assert len(findings) == 1
        assert findings[0].severity == "error"

    def test_unparseable_timestamp_is_its_own_finding_never_silent(self, isolated_db: Path) -> None:
        """'I could not inspect this' must be a finding, never silence."""
        self._insert(isolated_db, next_retry_at="not-a-timestamp")

        findings = config_doctor.check_retry_queue_backlog()

        assert len(findings) == 1
        assert findings[0].check == "retry_queue_unparseable_timestamp"
        assert findings[0].severity == "error"

    def test_running_and_dead_rows_never_counted(self, isolated_db: Path) -> None:
        """Only PENDING rows count toward the backlog -- rows already
        claimed ('running') or exhausted ('dead') are not 'stuck'."""
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        self._insert(isolated_db, status="running", next_retry_at=old)
        self._insert(isolated_db, status="dead", next_retry_at=old)

        assert config_doctor.check_retry_queue_backlog() == []

    def test_wired_into_run_doctor(self, isolated_db: Path) -> None:
        """A check that exists but is never registered in `run_doctor()` is
        exactly as invisible as no check at all -- assert it's reachable
        through the real entry point, not just directly callable."""
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        for i in range(3):
            self._insert(isolated_db, schedule_name=f"groomer-{i}", next_retry_at=old)

        fake_manager = SimpleNamespace(
            loaded=[SimpleNamespace(name=stem) for stem in _currently_enabled_plugin_stems()],
            check_all=lambda: {},
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)
            findings = config_doctor.run_doctor(config_dir=None)

        assert any(f.check == "retry_queue_backlog" for f in findings)


# ---------------------------------------------------------------------------
# Dangling references (incident #7 + #6's alias variant)
# ---------------------------------------------------------------------------


class TestSchedulesDangling:
    def test_flags_unknown_task_and_project(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {"real-proj": {}}}))
        (tmp_path / "tasks.yaml").write_text(yaml.dump({"tasks": {"real-task": {}}}))
        (tmp_path / "schedules.yaml").write_text(
            yaml.dump(
                {
                    "schedules": {
                        "nightly": {
                            "task": "ghost-task",
                            "projects": ["ghost-project"],
                        }
                    }
                }
            )
        )

        findings = config_doctor._check_schedules_dangling(tmp_path)

        checks = {f.check for f in findings}
        assert "dangling_schedule_task" in checks
        assert "dangling_schedule_project" in checks
        task_finding = next(f for f in findings if f.check == "dangling_schedule_task")
        assert "ghost-task" in task_finding.message
        assert "tasks.yaml" in task_finding.fix

    def test_clean_when_references_are_valid(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {"real-proj": {}}}))
        (tmp_path / "tasks.yaml").write_text(yaml.dump({"tasks": {"real-task": {}}}))
        (tmp_path / "schedules.yaml").write_text(
            yaml.dump({"schedules": {"nightly": {"task": "real-task", "projects": ["real-proj"]}}})
        )

        assert config_doctor._check_schedules_dangling(tmp_path) == []

    def test_malformed_schedules_yaml_yields_finding_not_silence(self, tmp_path: Path) -> None:
        """H3: schedules.yaml is NOT in validate_config's required_files, so
        NOTHING else covers it -- a YAML syntax error here used to collapse
        to `{}` (zero schedules) and zero findings: a clean bill of health
        for a file that was never actually read."""
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {"real-proj": {}}}))
        (tmp_path / "tasks.yaml").write_text(yaml.dump({"tasks": {"real-task": {}}}))
        (tmp_path / "schedules.yaml").write_text("schedules: [unclosed\n")

        findings = config_doctor._check_schedules_dangling(tmp_path)

        assert findings, "expected an emitted finding for unparseable YAML, never silence"
        assert any(f.check == "unparseable_config_yaml" for f in findings)
        assert findings[0].severity == "error"

    def test_non_dict_schedules_root_yields_finding_not_crash(self, tmp_path: Path) -> None:
        """M1: a parseable-but-non-mapping root (e.g. a YAML list) must not
        crash on the first `.get(...)` call, and must not be silently
        collapsed into `{}` either."""
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        (tmp_path / "tasks.yaml").write_text(yaml.dump({"tasks": {}}))
        (tmp_path / "schedules.yaml").write_text(yaml.dump(["not", "a", "mapping"]))

        findings = config_doctor._check_schedules_dangling(tmp_path)

        assert any(f.check == "invalid_config_yaml_root" for f in findings)

    def test_non_mapping_schedule_entry_yields_finding_not_silence(self, tmp_path: Path) -> None:
        """M3: a schedule entry that isn't a mapping (a YAML typo like
        `nightly: "my-task"` instead of `nightly: {task: my-task}`) used to
        be skipped with ZERO output."""
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        (tmp_path / "tasks.yaml").write_text(yaml.dump({"tasks": {}}))
        (tmp_path / "schedules.yaml").write_text(yaml.dump({"schedules": {"nightly": "my-task"}}))

        findings = config_doctor._check_schedules_dangling(tmp_path)

        assert any(
            f.check == "malformed_schedule_entry" and "nightly" in f.message for f in findings
        )


class TestRoleOverridesDangling:
    def test_flags_alias_used_instead_of_real_role_key(self, tmp_path: Path) -> None:
        """FAILING fixture: incident #6 -- 'cos' is a Telegram command alias
        for 'chief_of_staff', never a real roles.yaml key."""
        (tmp_path / "roles.yaml").write_text(
            yaml.dump({"roles": [{"name": "chief_of_staff", "prompt_file": "cos.md"}]})
        )
        (tmp_path / "policies.yaml").write_text(
            yaml.dump({"policies": {"default": {"role_overrides": {"cos": {"model": "glm"}}}}})
        )

        findings = config_doctor._check_role_overrides_dangling(tmp_path)

        assert len(findings) == 1
        assert findings[0].check == "role_alias_used_as_role_key"
        assert "cos" in findings[0].message
        assert "chief_of_staff" in findings[0].fix

    def test_flags_dangling_role_override_not_an_alias(self, tmp_path: Path) -> None:
        (tmp_path / "roles.yaml").write_text(
            yaml.dump({"roles": [{"name": "chief_of_staff", "prompt_file": "cos.md"}]})
        )
        (tmp_path / "policies.yaml").write_text(
            yaml.dump({"policies": {"default": {"role_overrides": {"totally_bogus": {}}}}})
        )

        findings = config_doctor._check_role_overrides_dangling(tmp_path)

        assert len(findings) == 1
        assert findings[0].check == "dangling_role_override"
        assert "totally_bogus" in findings[0].message

    def test_clean_when_role_override_is_real(self, tmp_path: Path) -> None:
        (tmp_path / "roles.yaml").write_text(
            yaml.dump({"roles": [{"name": "chief_of_staff", "prompt_file": "cos.md"}]})
        )
        (tmp_path / "policies.yaml").write_text(
            yaml.dump({"policies": {"default": {"role_overrides": {"chief_of_staff": {}}}}})
        )

        assert config_doctor._check_role_overrides_dangling(tmp_path) == []

    def test_non_mapping_policy_scope_yields_finding_not_silence(self, tmp_path: Path) -> None:
        """M3: a policy scope that isn't a mapping used to be skipped with
        ZERO output for every rule under it (role_overrides included)."""
        (tmp_path / "roles.yaml").write_text(
            yaml.dump({"roles": [{"name": "chief_of_staff", "prompt_file": "cos.md"}]})
        )
        (tmp_path / "policies.yaml").write_text(
            yaml.dump({"policies": {"default": "not-a-mapping"}})
        )

        findings = config_doctor._check_role_overrides_dangling(tmp_path)

        assert any(f.check == "malformed_policy_entry" for f in findings)

    def test_malformed_roles_yaml_yields_finding_not_silence(self, tmp_path: Path) -> None:
        """H3: an unparseable roles.yaml must surface a finding, not
        silently resolve to zero known roles (which would make EVERY
        role_override look dangling for the wrong reason)."""
        (tmp_path / "roles.yaml").write_text("roles: [unclosed\n")
        (tmp_path / "policies.yaml").write_text(yaml.dump({"policies": {}}))

        findings = config_doctor._check_role_overrides_dangling(tmp_path)

        assert any(f.check == "unparseable_config_yaml" for f in findings)


# ---------------------------------------------------------------------------
# check_role_display_name_collisions -- incident: five roles (designer_console,
# designer_extension, designer_vscode, designer_agent, design_reviewer) all
# carried display_name "Margaux"; the Telegram agent registry derives its
# addressing alias from display_name, so four of the five became
# unaddressable by name. The engine already logs
# 'telegram.agent_registry.alias_collision' at startup, but nobody reads
# startup logs -- this is the silent-degradation gap `config doctor` exists
# to close.
# ---------------------------------------------------------------------------


class TestRoleDisplayNameCollisions:
    def test_five_roles_sharing_display_name_flags_one_error_naming_all(
        self, tmp_path: Path
    ) -> None:
        """FAILING fixture reproducing the real incident: against current
        origin/main (no such check exists yet), `check_role_display_name_
        collisions` doesn't even exist."""
        role_names = [
            "designer_console",
            "designer_extension",
            "designer_vscode",
            "designer_agent",
            "design_reviewer",
        ]
        roles = [{"name": name, "display_name": "Margaux"} for name in role_names]
        (tmp_path / "roles.yaml").write_text(yaml.dump({"roles": roles}))

        findings = config_doctor.check_role_display_name_collisions(tmp_path)

        assert len(findings) == 1
        finding = findings[0]
        assert finding.severity == "error"
        assert finding.check == "duplicate_role_display_name"
        for name in role_names:
            assert name in finding.message
        assert "unaddressable by name" in finding.why
        assert "mentions resolve to only one of them" in finding.why

    def test_case_and_whitespace_variants_collide(self, tmp_path: Path) -> None:
        """'Margaux' and 'margaux ' must collide -- case-insensitive and
        trimmed, matching the real alias derivation's normalisation."""
        roles = [
            {"name": "designer_console", "display_name": "Margaux"},
            {"name": "designer_extension", "display_name": "margaux "},
        ]
        (tmp_path / "roles.yaml").write_text(yaml.dump({"roles": roles}))

        findings = config_doctor.check_role_display_name_collisions(tmp_path)

        assert len(findings) == 1
        assert findings[0].check == "duplicate_role_display_name"
        assert "designer_console" in findings[0].message
        assert "designer_extension" in findings[0].message

    def test_all_distinct_display_names_yields_no_findings(self, tmp_path: Path) -> None:
        """PASSING fixture: every role has a genuinely unique display_name."""
        roles = [
            {"name": "ceo", "display_name": "Aliénor"},
            {"name": "cto", "display_name": "Blaise"},
            {"name": "developer", "display_name": "Gustave"},
            {"name": "reviewer", "display_name": "Victor"},
        ]
        (tmp_path / "roles.yaml").write_text(yaml.dump({"roles": roles}))

        assert config_doctor.check_role_display_name_collisions(tmp_path) == []

    @pytest.mark.parametrize("blank_value", ["", "   "])
    def test_blank_display_name_yields_finding(self, tmp_path: Path, blank_value: str) -> None:
        """An empty or whitespace-only display_name would make the role
        unaddressable by name (whitespace-only would even crash the real
        Telegram agent-registry construction: `display_name.split()[0]`
        raises IndexError on a string with no tokens)."""
        roles = [{"name": "solo_role", "display_name": blank_value}]
        (tmp_path / "roles.yaml").write_text(yaml.dump({"roles": roles}))

        findings = config_doctor.check_role_display_name_collisions(tmp_path)

        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert findings[0].check == "blank_role_display_name"
        assert "solo_role" in findings[0].message

    def test_malformed_roles_yaml_yields_finding_not_crash(self, tmp_path: Path) -> None:
        """H3: an unparseable roles.yaml must surface a finding, never
        silently resolve to zero findings and never raise."""
        (tmp_path / "roles.yaml").write_text("roles: [unclosed\n")

        findings = config_doctor.check_role_display_name_collisions(tmp_path)

        assert any(f.check == "unparseable_config_yaml" for f in findings)

    def test_non_mapping_root_yields_finding_not_crash(self, tmp_path: Path) -> None:
        """A roles.yaml whose top-level document is a bare list (not a
        mapping) must surface a finding, never raise AttributeError."""
        (tmp_path / "roles.yaml").write_text(yaml.dump(["not", "a", "mapping"]))

        findings = config_doctor.check_role_display_name_collisions(tmp_path)

        assert any(f.check == "invalid_config_yaml_root" for f in findings)

    def test_roles_section_as_list_is_handled(self, tmp_path: Path) -> None:
        """The canonical, engine-loaded shape: `roles:` is a LIST of role
        mappings (see docs/CONFIGURATION.md and hivepilot/roles.py)."""
        roles = [
            {"name": "role_a", "display_name": "Same"},
            {"name": "role_b", "display_name": "Same"},
        ]
        (tmp_path / "roles.yaml").write_text(yaml.dump({"roles": roles}))

        findings = config_doctor.check_role_display_name_collisions(tmp_path)

        assert any(f.check == "duplicate_role_display_name" for f in findings)

    def test_roles_section_as_mapping_is_handled(self, tmp_path: Path) -> None:
        """An alternate, hand-edited shape: `roles:` is a MAPPING keyed by
        role name instead of a list -- must be handled, not crash."""
        roles = {
            "role_a": {"display_name": "Same"},
            "role_b": {"display_name": "Same"},
        }
        (tmp_path / "roles.yaml").write_text(yaml.dump({"roles": roles}))

        findings = config_doctor.check_role_display_name_collisions(tmp_path)

        assert any(f.check == "duplicate_role_display_name" for f in findings)

    def test_malformed_role_entry_in_list_yields_finding_not_crash(self, tmp_path: Path) -> None:
        (tmp_path / "roles.yaml").write_text(
            yaml.dump({"roles": ["not-a-mapping", {"name": "chief_of_staff"}]})
        )

        findings = config_doctor.check_role_display_name_collisions(tmp_path)

        assert any(f.check == "malformed_role_entry" for f in findings)

    def test_absent_roles_file_yields_no_findings(self, tmp_path: Path) -> None:
        """A missing roles.yaml is an already-covered case elsewhere
        (validate_config's required_files) -- this check must not crash or
        invent a finding for it."""
        assert config_doctor.check_role_display_name_collisions(tmp_path) == []

    # -----------------------------------------------------------------------
    # The check must SHARE the registry's alias derivation, not reimplement
    # it -- the real incident: the OLD check compared whole `display_name`
    # strings, so "Margaux" and "Margaux (Console)" looked distinct to the
    # doctor while the Telegram registry's real alias derivation (first
    # token of display_name) collided them. A check that disagrees with the
    # mechanism it guards is worse than no check.
    # -----------------------------------------------------------------------

    def test_doctor_shares_engine_derivation_function_not_a_reimplementation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proven by patching `telegram_bot._display_name_alias_claims`
        itself and asserting the doctor's finding is driven by what THAT
        function reports -- not by any doctor-local string comparison.
        FAILS against unfixed origin/main: `_display_name_alias_claims`
        doesn't exist yet, so there is nothing to patch."""
        from hivepilot.services import telegram_bot

        roles = [
            {"name": "role_a", "display_name": "Alpha"},
            {"name": "role_b", "display_name": "Beta"},
        ]
        (tmp_path / "roles.yaml").write_text(yaml.dump({"roles": roles}))

        def fake_claims(display_names: dict[str, str]) -> list[tuple[str, str]]:
            assert display_names == {"role_a": "Alpha", "role_b": "Beta"}
            # A made-up alias that bears no resemblance to a whole-string
            # comparison of "Alpha"/"Beta" -- if the doctor's finding
            # reflects THIS alias, it can only have come from calling this
            # function, not from reimplementing its own normalisation.
            return [("shared_fake_alias", "role_a"), ("shared_fake_alias", "role_b")]

        monkeypatch.setattr(telegram_bot, "_display_name_alias_claims", fake_claims)

        findings = config_doctor.check_role_display_name_collisions(tmp_path)

        assert len(findings) == 1
        assert findings[0].check == "duplicate_role_display_name"
        assert "role_a" in findings[0].message
        assert "role_b" in findings[0].message
        assert "shared_fake_alias" in findings[0].message

    def test_structured_display_names_engine_and_doctor_now_agree(self, tmp_path: Path) -> None:
        """End-to-end with the REAL (unmocked) shared derivation, using the
        exact real-incident display names: since the registry now derives a
        distinct alias for each of the five, the doctor correctly reports
        NO collision either -- registry and doctor agree, instead of the
        doctor silently certifying a broken state."""
        roles = [
            {"name": "designer_plain", "display_name": "Margaux"},
            {"name": "designer_console", "display_name": "Margaux (Console)"},
            {"name": "designer_extension", "display_name": "Margaux (Extension)"},
            {"name": "designer_vscode", "display_name": "Margaux (VS Code)"},
            {"name": "designer_agent", "display_name": "Margaux (Agent)"},
        ]
        (tmp_path / "roles.yaml").write_text(yaml.dump({"roles": roles}))

        assert config_doctor.check_role_display_name_collisions(tmp_path) == []

    def test_display_name_sanitising_to_empty_is_reported_not_skipped(self, tmp_path: Path) -> None:
        """A display_name made entirely of punctuation (e.g. "!!!")
        sanitises to an empty alias -- the registry's `_claim` silently
        no-ops on an empty alias (its early return), so this role gets NO
        display-name-derived alias at all, with zero warning at startup.
        Previously this fell through the check's `if not normalised:
        continue` branch with NO finding whatsoever (fail-open on an empty
        value -- the exact recurring bug class this repo tracks). FAILS
        against unfixed origin/main: zero findings emitted for this
        fixture."""
        roles = [{"name": "ghost_named_role", "display_name": "!!!"}]
        (tmp_path / "roles.yaml").write_text(yaml.dump({"roles": roles}))

        findings = config_doctor.check_role_display_name_collisions(tmp_path)

        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert findings[0].check == "unusable_role_display_name"
        assert "ghost_named_role" in findings[0].message

    def test_display_name_sanitising_to_empty_is_not_confused_with_blank(
        self, tmp_path: Path
    ) -> None:
        """A punctuation-only display_name is a DIFFERENT finding kind from
        a blank one -- distinct operator-facing consequence (blank crashes
        the registry outright; sanitises-to-empty just silently drops the
        alias) and a distinct fix (blank must be non-empty; this one needs
        at least one letter/digit)."""
        roles = [
            {"name": "blank_role", "display_name": "   "},
            {"name": "unusable_role", "display_name": "###"},
        ]
        (tmp_path / "roles.yaml").write_text(yaml.dump({"roles": roles}))

        findings = config_doctor.check_role_display_name_collisions(tmp_path)

        checks = {f.check for f in findings}
        assert checks == {"blank_role_display_name", "unusable_role_display_name"}


class TestOnlyModulesDangling:
    def test_flags_module_not_defined_anywhere(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(
            yaml.dump({"projects": {"acme": {"modules": {"real-module": "apps/real"}}}})
        )
        (tmp_path / "pipelines.yaml").write_text(
            yaml.dump(
                {
                    "pipelines": {
                        "default": {"stages": [{"name": "dev", "only_modules": ["ghost-module"]}]}
                    }
                }
            )
        )

        findings = config_doctor._check_only_modules_dangling(tmp_path)

        assert len(findings) == 1
        assert findings[0].check == "dangling_only_module"
        assert "ghost-module" in findings[0].message

    def test_clean_when_module_is_defined(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(
            yaml.dump({"projects": {"acme": {"modules": {"real-module": "apps/real"}}}})
        )
        (tmp_path / "pipelines.yaml").write_text(
            yaml.dump(
                {
                    "pipelines": {
                        "default": {"stages": [{"name": "dev", "only_modules": ["real-module"]}]}
                    }
                }
            )
        )

        assert config_doctor._check_only_modules_dangling(tmp_path) == []

    def test_non_mapping_pipeline_entry_yields_finding_not_crash(self, tmp_path: Path) -> None:
        """M2: its sibling `_check_schedules_dangling` already guards
        against a non-mapping entry; unguarded here, `pipeline.get("stages")`
        raised AttributeError on a scalar pipeline and crashed the whole
        doctor report instead of reporting just this one problem."""
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        (tmp_path / "pipelines.yaml").write_text(
            yaml.dump({"pipelines": {"broken": "not-a-mapping"}})
        )

        findings = config_doctor._check_only_modules_dangling(tmp_path)

        assert any(f.check == "malformed_pipeline_entry" for f in findings)

    def test_non_mapping_stage_entry_yields_finding_not_crash(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        (tmp_path / "pipelines.yaml").write_text(
            yaml.dump({"pipelines": {"default": {"stages": ["not-a-mapping"]}}})
        )

        findings = config_doctor._check_only_modules_dangling(tmp_path)

        assert any(f.check == "malformed_stage_entry" for f in findings)


class TestDanglingReferencesIntegration:
    def test_clean_full_config_yields_no_dangling_findings(self, tmp_path: Path) -> None:
        _write_minimal_valid_config(tmp_path)
        (tmp_path / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        assert config_doctor.check_dangling_references(tmp_path) == []

    def test_malformed_projects_yaml_yields_finding_not_traceback(self, tmp_path: Path) -> None:
        """M4: `validate_config` re-raises a YAML parse error as
        ValueError -- letting it propagate would give a raw traceback AND
        lose every finding already computed by the other checks in the same
        doctor run. Must degrade to a single named finding instead."""
        (tmp_path / "projects.yaml").write_text("projects: [unclosed\n")
        (tmp_path / "roles.yaml").write_text(yaml.dump({"roles": []}))
        (tmp_path / "policies.yaml").write_text(yaml.dump({"policies": {}}))
        (tmp_path / "groups.yaml").write_text(yaml.dump({"groups": {}}))
        (tmp_path / "tasks.yaml").write_text(yaml.dump({"tasks": {}}))
        (tmp_path / "pipelines.yaml").write_text(yaml.dump({"pipelines": {}}))

        findings = config_doctor.check_dangling_references(tmp_path)

        assert findings, "expected at least one finding, never silence, for unparseable YAML"
        assert any(f.check == "dangling_reference_check_failed" for f in findings)
        failure_finding = next(f for f in findings if f.check == "dangling_reference_check_failed")
        assert failure_finding.severity == "error"
        assert "ValueError" in failure_finding.message
        # Never leak the raw exception message (may embed file contents).
        assert "unclosed" not in failure_finding.message


class TestDanglingTaskStepPromptFileIntegration:
    """Real incident: an operator's tasks.yaml referenced a `prompt_file`
    that did not exist anywhere on the box; `hivepilot config doctor`
    reported zero findings. `check_dangling_references` wraps EVERY
    `validate_config()` problem (the same bridge that already surfaces a
    role's dangling `prompt_file`, a pipeline's unknown task, etc.) with
    this module's WHAT/WHY/FIX shape -- so a new dangling task-step
    `prompt_file` problem string needs no separate doctor-side check to
    show up here."""

    def test_missing_task_step_prompt_file_is_reported(self, tmp_path: Path) -> None:
        _write_minimal_valid_config(tmp_path)
        (tmp_path / "tasks.yaml").write_text(
            yaml.dump(
                {
                    "tasks": {
                        "pentest": {
                            "steps": [
                                {
                                    "name": "security review",
                                    "runner": "claude",
                                    "prompt_file": "security_review.md",
                                }
                            ]
                        }
                    }
                }
            )
        )

        findings = config_doctor.check_dangling_references(tmp_path)

        assert findings, "expected a finding for the dangling task-step prompt_file"
        matching = [f for f in findings if "security_review.md" in f.message]
        assert matching, f"Expected a prompt_file finding, got: {[f.message for f in findings]}"
        assert any("pentest" in f.message and "security review" in f.message for f in matching), (
            matching
        )
        assert any("searched" in f.message for f in matching), matching
        for f in matching:
            assert f.severity == "error"
            assert f.why, "finding must carry a WHY"
            assert f.fix, "finding must carry a FIX"

    def test_resolvable_task_step_prompt_file_is_clean(self, tmp_path: Path) -> None:
        _write_minimal_valid_config(tmp_path)
        (tmp_path / "security_review.md").write_text("# security review")
        (tmp_path / "tasks.yaml").write_text(
            yaml.dump(
                {
                    "tasks": {
                        "pentest": {
                            "steps": [
                                {
                                    "name": "security review",
                                    "runner": "claude",
                                    "prompt_file": "security_review.md",
                                }
                            ]
                        }
                    }
                }
            )
        )

        assert config_doctor.check_dangling_references(tmp_path) == []


# ---------------------------------------------------------------------------
# check_secrets_sanity
# ---------------------------------------------------------------------------


class TestSecretsSanity:
    def test_flags_empty_secret_setting(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        monkeypatch.setattr(settings, "linear_api_key", "", raising=False)

        findings = config_doctor.check_secrets_sanity(tmp_path)

        empty_findings = [f for f in findings if f.check == "empty_secret_setting"]
        assert empty_findings, "expected an empty_secret_setting finding"
        assert "linear_api_key" in empty_findings[0].message

    def test_clean_when_secret_setting_is_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        monkeypatch.setattr(settings, "linear_api_key", None, raising=False)

        findings = config_doctor.check_secrets_sanity(tmp_path)

        assert not any(f.check == "empty_secret_setting" for f in findings)

    def test_flags_dangling_secret_ref(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(
            yaml.dump(
                {
                    "projects": {
                        "acme": {
                            "env": {"API_KEY": "${secret:missing_ref}"},
                            "secrets": {},
                        }
                    }
                }
            )
        )

        findings = config_doctor.check_secrets_sanity(tmp_path)

        ref_findings = [f for f in findings if f.check == "dangling_secret_ref"]
        assert ref_findings, "expected a dangling_secret_ref finding"
        assert "missing_ref" in ref_findings[0].message

    def test_clean_when_secret_ref_has_catalog_entry(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(
            yaml.dump(
                {
                    "projects": {
                        "acme": {
                            "env": {"API_KEY": "${secret:present_ref}"},
                            "secrets": {"present_ref": {"source": "env", "key": "SOME_VAR"}},
                        }
                    }
                }
            )
        )

        findings = config_doctor.check_secrets_sanity(tmp_path)

        assert not any(f.check == "dangling_secret_ref" for f in findings)

    def test_flags_whitespace_only_secret_setting(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """M7: `raw == ""` missed a whitespace-only value -- but
        `forges/provider.py`, `swarm_service.py`, and `config_provenance.py`
        all guard secret values with `.strip()`, not a bare `== ""`."""
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        monkeypatch.setattr(settings, "linear_api_key", "   ", raising=False)

        findings = config_doctor.check_secrets_sanity(tmp_path)

        empty_findings = [f for f in findings if f.check == "empty_secret_setting"]
        assert empty_findings, "expected an empty_secret_setting finding for a whitespace value"
        assert "linear_api_key" in empty_findings[0].message

    def test_non_mapping_project_entry_yields_finding_not_silence(self, tmp_path: Path) -> None:
        """M3: a project entry that isn't a mapping used to be skipped with
        ZERO output for its secrets/env sanity checks."""
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {"acme": "not-a-mapping"}}))

        findings = config_doctor.check_secrets_sanity(tmp_path)

        assert any(f.check == "malformed_project_entry" for f in findings)

    def test_non_mapping_project_env_yields_finding_not_silence(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(
            yaml.dump({"projects": {"acme": {"env": ["not", "a", "mapping"], "secrets": {}}}})
        )

        findings = config_doctor.check_secrets_sanity(tmp_path)

        assert any(f.check == "malformed_project_env" for f in findings)

    def test_malformed_projects_yaml_yields_finding_not_silence(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text("projects: [unclosed\n")

        findings = config_doctor.check_secrets_sanity(tmp_path)

        assert any(f.check == "unparseable_config_yaml" for f in findings)

    def test_flags_dangling_swarm_secret_ref(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """M6: `swarm_service.resolve_swarm_signing_key` degrades an
        unresolvable ${secret:NAME} reference in `swarm_key` to `None`
        (signing silently disabled) rather than raising -- exactly the
        "silent until it matters" state this doctor exists to surface."""
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        monkeypatch.setattr(settings, "swarm_key", "${secret:swarm_signing_key}", raising=False)
        monkeypatch.setattr(settings, "swarm_secrets", {}, raising=False)

        findings = config_doctor.check_secrets_sanity(tmp_path)

        assert any(f.check == "dangling_swarm_secret_ref" for f in findings)

    def test_clean_when_swarm_secret_ref_has_catalog_entry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        monkeypatch.setattr(settings, "swarm_key", "${secret:swarm_signing_key}", raising=False)
        monkeypatch.setattr(
            settings,
            "swarm_secrets",
            {"swarm_signing_key": {"source": "env", "key": "SWARM_KEY"}},
            raising=False,
        )

        findings = config_doctor.check_secrets_sanity(tmp_path)

        assert not any(f.check == "dangling_swarm_secret_ref" for f in findings)

    def test_clean_when_swarm_key_has_no_secret_ref(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        monkeypatch.setattr(settings, "swarm_key", "a-plain-literal-key", raising=False)

        findings = config_doctor.check_secrets_sanity(tmp_path)

        assert not any(f.check == "dangling_swarm_secret_ref" for f in findings)


# ---------------------------------------------------------------------------
# check_cost_accounting (usage-capture-modelusage fix) -- fires on an
# abnormal share of unpriced steps, or a recorded model id absent from the
# price map. Two rules apply: (1) "I could not inspect this" must produce a
# finding, never silence; (2) no noise -- an earlier check (#4b) emitted 17
# false positives out of 19 and the operator stopped reading it, so this
# check is sample-size-gated and excludes the "unknown" (NULL-model, e.g.
# shell-runner) bucket entirely -- that's a legitimate "cost doesn't apply"
# case, never an anomaly.
# ---------------------------------------------------------------------------


def _seed_cost_step(
    run_id: int,
    step: str,
    *,
    model: str | None,
    cost_usd: float | None = None,
    input_tokens: int | None = 100,
    output_tokens: int | None = 100,
) -> None:
    from hivepilot.services import db, state_service

    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                "INSERT INTO steps (run_id, step, status, provider, model, "
                "input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (run_id, step, "success", "claude", model, input_tokens, output_tokens, cost_usd),
        )


def _seed_cost_run() -> int:
    from hivepilot.services import db, state_service

    state_service.init_db()
    with db.connect() as conn:
        return db.insert_returning_id(
            conn,
            "INSERT INTO runs (project, task, status, tenant) VALUES (?, ?, ?, ?)",
            ("proj", "task", "success", "default"),
        )


class TestCheckCostAccounting:
    def test_silent_on_empty_db(self) -> None:
        assert config_doctor.check_cost_accounting() == []

    def test_silent_below_minimum_sample_size(self) -> None:
        """A handful of unpriced steps for a brand-new install must not fire
        -- not enough data to call it an anomaly (anti-noise rule)."""
        run_id = _seed_cost_run()
        for i in range(3):
            _seed_cost_step(run_id, f"s{i}", model="totally-unlisted-model", cost_usd=None)

        assert config_doctor.check_cost_accounting() == []

    def test_fires_on_abnormal_unpriced_share(self) -> None:
        run_id = _seed_cost_run()
        for i in range(20):
            _seed_cost_step(run_id, f"s{i}", model="totally-unlisted-model", cost_usd=None)

        findings = config_doctor.check_cost_accounting()
        assert any(f.check == "cost_accounting_unpriced_share" for f in findings)

    def test_silent_when_fully_priced(self) -> None:
        run_id = _seed_cost_run()
        for i in range(20):
            _seed_cost_step(run_id, f"s{i}", model="claude-sonnet-4-6", cost_usd=0.01)

        assert config_doctor.check_cost_accounting() == []

    def test_silent_when_only_unknown_model_bucket_is_unpriced(self) -> None:
        """NULL-model steps (e.g. a shell runner) never self-report cost --
        that's expected, not an anomaly, and must never inflate the share."""
        run_id = _seed_cost_run()
        for i in range(20):
            _seed_cost_step(run_id, f"s{i}", model=None, cost_usd=None)

        assert config_doctor.check_cost_accounting() == []

    def test_fires_on_model_missing_from_price_map_even_with_low_overall_share(self) -> None:
        """A specific model that has NEVER been priced must be surfaced even
        when it's a small fraction of overall (well-priced) traffic."""
        run_id = _seed_cost_run()
        for i in range(30):
            _seed_cost_step(run_id, f"priced-{i}", model="claude-sonnet-4-6", cost_usd=0.01)
        for i in range(5):
            _seed_cost_step(run_id, f"unpriced-{i}", model="totally-unlisted-model", cost_usd=None)

        findings = config_doctor.check_cost_accounting()
        model_findings = [
            f for f in findings if f.check == "cost_accounting_model_missing_from_price_map"
        ]
        assert model_findings, [f.message for f in findings]
        assert "totally-unlisted-model" in model_findings[0].message

    def test_silent_for_model_absent_from_price_map_but_self_reporting_cost(self) -> None:
        """A model absent from the static price map is harmless as long as
        it self-reports cost_usd every time -- the price map is only a
        FALLBACK, so this must not be flagged as a gap."""
        run_id = _seed_cost_run()
        for i in range(20):
            _seed_cost_step(
                run_id, f"s{i}", model="brand-new-model-with-self-reported-cost", cost_usd=0.02
            )

        assert config_doctor.check_cost_accounting() == []

    def test_findings_have_actionable_why_and_fix(self) -> None:
        run_id = _seed_cost_run()
        for i in range(20):
            _seed_cost_step(run_id, f"s{i}", model="totally-unlisted-model", cost_usd=None)

        findings = config_doctor.check_cost_accounting()
        assert findings
        for finding in findings:
            assert finding.why
            assert finding.fix


# ---------------------------------------------------------------------------
# check_cost_accounting -- boundary scoping (fix/cost-check-window)
#
# The unpriced-share ratio must be scoped to steps recorded AT OR AFTER a
# cost-instrumentation boundary -- steps from before it have no tokens at
# all and can never be priced, so counting them measures "did an old,
# already-fixed bug exist" (permanently true) rather than "is the CURRENT
# instrumentation healthy".
#
# The boundary itself (`config_doctor._MODELUSAGE_FIX_LANDED_AT`) is
# monkeypatched to a "N days ago" instant computed at test time -- NOT the
# real hardcoded release date -- so these tests exercise the SCOPING LOGIC
# and stay correct regardless of how much real wall-clock time has passed
# since the fix actually landed (the real constant only needs to be
# "sometime in the last 30 days" for the check to matter at all, which
# stops being true a few weeks after this sprint).
#
# Every test below must FAIL against the unscoped check on `origin/main`.
# ---------------------------------------------------------------------------

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _days_ago(days: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(_TS_FORMAT)


def _seed_cost_step_at(
    run_id: int,
    step: str,
    *,
    model: str | None,
    timestamp: str,
    cost_usd: float | None = None,
    input_tokens: int | None = 100,
    output_tokens: int | None = 100,
) -> None:
    from hivepilot.services import db, state_service

    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                "INSERT INTO steps (run_id, step, status, provider, model, "
                "input_tokens, output_tokens, cost_usd, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                run_id,
                step,
                "success",
                "claude",
                model,
                input_tokens,
                output_tokens,
                cost_usd,
                timestamp,
            ),
        )


class TestCheckCostAccountingBoundary:
    def test_pre_boundary_unpriced_steps_produce_no_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-fix rows with zero tokens/no cost signal, recorded WITHIN the
        30-day dashboard window but BEFORE the cost-instrumentation
        boundary, must never fire the unpriced-share warning -- they can
        never be priced (see `hivepilot costs backfill`) and are a known,
        already-explained gap, not a live anomaly."""
        monkeypatch.setattr(config_doctor, "_MODELUSAGE_FIX_LANDED_AT", _days_ago(5))
        run_id = _seed_cost_run()
        for i in range(30):
            _seed_cost_step_at(
                run_id,
                f"s{i}",
                model="opus",
                timestamp=_days_ago(10),  # before the boundary, inside the 30-day window
                cost_usd=None,
                input_tokens=None,
                output_tokens=None,
            )

        findings = config_doctor.check_cost_accounting()
        assert not any(f.check == "cost_accounting_unpriced_share" for f in findings), [
            f.message for f in findings
        ]

    def test_post_boundary_unpriced_steps_still_fire(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The exact same unpriced shape, recorded AFTER the boundary, is a
        live anomaly and must still fire."""
        monkeypatch.setattr(config_doctor, "_MODELUSAGE_FIX_LANDED_AT", _days_ago(5))
        run_id = _seed_cost_run()
        for i in range(20):
            _seed_cost_step_at(
                run_id,
                f"s{i}",
                model="totally-unlisted-model",
                timestamp=_days_ago(1),  # after the boundary
                cost_usd=None,
                input_tokens=None,
                output_tokens=None,
            )

        findings = config_doctor.check_cost_accounting()
        assert any(f.check == "cost_accounting_unpriced_share" for f in findings), [
            f.message for f in findings
        ]

    def test_regression_after_boundary_is_still_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A FUTURE regression reintroducing the exact pre-fix symptom
        (bare alias, zero tokens) AFTER the boundary must still be caught --
        proves the boundary is a fixed instant, never re-derived from the
        shape of the rows it grades (which would let a future regression
        reproducing that same shape push the boundary forward and hide
        itself forever)."""
        monkeypatch.setattr(config_doctor, "_MODELUSAGE_FIX_LANDED_AT", _days_ago(10))
        run_id = _seed_cost_run()
        # A healthy stretch right after the fix landed...
        for i in range(20):
            _seed_cost_step_at(
                run_id,
                f"healthy-{i}",
                model="claude-opus-5",
                timestamp=_days_ago(8),
                cost_usd=0.01,
            )
        # ...then a NEW regression reintroducing the old bare-alias/zero-token
        # shape, recorded well AFTER the boundary (i.e. "now").
        for i in range(20):
            _seed_cost_step_at(
                run_id,
                f"regressed-{i}",
                model="opus",
                timestamp=_days_ago(0),
                cost_usd=None,
                input_tokens=None,
                output_tokens=None,
            )

        findings = config_doctor.check_cost_accounting()
        assert any(f.check == "cost_accounting_unpriced_share" for f in findings), [
            f.message for f in findings
        ]

    def test_unparseable_boundary_override_yields_finding_not_silence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            settings, "cost_instrumentation_since", "not-a-timestamp", raising=False
        )
        run_id = _seed_cost_run()
        for i in range(20):
            _seed_cost_step(run_id, f"s{i}", model="totally-unlisted-model", cost_usd=None)

        findings = config_doctor.check_cost_accounting()
        assert any(f.check == "cost_instrumentation_boundary_unparseable" for f in findings), [
            f.check for f in findings
        ]

    def test_healthy_post_boundary_window_is_silent_despite_pre_boundary_junk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config_doctor, "_MODELUSAGE_FIX_LANDED_AT", _days_ago(5))
        run_id = _seed_cost_run()
        for i in range(50):
            _seed_cost_step_at(
                run_id,
                f"old-{i}",
                model="opus",
                timestamp=_days_ago(10),
                cost_usd=None,
                input_tokens=None,
                output_tokens=None,
            )
        for i in range(20):
            _seed_cost_step_at(
                run_id, f"new-{i}", model="claude-opus-5", timestamp=_days_ago(1), cost_usd=0.01
            )

        findings = config_doctor.check_cost_accounting()
        assert not any(f.severity == "warning" for f in findings), [f.message for f in findings]
        assert not any(f.severity == "error" for f in findings), [f.message for f in findings]

    def test_excluded_rows_info_line_states_real_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config_doctor, "_MODELUSAGE_FIX_LANDED_AT", _days_ago(5))
        run_id = _seed_cost_run()
        for i in range(7):
            _seed_cost_step_at(
                run_id,
                f"old-{i}",
                model="opus",
                timestamp=_days_ago(10),
                cost_usd=None,
                input_tokens=None,
                output_tokens=None,
            )
        for i in range(20):
            _seed_cost_step_at(
                run_id, f"new-{i}", model="claude-opus-5", timestamp=_days_ago(1), cost_usd=0.01
            )

        findings = config_doctor.check_cost_accounting()
        info_findings = [
            f for f in findings if f.check == "cost_accounting_pre_instrumentation_steps"
        ]
        assert info_findings, [f.message for f in findings]
        assert "7" in info_findings[0].message


# ---------------------------------------------------------------------------
# run_doctor / CLI integration
# ---------------------------------------------------------------------------


class TestRunDoctorIntegration:
    def test_clean_config_yields_zero_findings_exit_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """PASSING fixture: a fully clean config + a stubbed, empty
        PluginManager must yield zero findings end to end."""
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(settings, "config_repo", None, raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_valid_config(config_dir)
        (config_dir / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        # Simulate "every currently-enabled plugin flag IS loaded" -- robust
        # to whichever flags happen to default True in this codebase, rather
        # than hardcoding today's default set.
        fake_manager = SimpleNamespace(
            loaded=[SimpleNamespace(name=stem) for stem in _currently_enabled_plugin_stems()],
            check_all=lambda: {},
        )
        monkeypatch.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)

        findings = config_doctor.run_doctor(config_dir=config_dir)

        assert findings == [], f"expected zero findings, got: {[f.message for f in findings]}"

    def test_cli_command_reports_ok_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from typer.testing import CliRunner

        from hivepilot.cli import app

        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(settings, "config_repo", None, raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_valid_config(config_dir)
        (config_dir / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        fake_manager = SimpleNamespace(
            loaded=[SimpleNamespace(name=stem) for stem in _currently_enabled_plugin_stems()],
            check_all=lambda: {},
        )
        monkeypatch.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)

        runner = CliRunner()
        result = runner.invoke(app, ["config", "doctor", "--dir", str(config_dir)])

        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    def test_cli_command_exits_nonzero_on_error_finding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from typer.testing import CliRunner

        from hivepilot.cli import app

        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(settings, "config_repo", None, raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        # Deliberately empty/invalid -- validate_config will report missing files.

        fake_manager = SimpleNamespace(loaded=[], check_all=lambda: {})
        monkeypatch.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)

        runner = CliRunner()
        result = runner.invoke(app, ["config", "doctor", "--dir", str(config_dir)])

        assert result.exit_code == 1, result.output
        assert "ERROR" in result.output

    def test_cli_command_warning_only_still_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Anti-regression for the exit-inversion risk this doctor's own
        docstring calls out ('a WARNING alone still exits 0'): deliberately
        leave HIVEPILOT_BASE_DIR unpinned (incident #1's cwd-relative-path
        WARNING, never an ERROR) with an otherwise clean config, and assert
        the CLI still exits 0."""
        from typer.testing import CliRunner

        from hivepilot.cli import app

        _clear_path_env(monkeypatch)
        # Deliberately NOT setting HIVEPILOT_BASE_DIR here -- that's the
        # point of this test.
        monkeypatch.setattr(settings, "config_repo", None, raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_valid_config(config_dir)
        (config_dir / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        fake_manager = SimpleNamespace(
            loaded=[SimpleNamespace(name=stem) for stem in _currently_enabled_plugin_stems()],
            check_all=lambda: {},
        )
        monkeypatch.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)

        runner = CliRunner()
        result = runner.invoke(app, ["config", "doctor", "--dir", str(config_dir)])

        assert result.exit_code == 0, result.output
        assert "WARN" in result.output, "expected at least one cwd-relative-path WARNING"
        assert "[ERROR]" not in result.output

    def test_cli_command_never_echoes_secret_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Negative secret-leak test: a configured (non-empty) secret value
        must NEVER appear verbatim in `config doctor`'s output, locking in
        the clean-secrets discipline (`check_secrets_sanity` only ever
        reports presence/absence, never the value itself)."""
        from typer.testing import CliRunner

        from hivepilot.cli import app

        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(settings, "config_repo", None, raising=False)
        monkeypatch.setattr(settings, "linear_api_key", "SUPERSECRETVALUE123", raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_valid_config(config_dir)
        (config_dir / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        fake_manager = SimpleNamespace(
            loaded=[SimpleNamespace(name=stem) for stem in _currently_enabled_plugin_stems()],
            check_all=lambda: {},
        )
        monkeypatch.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)

        runner = CliRunner()
        result = runner.invoke(app, ["config", "doctor", "--dir", str(config_dir)])

        assert "SUPERSECRETVALUE123" not in result.output


# ---------------------------------------------------------------------------
# Plugin-double-registration regression (production incident): `run_doctor`
# constructs its OWN `PluginManager()` for plugin health checks, then
# `check_dangling_references` -> `validate_config()` conditionally
# constructs a SECOND, independent `PluginManager()` (only when a task step
# declares `skills:` -- see `config_validation.validate_config_report`).
# Unlike every OTHER test in this file, this one does NOT stub
# `hivepilot.plugins.PluginManager` -- stubbing it is exactly what let this
# bug ship silently: it replaces BOTH real constructions (doctor's own, and
# validate_config's lazily-imported one) with the same no-op fake, so the
# real `_stage_kind` collision path was never exercised end to end.
# ---------------------------------------------------------------------------


class TestPluginDoubleRegistrationRegression:
    def test_doctor_with_real_runner_plugin_and_skill_ref_does_not_lose_dangling_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Exact repro of the reported incident, unmocked end to end: a
        runner-contributing local-file plugin (modeled on the real
        `plugins/gh.py`) plus a task step referencing an unknown `skills:`
        name (the dormant trigger for `validate_config`'s own second
        `PluginManager()`). Before the fix, doctor's own plugin-health
        manager and validate_config's second one collided on the plugin's
        runner kind, producing a `dangling_reference_check_failed` finding
        INSTEAD OF the real dangling-reference checks actually running."""
        monkeypatch.setattr(settings, "config_repo", None, raising=False)
        monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)

        (tmp_path / "plugins").mkdir()
        (tmp_path / "plugins" / "gh.py").write_text(
            "class GhRunner:\n"
            "    def __init__(self, definition, settings):\n"
            "        pass\n\n"
            "    def run(self, payload):\n"
            "        return None\n\n"
            "def register():\n"
            "    return {'runners': {'gh': GhRunner}}\n",
            encoding="utf-8",
        )

        _write_minimal_valid_config(tmp_path)
        # A `skills:` reference is the dormant trigger for validate_config's
        # OWN, second, independent `PluginManager()` construction. The
        # resulting "unknown skill" problem is expected and harmless --
        # what matters is that check_dangling_references runs at all.
        (tmp_path / "tasks.yaml").write_text(
            yaml.dump(
                {
                    "tasks": {
                        "task-a": {
                            "steps": [
                                {"name": "s1", "runner": "claude", "skills": ["dummy-skill"]}
                            ],
                        }
                    }
                }
            )
        )
        (tmp_path / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        findings = config_doctor.run_doctor(config_dir=None)

        assert not any(f.check == "dangling_reference_check_failed" for f in findings), (
            f"dangling-reference checks were lost: {[f.message for f in findings]}"
        )
        # The unknown-skill reference is still correctly reported -- proves
        # check_dangling_references actually RAN end to end (not merely
        # "didn't crash").
        assert any(
            f.check == "dangling_reference" and "dummy-skill" in f.message for f in findings
        ), f"expected a dangling_reference finding, got: {[f.message for f in findings]}"


# ---------------------------------------------------------------------------
# N1 (2nd Opus review, PR #334): a malformed SECOND-level container (e.g.
# `projects.yaml` written as a LIST of projects, exactly like `roles.yaml`
# genuinely IS a list) must never crash the whole doctor report.
# ---------------------------------------------------------------------------


class TestN1MalformedSecondLevelContainer:
    def test_projects_list_does_not_crash_schedules_check(self, tmp_path: Path) -> None:
        """Against commit 46404b2 this raises AttributeError at
        `set((projects_data.get("projects") or {}).keys())` instead of
        returning a finding."""
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": [{"name": "acme"}]}))
        (tmp_path / "tasks.yaml").write_text(yaml.dump({"tasks": {}}))
        (tmp_path / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        findings = config_doctor._check_schedules_dangling(tmp_path)

        assert any(f.check == "invalid_config_section" for f in findings)

    def test_tasks_list_does_not_crash_schedules_check(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        (tmp_path / "tasks.yaml").write_text(yaml.dump({"tasks": ["not", "a", "mapping"]}))
        (tmp_path / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        findings = config_doctor._check_schedules_dangling(tmp_path)

        assert any(f.check == "invalid_config_section" for f in findings)

    def test_schedules_section_list_does_not_crash_schedules_check(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        (tmp_path / "tasks.yaml").write_text(yaml.dump({"tasks": {}}))
        (tmp_path / "schedules.yaml").write_text(yaml.dump({"schedules": ["not", "a", "mapping"]}))

        findings = config_doctor._check_schedules_dangling(tmp_path)

        assert any(f.check == "invalid_config_section" for f in findings)

    def test_policies_section_list_does_not_crash_role_overrides_check(
        self, tmp_path: Path
    ) -> None:
        """Against commit 46404b2 this raises AttributeError at
        `policies.get("default")` (policies_data.get("policies") is a list,
        not a dict)."""
        (tmp_path / "roles.yaml").write_text(
            yaml.dump({"roles": [{"name": "chief_of_staff", "prompt_file": "cos.md"}]})
        )
        (tmp_path / "policies.yaml").write_text(yaml.dump({"policies": ["not", "a", "mapping"]}))

        findings = config_doctor._check_role_overrides_dangling(tmp_path)

        assert any(f.check == "invalid_config_section" for f in findings)

    def test_policies_projects_list_does_not_crash_role_overrides_check(
        self, tmp_path: Path
    ) -> None:
        """Against commit 46404b2 this raises AttributeError at
        `(policies.get("projects") or {}).items()`."""
        (tmp_path / "roles.yaml").write_text(
            yaml.dump({"roles": [{"name": "chief_of_staff", "prompt_file": "cos.md"}]})
        )
        (tmp_path / "policies.yaml").write_text(
            yaml.dump({"policies": {"projects": ["not", "a", "mapping"]}})
        )

        findings = config_doctor._check_role_overrides_dangling(tmp_path)

        assert any(f.check == "invalid_config_section" for f in findings)

    def test_projects_list_does_not_crash_only_modules_check(self, tmp_path: Path) -> None:
        """Against commit 46404b2 this raises AttributeError at
        `(projects_data.get("projects") or {}).values()`."""
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": [{"name": "acme"}]}))
        (tmp_path / "pipelines.yaml").write_text(yaml.dump({"pipelines": {}}))

        findings = config_doctor._check_only_modules_dangling(tmp_path)

        assert any(f.check == "invalid_config_section" for f in findings)

    def test_pipelines_list_does_not_crash_only_modules_check(self, tmp_path: Path) -> None:
        """Against commit 46404b2 this raises AttributeError at
        `(pipelines_data.get("pipelines") or {}).items()`."""
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        (tmp_path / "pipelines.yaml").write_text(yaml.dump({"pipelines": ["not", "a", "mapping"]}))

        findings = config_doctor._check_only_modules_dangling(tmp_path)

        assert any(f.check == "invalid_config_section" for f in findings)

    def test_project_modules_non_mapping_does_not_crash_only_modules_check(
        self, tmp_path: Path
    ) -> None:
        """Against commit 46404b2 this raises AttributeError at
        `(project.get("modules") or {}).keys()`."""
        (tmp_path / "projects.yaml").write_text(
            yaml.dump({"projects": {"acme": {"modules": ["not", "a", "mapping"]}}})
        )
        (tmp_path / "pipelines.yaml").write_text(yaml.dump({"pipelines": {}}))

        findings = config_doctor._check_only_modules_dangling(tmp_path)

        assert any(f.check == "invalid_config_section" for f in findings)

    def test_projects_list_does_not_crash_secrets_sanity(self, tmp_path: Path) -> None:
        """Against commit 46404b2 this raises AttributeError at
        `(projects_data.get("projects") or {}).items()`."""
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": [{"name": "acme"}]}))

        findings = config_doctor.check_secrets_sanity(tmp_path)

        assert any(f.check == "invalid_config_section" for f in findings)

    def test_projects_list_survives_in_full_run_doctor(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The governing rule of this module end to end: a malformed
        `projects.yaml` must produce an emitted finding AND every other
        check's already-computed findings must survive in the same report --
        never a raw traceback that discards everything. Against commit
        46404b2 this crashes `run_doctor` outright with an unhandled
        AttributeError."""
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(settings, "config_repo", None, raising=False)
        # Unrelated, independently-detectable problem that must survive.
        monkeypatch.setattr(settings, "linear_api_key", "", raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_valid_config(config_dir)
        (config_dir / "projects.yaml").write_text(
            yaml.dump({"projects": [{"name": "acme", "path": "~/dev/acme"}]})
        )
        (config_dir / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        fake_manager = SimpleNamespace(
            loaded=[SimpleNamespace(name=stem) for stem in _currently_enabled_plugin_stems()],
            check_all=lambda: {},
        )
        monkeypatch.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)

        findings = config_doctor.run_doctor(config_dir=config_dir)

        assert any(f.check in ("invalid_config_section", "check_crashed") for f in findings), (
            f"expected a finding for the malformed projects.yaml, got: {[f.check for f in findings]}"
        )
        assert any(f.check == "empty_secret_setting" for f in findings), (
            "an unrelated, already-computed finding must survive"
        )


class TestSystemicCheckCrashBackstop:
    def test_one_check_raising_does_not_discard_other_checks_findings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Defense-in-depth on top of the targeted per-site guards above: if
        ANY doctor check raises for a reason not yet anticipated, `run_doctor`
        must convert it to a single `check_crashed` finding instead of
        losing every OTHER check's already-computed findings."""
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(settings, "config_repo", None, raising=False)
        monkeypatch.setattr(settings, "linear_api_key", "", raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_valid_config(config_dir)
        (config_dir / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        fake_manager = SimpleNamespace(
            loaded=[SimpleNamespace(name=stem) for stem in _currently_enabled_plugin_stems()],
            check_all=lambda: {},
        )
        monkeypatch.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)

        def _boom(config_dir: Path | None = None) -> list[config_doctor.DoctorFinding]:
            raise RuntimeError("credential=super-secret-value should never leak")

        monkeypatch.setattr(config_doctor, "check_dangling_references", _boom)

        findings = config_doctor.run_doctor(config_dir=config_dir)

        crashed = [f for f in findings if f.check == "check_crashed"]
        assert crashed, "expected a check_crashed finding, not a lost check"
        assert crashed[0].severity == "error"
        assert "RuntimeError" in crashed[0].message
        assert "credential" not in crashed[0].message
        assert "super-secret-value" not in crashed[0].message
        # The OTHER already-computed check's finding must survive.
        assert any(f.check == "empty_secret_setting" for f in findings)


# ---------------------------------------------------------------------------
# N2 (2nd Opus review, PR #334): one unparseable/malformed file loaded by
# more than one check must not emit the SAME finding multiple times.
# ---------------------------------------------------------------------------


class TestN2DuplicateFindingsCollapsed:
    def test_run_doctor_deduplicates_identical_findings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`projects.yaml` is loaded independently by three different
        checks (_check_schedules_dangling, _check_only_modules_dangling,
        check_secrets_sanity); against commit 46404b2 an unparseable
        projects.yaml emits `unparseable_config_yaml` three times in the
        same `run_doctor()` report."""
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(settings, "config_repo", None, raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_valid_config(config_dir)
        (config_dir / "projects.yaml").write_text("projects: [unclosed\n")
        (config_dir / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        fake_manager = SimpleNamespace(
            loaded=[SimpleNamespace(name=stem) for stem in _currently_enabled_plugin_stems()],
            check_all=lambda: {},
        )
        monkeypatch.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)

        findings = config_doctor.run_doctor(config_dir=config_dir)

        unparseable = [f for f in findings if f.check == "unparseable_config_yaml"]
        assert len(unparseable) == 1, (
            f"expected exactly one deduplicated finding, got {len(unparseable)}: "
            f"{[f.message for f in unparseable]}"
        )


# ---------------------------------------------------------------------------
# N5 (2nd Opus review, PR #334): a `secrets:` catalog that isn't a mapping
# used to silently degrade `ref_name not in catalog` to SUBSTRING matching
# against a scalar -- a fail-open, not just a crash.
# ---------------------------------------------------------------------------


class TestN5SecretsCatalogTypeGuard:
    def test_non_mapping_secrets_catalog_does_not_fail_open_to_substring_match(
        self, tmp_path: Path
    ) -> None:
        """Against commit 46404b2, `catalog = project.get("secrets") or {}`
        is unguarded: if `secrets:` is the STRING `"abc"`, `"a" not in
        catalog` is `False` (substring match) -- a reference named 'a'
        would incorrectly appear resolved. Must emit a finding instead,
        never silently accept the substring match."""
        (tmp_path / "projects.yaml").write_text(
            yaml.dump(
                {
                    "projects": {
                        "acme": {
                            "env": {"API_KEY": "${secret:a}"},
                            "secrets": "abc",
                        }
                    }
                }
            )
        )

        findings = config_doctor.check_secrets_sanity(tmp_path)

        assert any(f.check == "malformed_project_secrets" for f in findings)
        # The fail-open substring match must NOT silently mark 'a' as resolved.
        assert not any(f.check == "dangling_secret_ref" and "'a'" in f.message for f in findings)


# ---------------------------------------------------------------------------
# N6 (2nd Opus review, PR #334): consistency -- every bare type-check that
# silently dropped a malformed entry must emit a finding instead, matching
# the module's stated governing rule.
# ---------------------------------------------------------------------------


class TestN6ConsistentTypeGuards:
    def test_non_string_env_value_emits_finding_not_silence(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(
            yaml.dump(
                {
                    "projects": {
                        "acme": {
                            "env": {"DEBUG": True},
                            "secrets": {},
                        }
                    }
                }
            )
        )

        findings = config_doctor.check_secrets_sanity(tmp_path)

        assert any(f.check == "non_string_project_env_value" for f in findings)
        # An info-level oddity, not an actionable secrets-hygiene error.
        skipped = next(f for f in findings if f.check == "non_string_project_env_value")
        assert skipped.severity == "info"
        assert "DEBUG" in skipped.message

    def test_non_mapping_role_entry_emits_finding_not_silence(self, tmp_path: Path) -> None:
        """Against commit 46404b2, a non-mapping role entry silently
        vanishes from `role_names`, making a role_overrides reference to the
        REAL role that failed to parse look like a dangling reference to a
        role that was simply never declared."""
        (tmp_path / "roles.yaml").write_text(
            yaml.dump({"roles": ["not-a-mapping", {"name": "chief_of_staff"}]})
        )
        (tmp_path / "policies.yaml").write_text(
            yaml.dump({"policies": {"default": {"role_overrides": {"chief_of_staff": {}}}}})
        )

        findings = config_doctor._check_role_overrides_dangling(tmp_path)

        assert any(f.check == "malformed_role_entry" for f in findings)
        # The real, valid role_override must still resolve cleanly.
        assert not any(f.check == "dangling_role_override" for f in findings)

    def test_non_mapping_project_entry_emits_finding_in_only_modules_check(
        self, tmp_path: Path
    ) -> None:
        """Against commit 46404b2, a non-mapping project entry contributes
        no modules to the known-modules set, silently making a genuinely
        valid `only_modules` reference to that project look dangling for
        the WRONG reason."""
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {"acme": "not-a-mapping"}}))
        (tmp_path / "pipelines.yaml").write_text(
            yaml.dump(
                {
                    "pipelines": {
                        "default": {"stages": [{"name": "dev", "only_modules": ["real-module"]}]}
                    }
                }
            )
        )

        findings = config_doctor._check_only_modules_dangling(tmp_path)

        assert any(f.check == "malformed_project_entry" for f in findings)


# ---------------------------------------------------------------------------
# N4 (2nd Opus review, PR #334): guard the M5 CLI wiring -- reverting
# `cli.py`'s `badge = verify_badge(result)` to an inline `"ok"` badge must
# not pass silently at the CLI level.
# ---------------------------------------------------------------------------


class TestN4CliSurfacesMissingBadge:
    def test_cli_command_surfaces_missing_badge_from_verify_badge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reverting `cli.py:3756` to an inline `"ok"` badge (instead of
        calling `verify_badge(result)`) would still pass
        `TestVerifyBadge` (tests the helper in isolation) AND
        `test_cli_command_lists_every_plugin` (only asserts names + exit
        code) -- neither would catch the regression. This test stubs
        `verify_plugins` with a MISSING-shaped result and asserts the badge
        actually reaches the CLI's rendered output."""
        from typer.testing import CliRunner

        from hivepilot.cli import app

        missing_result = config_doctor.PluginVerifyResult(
            name="mem0",
            prereq_kind="pip",
            present_per_declaration=False,
            importable=False,
            mismatch=None,
            detail="NOT importable (ImportError); pip: 'mem0ai' not installed",
        )
        monkeypatch.setattr(config_doctor, "verify_plugins", lambda: [missing_result])

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "verify"])

        assert "MISSING" in result.output, result.output


# ---------------------------------------------------------------------------
# plugins verify (incident #5)
# ---------------------------------------------------------------------------


class TestVerifyPlugins:
    def test_platform_tag_reports_real_platform_details(self) -> None:
        """Fixed (was a Goodhart test that passed for `return "x"`, catching
        NOTHING about the real implementation): assert the tag actually
        embeds this process's real machine arch and system-name prefix,
        which a stub could never produce."""
        import platform as platform_module

        tag = config_doctor.platform_tag()

        assert isinstance(tag, str) and tag
        assert platform_module.machine() in tag
        if platform_module.system() == "Linux":
            assert tag.startswith("linux-")
        else:
            assert tag.startswith(platform_module.system())

    def test_distinguishes_installed_per_pip_from_importable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A module that imports fine but whose EXPECTED distribution isn't
        what pip reports must be flagged as a mismatch (namesake collision,
        incident #5) -- NOT silently treated as 'ok'."""

        def fake_import(name: str):
            if name == "headroom":
                return object()
            raise ImportError(name)

        def fake_version(dist: str) -> str:
            raise config_doctor.importlib_metadata.PackageNotFoundError(dist)

        monkeypatch.setattr(config_doctor.importlib, "import_module", fake_import)
        monkeypatch.setattr(config_doctor.importlib_metadata, "version", fake_version)

        result = config_doctor._verify_pip_plugin("headroom", "headroom", "headroom-ai")

        assert result.importable is True
        assert result.present_per_declaration is False
        assert result.mismatch is not None
        assert "namesake collision" in result.mismatch

    def test_pip_installed_but_not_importable_is_flagged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of incident #5: pip believes it's installed, but
        the import fails (e.g. a broken native build)."""

        def fake_import(name: str):
            raise ImportError("no module named " + name)

        def fake_version(dist: str) -> str:
            return "1.2.3"

        monkeypatch.setattr(config_doctor.importlib, "import_module", fake_import)
        monkeypatch.setattr(config_doctor.importlib_metadata, "version", fake_version)

        result = config_doctor._verify_pip_plugin("headroom", "headroom", "headroom-ai")

        assert result.importable is False
        assert result.present_per_declaration is True
        assert result.mismatch is not None
        assert "broken" in result.mismatch

    def test_clean_when_import_and_distribution_agree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config_doctor.importlib, "import_module", lambda name: object())
        monkeypatch.setattr(config_doctor.importlib_metadata, "version", lambda dist: "1.0.0")

        result = config_doctor._verify_pip_plugin("mem0", "mem0", "mem0ai")

        assert result.importable is True
        assert result.present_per_declaration is True
        assert result.mismatch is None

    def test_verify_plugins_covers_every_known_example_plugin(self) -> None:
        from hivepilot.services.plugin_installer import KNOWN_EXAMPLE_PLUGINS

        results = config_doctor.verify_plugins()

        names = {r.name for r in results}
        assert names == set(KNOWN_EXAMPLE_PLUGINS)

    def test_binary_probe_reports_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config_doctor.shutil, "which", lambda binary: None)

        result = config_doctor._verify_binary_plugin("rtk", "rtk")

        assert result.prereq_kind == "binary"
        assert result.present_per_declaration is False
        assert "NOT FOUND" in result.detail

    def test_binary_probe_reports_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config_doctor.shutil, "which", lambda binary: f"/usr/bin/{binary}")

        result = config_doctor._verify_binary_plugin("rtk", "rtk")

        assert result.present_per_declaration is True
        assert "found on PATH" in result.detail


class TestVerifyBadge:
    """M5: `verify_badge`'s three-state mapping -- a plugin that is neither
    importable nor pip-installed (mismatch=None, both AGREE "absent") must
    render `MISSING`, never a plain `ok` an operator would read as healthy."""

    def test_mismatch_wins_over_everything(self) -> None:
        result = config_doctor.PluginVerifyResult(
            name="headroom",
            prereq_kind="pip",
            present_per_declaration=False,
            importable=True,
            mismatch="namesake collision",
            detail="...",
        )
        assert config_doctor.verify_badge(result) == "MISMATCH"

    def test_missing_pip_dependency_is_not_ok(self) -> None:
        result = config_doctor.PluginVerifyResult(
            name="mem0",
            prereq_kind="pip",
            present_per_declaration=False,
            importable=False,
            mismatch=None,
            detail="NOT importable (ImportError); pip: 'mem0ai' not installed",
        )
        assert config_doctor.verify_badge(result) == "MISSING"

    def test_missing_binary_is_not_ok(self) -> None:
        result = config_doctor.PluginVerifyResult(
            name="rtk",
            prereq_kind="binary",
            present_per_declaration=False,
            importable=None,
            mismatch=None,
            detail="binary 'rtk': NOT FOUND on PATH",
        )
        assert config_doctor.verify_badge(result) == "MISSING"

    def test_healthy_pip_plugin_is_ok(self) -> None:
        result = config_doctor.PluginVerifyResult(
            name="mem0",
            prereq_kind="pip",
            present_per_declaration=True,
            importable=True,
            mismatch=None,
            detail="importable; pip: 'mem0ai' 1.0.0 installed",
        )
        assert config_doctor.verify_badge(result) == "ok"

    def test_healthy_binary_plugin_is_ok(self) -> None:
        result = config_doctor.PluginVerifyResult(
            name="rtk",
            prereq_kind="binary",
            present_per_declaration=True,
            importable=None,
            mismatch=None,
            detail="binary 'rtk': found on PATH",
        )
        assert config_doctor.verify_badge(result) == "ok"

    def test_unverified_multi_mode_dependency_is_ok(self) -> None:
        """A plugin `verify_plugins()` deliberately doesn't probe (e.g.
        multi-mode SDK selection) has no truth to contradict -- must stay
        `ok`, never `MISSING`."""
        result = config_doctor.PluginVerifyResult(
            name="onepassword",
            prereq_kind="pip",
            present_per_declaration=None,
            importable=None,
            mismatch=None,
            detail="multi-mode dependency; not automatically verified",
        )
        assert config_doctor.verify_badge(result) == "ok"


class TestPluginVerifyResilience:
    def test_corrupt_dist_info_does_not_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """L3: only `PackageNotFoundError` was caught around
        `importlib_metadata.version` before -- any OTHER exception (e.g.
        corrupt dist-info metadata) crashed `plugins verify` outright."""

        def fake_import(name: str):
            raise ImportError("not installed")

        def fake_version(dist: str) -> str:
            raise RuntimeError("corrupt RECORD file, path=/home/op/.secrets")

        monkeypatch.setattr(config_doctor.importlib, "import_module", fake_import)
        monkeypatch.setattr(config_doctor.importlib_metadata, "version", fake_version)

        result = config_doctor._verify_pip_plugin("mem0", "mem0", "mem0ai")

        assert result.present_per_declaration is False
        assert result.importable is False
        # never leak str(exc) (could embed a path/env value) -- type name only.
        assert "corrupt RECORD file" not in result.detail
        assert "RuntimeError" in result.detail

    def test_import_failure_never_leaks_exception_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L2: name the exception TYPE only, matching
        `plugins.py::run_health_check`'s discipline -- never interpolate
        `str(exc)` from an arbitrary third-party import."""

        def fake_import(name: str):
            raise ImportError("some sensitive path or env value leaked here")

        def fake_version(dist: str) -> str:
            return "1.0.0"

        monkeypatch.setattr(config_doctor.importlib, "import_module", fake_import)
        monkeypatch.setattr(config_doctor.importlib_metadata, "version", fake_version)

        result = config_doctor._verify_pip_plugin("mem0", "mem0", "mem0ai")

        assert "sensitive path or env value" not in result.detail
        assert "ImportError" in result.detail


class TestFindingSeverityGuard:
    def test_invalid_severity_raises_value_error(self) -> None:
        """L1: must not rely on a bare `assert` (stripped under `python
        -O`), which would otherwise let a bogus severity reach `render()`'s
        badge dict lookup and raise KeyError there instead."""
        with pytest.raises(ValueError):
            config_doctor._finding("bogus", "check", "msg", "why", "fix")


class TestPluginsVerifyCli:
    def test_cli_command_lists_every_plugin(self) -> None:
        """Fixed (was `exit_code in (0, 1)`, which accepts EITHER outcome
        and can never detect an exit-code regression): compute the expected
        exit code independently from the same `verify_plugins()` the CLI
        calls, and assert exact equality."""
        from typer.testing import CliRunner

        from hivepilot.cli import app
        from hivepilot.services.plugin_installer import KNOWN_EXAMPLE_PLUGINS

        expected_mismatch = any(r.mismatch for r in config_doctor.verify_plugins())

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "verify"])

        assert result.exit_code == (1 if expected_mismatch else 0), result.output
        for name in KNOWN_EXAMPLE_PLUGINS:
            assert name in result.output

    def test_cli_command_exits_zero_when_no_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Deterministic complement to the above (independent of the real
        environment's actual plugin state): force a clean result set and
        assert exit 0."""
        from typer.testing import CliRunner

        from hivepilot.cli import app

        clean_results = [
            config_doctor.PluginVerifyResult(
                name="mem0",
                prereq_kind="pip",
                present_per_declaration=True,
                importable=True,
                mismatch=None,
                detail="importable; pip: 'mem0ai' 1.0.0 installed",
            ),
        ]
        monkeypatch.setattr(config_doctor, "verify_plugins", lambda: clean_results)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "verify"])

        assert result.exit_code == 0, result.output

    def test_cli_command_exits_nonzero_on_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from typer.testing import CliRunner

        from hivepilot.cli import app

        mismatched_results = [
            config_doctor.PluginVerifyResult(
                name="headroom",
                prereq_kind="pip",
                present_per_declaration=False,
                importable=True,
                mismatch="namesake collision",
                detail="...",
            ),
        ]
        monkeypatch.setattr(config_doctor, "verify_plugins", lambda: mismatched_results)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "verify"])

        assert result.exit_code == 1, result.output


# ---------------------------------------------------------------------------
# Incident #1 (HIGHEST VALUE, config-doctor-session-incidents sprint): a
# `claude_md` / `instruction_files` reference pointing at a repo instructions
# file ABSENT from the repo used to produce zero findings anywhere -- every
# agent ran without governance context for months. Reuses
# `hivepilot.services.repo_instructions`'s OWN resolution
# (`declared_instruction_files` / `resolve_instruction_file_path`) rather
# than reimplementing it, so this check can never disagree with what
# `build_repo_instructions_section` actually does at run time.
# ---------------------------------------------------------------------------


class TestDanglingInstructionFiles:
    def test_no_projects_yields_no_findings(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))

        findings = config_doctor.check_dangling_instruction_files(tmp_path)

        assert findings == []

    def test_project_with_no_declared_files_yields_no_findings(self, tmp_path: Path) -> None:
        """Signal-to-noise: `claude_md`/`instruction_files` are dormant
        (None) by default -- a project that never sets them must yield
        ZERO findings, not a false positive."""
        (tmp_path / "projects.yaml").write_text(
            yaml.dump({"projects": {"acme": {"path": str(tmp_path / "acme")}}})
        )

        findings = config_doctor.check_dangling_instruction_files(tmp_path)

        assert findings == []

    def test_existing_claude_md_yields_no_findings(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "acme"
        project_dir.mkdir()
        (project_dir / "CLAUDE.md").write_text("# governance", encoding="utf-8")
        (tmp_path / "projects.yaml").write_text(
            yaml.dump({"projects": {"acme": {"path": str(project_dir), "claude_md": "CLAUDE.md"}}})
        )

        findings = config_doctor.check_dangling_instruction_files(tmp_path)

        assert findings == []

    def test_dangling_claude_md_names_project_declared_resolved_and_searched_dir(
        self, tmp_path: Path
    ) -> None:
        """Real incident: `claude_md: CLAUDE.md` pointed at a file absent
        from the repo, and nothing reported it for months. The finding must
        name the project, the declared filename, the resolved path, and the
        directory searched -- exactly what an operator needs to fix it."""
        project_dir = tmp_path / "acme"
        project_dir.mkdir()
        (tmp_path / "projects.yaml").write_text(
            yaml.dump({"projects": {"acme": {"path": str(project_dir), "claude_md": "CLAUDE.md"}}})
        )

        findings = config_doctor.check_dangling_instruction_files(tmp_path)

        dangling = [f for f in findings if f.check == "dangling_instruction_file"]
        assert dangling, f"expected a dangling_instruction_file finding, got: {findings}"
        message = dangling[0].message
        assert dangling[0].severity == "error"
        assert "acme" in message
        assert "CLAUDE.md" in message
        assert str((project_dir / "CLAUDE.md").resolve()) in message
        assert str(project_dir.resolve()) in message

    def test_dangling_instruction_files_entry_is_also_flagged(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "acme"
        project_dir.mkdir()
        (tmp_path / "projects.yaml").write_text(
            yaml.dump(
                {
                    "projects": {
                        "acme": {
                            "path": str(project_dir),
                            "instruction_files": ["AGENTS.md"],
                        }
                    }
                }
            )
        )

        findings = config_doctor.check_dangling_instruction_files(tmp_path)

        dangling = [f for f in findings if f.check == "dangling_instruction_file"]
        assert dangling and "AGENTS.md" in dangling[0].message

    def test_reuses_repo_instructions_resolution_not_a_reimplementation(
        self, tmp_path: Path
    ) -> None:
        """Anti-Goodhart: a monorepo/umbrella `../CLAUDE.md` reference must
        resolve the SAME way `repo_instructions.resolve_instruction_file_path`
        resolves it (relative to the project path, no implicit walk) -- proof
        this check calls the real resolver instead of a parallel one that
        could silently disagree."""
        outside = tmp_path / "CLAUDE.md"
        outside.write_text("# umbrella governance", encoding="utf-8")
        project_dir = tmp_path / "repo"
        project_dir.mkdir()
        (tmp_path / "projects.yaml").write_text(
            yaml.dump(
                {"projects": {"acme": {"path": str(project_dir), "claude_md": "../CLAUDE.md"}}}
            )
        )

        findings = config_doctor.check_dangling_instruction_files(tmp_path)

        assert not any(f.check == "dangling_instruction_file" for f in findings)

    def test_non_string_claude_md_yields_malformed_finding_not_crash(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(
            yaml.dump(
                {"projects": {"acme": {"path": str(tmp_path), "claude_md": ["not", "a", "str"]}}}
            )
        )

        findings = config_doctor.check_dangling_instruction_files(tmp_path)

        assert any(f.check == "invalid_instruction_file_declaration" for f in findings)

    def test_non_list_instruction_files_yields_malformed_finding_not_crash(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "projects.yaml").write_text(
            yaml.dump(
                {"projects": {"acme": {"path": str(tmp_path), "instruction_files": "not-a-list"}}}
            )
        )

        findings = config_doctor.check_dangling_instruction_files(tmp_path)

        assert any(f.check == "invalid_instruction_file_declaration" for f in findings)

    def test_non_mapping_project_entry_yields_finding_not_silence(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {"acme": "not-a-mapping"}}))

        findings = config_doctor.check_dangling_instruction_files(tmp_path)

        assert any(f.check == "malformed_project_entry" for f in findings)

    def test_missing_project_path_yields_finding_not_crash(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(
            yaml.dump({"projects": {"acme": {"claude_md": "CLAUDE.md"}}})
        )

        findings = config_doctor.check_dangling_instruction_files(tmp_path)

        assert any(f.check == "project_missing_path" for f in findings)

    def test_malformed_projects_yaml_yields_finding_not_silence(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text("projects: [unclosed\n")

        findings = config_doctor.check_dangling_instruction_files(tmp_path)

        assert any(f.check == "unparseable_config_yaml" for f in findings)


# ---------------------------------------------------------------------------
# Incident #2: `Settings.obsidian_vault` is a single GLOBAL path -- with N
# projects/pipelines on one machine, they cannot be routed to different
# vaults. Informational only (a known engine limitation, not a
# misconfiguration): severity must be "info", never "error"/"warning".
# ---------------------------------------------------------------------------


class TestSharedObsidianVaultLimitation:
    def test_single_project_yields_no_finding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(settings, "obsidian_enabled", True, raising=False)
        (tmp_path / "projects.yaml").write_text(
            yaml.dump({"projects": {"acme": {"path": str(tmp_path)}}})
        )

        findings = config_doctor.check_shared_obsidian_vault(tmp_path)

        assert findings == []

    def test_no_projects_yields_no_finding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(settings, "obsidian_enabled", True, raising=False)
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))

        findings = config_doctor.check_shared_obsidian_vault(tmp_path)

        assert findings == []

    def test_multiple_projects_yields_info_finding_naming_the_limitation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(settings, "obsidian_enabled", True, raising=False)
        (tmp_path / "projects.yaml").write_text(
            yaml.dump(
                {
                    "projects": {
                        "personal": {"path": str(tmp_path / "a")},
                        "product": {"path": str(tmp_path / "b")},
                    }
                }
            )
        )

        findings = config_doctor.check_shared_obsidian_vault(tmp_path)

        shared = [f for f in findings if f.check == "shared_obsidian_vault"]
        assert shared, f"expected a shared_obsidian_vault finding, got: {findings}"
        assert shared[0].severity == "info", "a known limitation must never be error/warning"
        assert "2" in shared[0].message

    def test_obsidian_disabled_yields_no_finding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(settings, "obsidian_enabled", False, raising=False)
        (tmp_path / "projects.yaml").write_text(
            yaml.dump(
                {
                    "projects": {
                        "personal": {"path": str(tmp_path / "a")},
                        "product": {"path": str(tmp_path / "b")},
                    }
                }
            )
        )

        findings = config_doctor.check_shared_obsidian_vault(tmp_path)

        assert findings == []

    def test_malformed_projects_yaml_yields_finding_not_silence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(settings, "obsidian_enabled", True, raising=False)
        (tmp_path / "projects.yaml").write_text("projects: [unclosed\n")

        findings = config_doctor.check_shared_obsidian_vault(tmp_path)

        assert any(f.check == "unparseable_config_yaml" for f in findings)


# ---------------------------------------------------------------------------
# Incident #3: `obsidian_service.py` has NO git capability -- on the
# operator's box the vault sat 67 files uncommitted for 6 days. Local git
# state only (no fetch/network), consistent with this doctor's offline
# discipline.
# ---------------------------------------------------------------------------


def _init_repo(path: Path):
    import git as gitlib

    repo = gitlib.Repo.init(path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")
    return repo


class TestVaultGitState:
    def test_vault_absent_yields_no_findings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(settings, "obsidian_vault", Path("does-not-exist"), raising=False)

        findings = config_doctor.check_vault_git_state()

        assert findings == []

    def test_vault_not_a_git_repo_yields_info(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _clear_path_env(monkeypatch)
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "note.md").write_text("hello", encoding="utf-8")
        monkeypatch.setattr(settings, "obsidian_vault", vault, raising=False)

        findings = config_doctor.check_vault_git_state()

        not_repo = [f for f in findings if f.check == "vault_not_git_repo"]
        assert not_repo, f"expected vault_not_git_repo, got: {findings}"
        assert not_repo[0].severity == "info"

    def test_clean_committed_vault_yields_no_findings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _clear_path_env(monkeypatch)
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "note.md").write_text("hello", encoding="utf-8")
        repo = _init_repo(vault)
        repo.git.add("-A")
        repo.git.commit("-m", "initial")
        monkeypatch.setattr(settings, "obsidian_vault", vault, raising=False)

        findings = config_doctor.check_vault_git_state()

        assert findings == []

    def test_uncommitted_artifacts_yield_warning_with_count(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Real incident: 67 files sat uncommitted for 6 days -- only ever
        visible on the host, never durable/shared."""
        _clear_path_env(monkeypatch)
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "committed.md").write_text("hello", encoding="utf-8")
        repo = _init_repo(vault)
        repo.git.add("-A")
        repo.git.commit("-m", "initial")
        (vault / "run-1.md").write_text("artifact 1", encoding="utf-8")
        (vault / "run-2.md").write_text("artifact 2", encoding="utf-8")
        monkeypatch.setattr(settings, "obsidian_vault", vault, raising=False)

        findings = config_doctor.check_vault_git_state()

        uncommitted = [f for f in findings if f.check == "vault_uncommitted_artifacts"]
        assert uncommitted, f"expected vault_uncommitted_artifacts, got: {findings}"
        assert uncommitted[0].severity == "warning"
        assert "2" in uncommitted[0].message

    def test_unpushed_commits_yield_warning_with_count(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _clear_path_env(monkeypatch)
        remote_bare = tmp_path / "remote.git"
        import git as gitlib

        gitlib.Repo.init(remote_bare, bare=True)

        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "note.md").write_text("hello", encoding="utf-8")
        repo = _init_repo(vault)
        repo.git.add("-A")
        repo.git.commit("-m", "initial")
        repo.create_remote("origin", str(remote_bare))
        repo.git.push("-u", "origin", repo.active_branch.name)

        (vault / "note2.md").write_text("second commit", encoding="utf-8")
        repo.git.add("-A")
        repo.git.commit("-m", "second, never pushed")
        monkeypatch.setattr(settings, "obsidian_vault", vault, raising=False)

        findings = config_doctor.check_vault_git_state()

        unpushed = [f for f in findings if f.check == "vault_unpushed_commits"]
        assert unpushed, f"expected vault_unpushed_commits, got: {findings}"
        assert unpushed[0].severity == "warning"
        assert "1" in unpushed[0].message

    def test_no_upstream_configured_is_not_reported_as_unpushed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A deliberately local-only vault (no remote at all) must not be
        misreported as having unpushed commits -- there is nowhere to push
        to, which is a legitimate setup, not a problem."""
        _clear_path_env(monkeypatch)
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "note.md").write_text("hello", encoding="utf-8")
        repo = _init_repo(vault)
        repo.git.add("-A")
        repo.git.commit("-m", "initial")
        monkeypatch.setattr(settings, "obsidian_vault", vault, raising=False)

        findings = config_doctor.check_vault_git_state()

        assert not any(f.check == "vault_unpushed_commits" for f in findings)

    def test_broken_git_checkout_yields_finding_not_crash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Governing principle: 'I could not inspect this' must be a
        finding, never silence or a crash."""
        _clear_path_env(monkeypatch)
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / ".git").write_text("gitdir: /does/not/exist\n", encoding="utf-8")
        monkeypatch.setattr(settings, "obsidian_vault", vault, raising=False)

        findings = config_doctor.check_vault_git_state()

        assert any(f.check == "vault_git_state_check_failed" for f in findings)
        assert any(f.severity == "error" for f in findings)


# ---------------------------------------------------------------------------
# Noise-floor regression (mandatory per sprint spec): a realistic config with
# everything at defaults must produce ZERO ERROR findings from the THREE new
# checks added in this sprint, end to end through `run_doctor`.
# ---------------------------------------------------------------------------


class TestSessionIncidentsNoiseFloor:
    def test_default_config_yields_zero_errors_from_new_checks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(settings, "config_repo", None, raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_valid_config(config_dir)
        (config_dir / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        fake_manager = SimpleNamespace(
            loaded=[SimpleNamespace(name=stem) for stem in _currently_enabled_plugin_stems()],
            check_all=lambda: {},
        )
        monkeypatch.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)

        findings = config_doctor.run_doctor(config_dir=config_dir)

        new_checks = {
            "dangling_instruction_file",
            "invalid_instruction_file_declaration",
            "project_missing_path",
            "shared_obsidian_vault",
            "vault_not_git_repo",
            "vault_uncommitted_artifacts",
            "vault_unpushed_commits",
            "vault_git_state_check_failed",
        }
        error_findings = [f for f in findings if f.check in new_checks and f.severity == "error"]
        assert error_findings == [], (
            f"a default config must yield zero ERROR findings from the new checks, "
            f"got: {[(f.check, f.message) for f in error_findings]}"
        )


class TestDisplayTimezoneCheck:
    """`check_display_timezone` — the doctor check for the display-timestamps
    fix: an operator misconfigured display timezone must never silently
    render UTC as if it were local (the original production incident)."""

    def test_valid_override_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
        findings = config_doctor.check_display_timezone()
        assert findings == []

    def test_no_override_with_detectable_system_zone_is_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "display_timezone", None, raising=False)
        monkeypatch.setattr(
            "hivepilot.utils.display_time.detect_system_zone_name",
            lambda: "Europe/Paris",
        )
        findings = config_doctor.check_display_timezone()
        assert findings == []

    def test_invalid_override_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "display_timezone", "Not/AZone", raising=False)
        findings = config_doctor.check_display_timezone()
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert findings[0].check == "invalid_display_timezone"

    def test_no_override_and_undetectable_system_zone_is_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "display_timezone", None, raising=False)
        monkeypatch.setattr("hivepilot.utils.display_time.detect_system_zone_name", lambda: None)
        findings = config_doctor.check_display_timezone()
        assert len(findings) == 1
        assert findings[0].severity == "warning"
        assert findings[0].check == "display_timezone_fallback_utc"


# ---------------------------------------------------------------------------
# check_partition_readiness -- propose->ratify->dispatch PRD, sprint S5.
#
# Two checks, both aimed at a partition that is configured but silently
# cannot do what the operator believes it does:
#
#   1. `claude_max_concurrency: 1` (the DEFAULT) turns "N parallel agents"
#      into one agent N times.
#   2. no positive `max_partition_cost_usd` makes the ratification gate
#      refuse EVERY partition naming that project -- and the refusal only
#      surfaces after a human has already reviewed a plan and pressed
#      dispatch.
#
# Anti-noise contract, tested explicitly below: a project that is NOT
# partition-capable is never a finding, and both checks aggregate into a
# single finding naming every affected project rather than one per project
# (incident #4b: 17 false positives out of 19 findings and the operator
# stopped reading the report).
# ---------------------------------------------------------------------------


class TestPartitionReadiness:
    @staticmethod
    def _write(tmp_path: Path, projects: dict, policies: dict) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": projects}))
        (tmp_path / "policies.yaml").write_text(yaml.dump({"policies": policies}))

    def test_partition_capable_project_without_cost_ceiling_is_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAILING fixture: `outward_actions` declares partition intent, but
        with no `max_partition_cost_usd` the gate refuses every partition."""
        monkeypatch.setattr(settings, "claude_max_concurrency", 4, raising=False)
        self._write(
            tmp_path,
            {"acme-api": {"path": "~/dev/acme-api"}},
            {"projects": {"acme-api": {"outward_actions": ["git_push"]}}},
        )

        findings = config_doctor.check_partition_readiness(tmp_path)

        assert len(findings) == 1
        assert findings[0].check == "partition_missing_cost_ceiling"
        assert findings[0].severity == "error"
        assert "acme-api" in findings[0].message
        assert "max_partition_cost_usd" in findings[0].fix

    def test_zero_cost_ceiling_is_treated_as_absent_not_as_a_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`autopilot_policy._positive_float` resolves 0/negative/bool/
        non-numeric to None (deny). The doctor must read the value through
        that SAME resolver, not through a bare `in` test, or it would call a
        `max_partition_cost_usd: 0` project healthy while the gate denies."""
        monkeypatch.setattr(settings, "claude_max_concurrency", 4, raising=False)
        for bad in (0, -1, True, "not-a-number"):
            self._write(
                tmp_path,
                {"acme-api": {"path": "~/dev/acme-api"}},
                {"projects": {"acme-api": {"max_partition_cost_usd": bad}}},
            )
            findings = config_doctor.check_partition_readiness(tmp_path)
            assert [f.check for f in findings] == ["partition_missing_cost_ceiling"], bad

    def test_positive_cost_ceiling_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "claude_max_concurrency", 4, raising=False)
        self._write(
            tmp_path,
            {"acme-api": {"path": "~/dev/acme-api"}},
            {
                "projects": {
                    "acme-api": {"outward_actions": ["git_push"], "max_partition_cost_usd": 5.0}
                }
            },
        )

        assert config_doctor.check_partition_readiness(tmp_path) == []

    def test_a_project_that_is_not_partition_capable_is_never_a_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The anti-noise rule, stated as a test: an ordinary project with no
        partition keys at all must produce ZERO findings even on the
        default-throttled host that check #1 exists for."""
        monkeypatch.setattr(settings, "claude_max_concurrency", 1, raising=False)
        self._write(
            tmp_path,
            {"acme-api": {"path": "~/dev/acme-api"}},
            {"projects": {"acme-api": {"require_approval": True, "budget_daily_usd": 5.0}}},
        )

        assert config_doctor.check_partition_readiness(tmp_path) == []

    def test_claude_max_concurrency_one_is_flagged_when_partitions_are_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "claude_max_concurrency", 1, raising=False)
        self._write(
            tmp_path,
            {"acme-api": {"path": "~/dev/acme-api"}},
            {"projects": {"acme-api": {"max_partition_cost_usd": 5.0}}},
        )

        findings = config_doctor.check_partition_readiness(tmp_path)

        assert len(findings) == 1
        assert findings[0].check == "partition_parallelism_capped_at_one"
        assert findings[0].severity == "warning"
        assert "acme-api" in findings[0].message
        assert "claude_max_concurrency" in findings[0].message
        assert "HIVEPILOT_CLAUDE_MAX_CONCURRENCY" in findings[0].fix

    def test_raised_concurrency_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "claude_max_concurrency", 3, raising=False)
        self._write(
            tmp_path,
            {"acme-api": {"path": "~/dev/acme-api"}},
            {"projects": {"acme-api": {"max_partition_cost_usd": 5.0}}},
        )

        assert config_doctor.check_partition_readiness(tmp_path) == []

    def test_both_checks_aggregate_into_one_finding_each_naming_every_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anti-noise: three affected projects must not become six findings."""
        monkeypatch.setattr(settings, "claude_max_concurrency", 1, raising=False)
        self._write(
            tmp_path,
            {
                "acme-api": {"path": "~/dev/acme-api"},
                "acme-web": {"path": "~/dev/acme-web"},
                "acme-worker": {"path": "~/dev/acme-worker"},
            },
            {"default": {"outward_actions": ["git_push"]}},
        )

        findings = config_doctor.check_partition_readiness(tmp_path)

        assert sorted(f.check for f in findings) == [
            "partition_missing_cost_ceiling",
            "partition_parallelism_capped_at_one",
        ]
        for finding in findings:
            for project in ("acme-api", "acme-web", "acme-worker"):
                assert project in finding.message

    def test_project_block_overrides_a_partition_capable_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`default` + project override is the same merge order
        `autopilot_policy.get_autopilot_policy` uses -- a project that sets
        its own positive ceiling is healthy even when `default` has none."""
        monkeypatch.setattr(settings, "claude_max_concurrency", 4, raising=False)
        self._write(
            tmp_path,
            {"acme-api": {"path": "~/dev/acme-api"}, "acme-web": {"path": "~/dev/acme-web"}},
            {
                "default": {"outward_actions": ["git_push"]},
                "projects": {"acme-api": {"max_partition_cost_usd": 5.0}},
            },
        )

        findings = config_doctor.check_partition_readiness(tmp_path)

        assert len(findings) == 1
        assert "acme-web" in findings[0].message
        assert "acme-api" not in findings[0].message

    def test_unparseable_policies_yaml_yields_a_finding_not_silence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The module's governing rule: "I could not inspect this" must
        produce a finding, never silence."""
        monkeypatch.setattr(settings, "claude_max_concurrency", 1, raising=False)
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {"acme-api": {}}}))
        (tmp_path / "policies.yaml").write_text("policies: [unclosed\n")

        findings = config_doctor.check_partition_readiness(tmp_path)

        assert any(f.check == "unparseable_config_yaml" for f in findings)

    def test_non_mapping_policies_section_yields_a_finding_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "claude_max_concurrency", 1, raising=False)
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {"acme-api": {}}}))
        (tmp_path / "policies.yaml").write_text(yaml.dump({"policies": ["not", "a", "mapping"]}))

        findings = config_doctor.check_partition_readiness(tmp_path)

        assert any(f.check == "invalid_config_section" for f in findings)

    def test_non_mapping_policy_scope_yields_a_finding_not_silence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "claude_max_concurrency", 4, raising=False)
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {"acme-api": {}}}))
        (tmp_path / "policies.yaml").write_text(
            yaml.dump({"policies": {"projects": {"acme-api": "not-a-mapping"}}})
        )

        findings = config_doctor.check_partition_readiness(tmp_path)

        assert any(f.check == "malformed_policy_entry" for f in findings)

    def test_wired_into_run_doctor(self, tmp_path: Path) -> None:
        """A check that exists but is never registered in `run_doctor()` is
        exactly as invisible as no check at all -- assert it's reachable
        through the real entry point, not just directly callable."""
        _write_minimal_valid_config(tmp_path)
        (tmp_path / "policies.yaml").write_text(
            yaml.dump({"policies": {"projects": {"demo": {"outward_actions": ["git_push"]}}}})
        )

        fake_manager = SimpleNamespace(
            loaded=[SimpleNamespace(name=stem) for stem in _currently_enabled_plugin_stems()],
            check_all=lambda: {},
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)
            findings = config_doctor.run_doctor(config_dir=tmp_path)

        assert any(f.check == "partition_missing_cost_ceiling" for f in findings)
