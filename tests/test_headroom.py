"""
Tests for the `headroom` plugin (Sprint 3 of the plugins plan).

`plugins/headroom.py` is a local-file plugin (see docs/PLUGINS.md) that
compresses a step's prompt/context before it runs, via the `before_step`
lifecycle hook. It targets `RunnerPayload.metadata["prior_context"]` and
`RunnerPayload.metadata["extra_prompt"]` — both are read verbatim by
`ClaudeRunner._build_prompt` (`hivepilot/runners/claude_runner.py`) off the
SAME `payload` object the orchestrator hands to the hook
(`Orchestrator._execute_task`, `hivepilot/orchestrator.py`,
`self.plugins.run_hook("before_step", payload=payload)`), so an in-place
mutation here is picked up by the runner with no copy in between.

Covers, per the sprint spec:
(a) `register()` exposes a `before_step` hook.
(b) With `headroom.compress` mocked to return a shorter string,
    `before_step(payload=...)` mutates the compressible field(s) in place
    and logs a compression ratio.
(c) Lib absent (`compress is None`): `before_step` is a silent no-op — the
    field is left unchanged, no exception propagates.
(d) An internal error (mocked `compress` raises) is swallowed — `before_step`
    never propagates.
(e) Loading via the real `PluginManager` local-file discovery mechanism
    (mirrors `tests/test_rtk.py` / `tests/test_plugin_obsidian.py`) registers
    the `before_step` hook into `pm.hooks`.

Covers, per the post-review fix-up:
(f) `headroom_enabled` opt-in gate: default False -> no-op; True -> compresses.
(g) Idempotency: `_execute_task` (`hivepilot/orchestrator.py`) builds ONE
    `metadata` dict per task and reuses it BY REFERENCE across every step's
    `RunnerPayload` — a naive `before_step` would re-compress
    already-compressed text on step 2, degrading unbounded. A sentinel key
    (`_headroom_compressed`) on the shared `metadata` dict makes
    `before_step` skip compression once it has already run for that dict.
(h) Non-shrinking guard: if `compress()` returns text that isn't actually
    shorter, the original field is kept unchanged.
(i) Non-string `prior_context`/`extra_prompt` values are skipped (the
    `isinstance(..., str)` guard).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner

REPO_ROOT = Path(__file__).parent.parent
HEADROOM_PLUGIN_PATH = REPO_ROOT / "plugins" / "headroom.py"


def _load_headroom_module() -> ModuleType:
    """Load plugins/headroom.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (no dependency on `plugins`
    being an importable package on sys.path)."""
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_headroom_test", HEADROOM_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def headroom_module() -> ModuleType:
    return _load_headroom_module()


@pytest.fixture(autouse=True)
def _headroom_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`headroom_enabled` defaults to False (ships dormant). Every test in
    this module except `TestHeadroomEnabledGate` exercises compression
    behavior, so enable the opt-in flag by default here; the gate tests
    override it explicitly per-test."""
    monkeypatch.setattr(settings, "headroom_enabled", True, raising=False)


def _payload(tmp_path: Path, **metadata: object) -> RunnerPayload:
    return RunnerPayload(
        project_name="proj",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata=dict(metadata),
        secrets={},
    )


class TestRegister:
    def test_register_exposes_before_step_hook(self, headroom_module: ModuleType) -> None:
        hooks = headroom_module.register()
        assert hooks["before_step"] is headroom_module.before_step

    def test_register_exposes_health_check(self, headroom_module: ModuleType) -> None:
        hooks = headroom_module.register()
        assert "health" in hooks
        assert hooks["health"]["headroom"] is headroom_module.health


class TestHealth:
    """Sprint 2 (plugin-health): `health()` reflects lib-importable +
    `settings.headroom_enabled`. Note: the module-level `_headroom_enabled_by_
    default` autouse fixture sets `headroom_enabled=True` for every test in
    this file except `TestHeadroomEnabledGate` — these tests override it
    explicitly per-case, same pattern that class already uses."""

    def test_error_when_lib_missing(self, headroom_module: ModuleType) -> None:
        with patch.object(headroom_module, "compress", None):
            result = headroom_module.health()
        assert result.status == "error"

    def test_degraded_when_installed_but_disabled(
        self, headroom_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "headroom_enabled", False, raising=False)
        with patch.object(headroom_module, "compress", MagicMock()):
            result = headroom_module.health()
        assert result.status == "degraded"
        assert "disabled" in result.detail

    def test_ok_when_installed_and_enabled(
        self, headroom_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "headroom_enabled", True, raising=False)
        with patch.object(headroom_module, "compress", MagicMock()):
            result = headroom_module.health()
        assert result.status == "ok"


class TestBeforeStepCompressesInPlace:
    def test_compresses_prior_context_and_logs_ratio(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        original = "x" * 1000
        payload = _payload(tmp_path, prior_context=original)

        with (
            patch.object(headroom_module, "compress", return_value="x" * 100) as mock_compress,
            patch.object(headroom_module, "logger", MagicMock()) as mock_logger,
        ):
            headroom_module.before_step(payload=payload)

        assert mock_compress.called
        assert payload.metadata["prior_context"] == "x" * 100
        assert mock_logger.info.called
        call_kwargs = mock_logger.info.call_args.kwargs
        assert call_kwargs["chars_before"] == 1000
        assert call_kwargs["chars_after"] == 100

    def test_compresses_extra_prompt_too(self, headroom_module: ModuleType, tmp_path: Path) -> None:
        payload = _payload(tmp_path, extra_prompt="y" * 500)

        with patch.object(headroom_module, "compress", return_value="y" * 50):
            headroom_module.before_step(payload=payload)

        assert payload.metadata["extra_prompt"] == "y" * 50

    def test_no_compressible_fields_is_a_noop(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path)

        with patch.object(headroom_module, "compress", return_value="unused") as mock_compress:
            headroom_module.before_step(payload=payload)

        assert not mock_compress.called
        assert payload.metadata == {}


class TestLibAbsentIsNoop:
    def test_compress_none_leaves_field_unchanged(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, prior_context="unchanged")

        with patch.object(headroom_module, "compress", None):
            headroom_module.before_step(payload=payload)  # must not raise

        assert payload.metadata["prior_context"] == "unchanged"

    def test_missing_payload_kwarg_is_a_noop(self, headroom_module: ModuleType) -> None:
        with patch.object(headroom_module, "compress", return_value="whatever"):
            headroom_module.before_step()  # must not raise, keyword-tolerant


class TestInternalErrorIsSwallowed:
    def test_compress_raising_does_not_propagate(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, prior_context="some context")

        def _boom(*args: object, **kwargs: object) -> str:
            raise RuntimeError("headroom internal failure")

        with (
            patch.object(headroom_module, "compress", side_effect=_boom),
            patch.object(headroom_module, "logger", MagicMock()) as mock_logger,
        ):
            headroom_module.before_step(payload=payload)  # must not raise

        assert payload.metadata["prior_context"] == "some context"
        assert mock_logger.warning.called


class TestPluginManagerDiscoversHeadroom:
    def test_plugin_manager_registers_before_step_hook(self, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert any(
            getattr(hook, "__module__", "").startswith("hivepilot_plugin_headroom")
            for hook in pm.hooks.get("before_step", [])
        )
        assert any(r.source == "local-file" and r.name == "headroom" for r in pm.loaded)


class TestHeadroomEnabledGate:
    """`headroom_enabled` defaults to False — the plugin ships dormant even
    when the file is present and `headroom` is installed."""

    def test_disabled_by_default_is_a_noop(
        self, headroom_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "headroom_enabled", False, raising=False)
        payload = _payload(tmp_path, prior_context="x" * 1000)

        with patch.object(headroom_module, "compress", return_value="x" * 100) as mock_compress:
            headroom_module.before_step(payload=payload)

        assert not mock_compress.called
        assert payload.metadata["prior_context"] == "x" * 1000

    def test_enabled_compresses(
        self, headroom_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "headroom_enabled", True, raising=False)
        payload = _payload(tmp_path, prior_context="x" * 1000)

        with patch.object(headroom_module, "compress", return_value="x" * 100) as mock_compress:
            headroom_module.before_step(payload=payload)

        assert mock_compress.called
        assert payload.metadata["prior_context"] == "x" * 100


class TestIdempotencyAcrossSharedMetadataDict:
    """`_execute_task` (`hivepilot/orchestrator.py:1825`) builds ONE `metadata`
    dict per task and reuses it BY REFERENCE for every step's `RunnerPayload`
    (`hivepilot/orchestrator.py:1915-1922`). Compressing on every `before_step`
    call would re-compress already-compressed text on step 2+, degrading
    unbounded. A sentinel key on the shared dict must make the second call a
    no-op."""

    def test_compress_called_once_across_two_payloads_sharing_metadata(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        shared_metadata = {"prior_context": "x" * 1000}
        payload_step_1 = RunnerPayload(
            project_name="proj",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="step-1", runner="claude"),
            metadata=shared_metadata,
            secrets={},
        )
        payload_step_2 = RunnerPayload(
            project_name="proj",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="step-2", runner="claude"),
            metadata=shared_metadata,  # SAME dict object, mirrors _execute_task
            secrets={},
        )

        with patch.object(headroom_module, "compress", return_value="x" * 100) as mock_compress:
            headroom_module.before_step(payload=payload_step_1)
            headroom_module.before_step(payload=payload_step_2)

        assert mock_compress.call_count == 1
        assert shared_metadata["prior_context"] == "x" * 100

    def test_sentinel_key_set_after_first_call(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, prior_context="x" * 1000)

        with patch.object(headroom_module, "compress", return_value="x" * 100):
            headroom_module.before_step(payload=payload)

        assert payload.metadata[headroom_module._SENTINEL_KEY] is True


class TestNonShrinkingGuard:
    def test_non_shrinking_result_keeps_original(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        original = "x" * 100
        payload = _payload(tmp_path, prior_context=original)

        # "compressed" text that is NOT actually shorter (e.g. headroom added
        # framing/markup that made it longer) must not overwrite the field.
        with patch.object(headroom_module, "compress", return_value="x" * 150):
            headroom_module.before_step(payload=payload)

        assert payload.metadata["prior_context"] == original

    def test_equal_length_result_keeps_original(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        original = "x" * 100
        payload = _payload(tmp_path, prior_context=original)

        with patch.object(headroom_module, "compress", return_value="y" * 100):
            headroom_module.before_step(payload=payload)

        assert payload.metadata["prior_context"] == original


class TestNonStringValuesAreSkipped:
    def test_non_string_prior_context_is_skipped(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, prior_context={"not": "a string"})

        with patch.object(headroom_module, "compress", return_value="unused") as mock_compress:
            headroom_module.before_step(payload=payload)

        assert not mock_compress.called
        assert payload.metadata["prior_context"] == {"not": "a string"}

    def test_non_string_extra_prompt_is_skipped(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt=12345)

        with patch.object(headroom_module, "compress", return_value="unused") as mock_compress:
            headroom_module.before_step(payload=payload)

        assert not mock_compress.called
        assert payload.metadata["extra_prompt"] == 12345


class TestSentinelKeyNeverRenderedIntoPrompt:
    """Confirms the `_headroom_compressed` sentinel key left on
    `payload.metadata` is safe: `ClaudeRunner._build_prompt`
    (`hivepilot/runners/claude_runner.py`) only reads the specific
    `extra_prompt` / `prior_context` keys off `payload.metadata` — it never
    iterates/dumps the dict as a whole — so the sentinel never reaches the
    rendered prompt text."""

    def test_sentinel_key_absent_from_rendered_prompt(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, prior_context="x" * 1000, extra_prompt="do the thing")

        with patch.object(headroom_module, "compress", return_value="x" * 100):
            headroom_module.before_step(payload=payload)

        assert headroom_module._SENTINEL_KEY in payload.metadata

        runner = ClaudeRunner.__new__(ClaudeRunner)
        runner.settings = settings
        runner.definition = RunnerDefinition(kind="claude")
        prompt = ClaudeRunner._build_prompt(runner, payload, "instructions", None)

        assert headroom_module._SENTINEL_KEY not in prompt
        assert "_headroom_compressed" not in prompt
