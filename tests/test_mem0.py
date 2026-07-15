"""
Tests for the `mem0` plugin (Sprint 3b of the plugins plan).

`plugins/mem0.py` is a local-file plugin (see docs/v4/PLUGINS.md) that gives
agents persistent cross-run memory via `mem0` (`pip install mem0ai` — NOT a
hivepilot dependency, never installed by this plugin; mocked throughout this
module). It mirrors `plugins/headroom.py`'s proven shape (opt-in gate,
lazy import, never-raise, sentinel-guarded idempotency on the shared
`metadata` dict) but wires TWO lifecycle hooks instead of one:

- `before_step` -> `recall`: search mem0 for memories relevant to this
  project/task and inject them into `RunnerPayload.metadata["extra_prompt"]`
  — the same field `ClaudeRunner._build_prompt`
  (`hivepilot/runners/claude_runner.py`) reads verbatim off the SAME
  `payload` object the orchestrator hands to the hook
  (`Orchestrator._execute_task`, `hivepilot/orchestrator.py`,
  `self.plugins.run_hook("before_step", payload=payload)`), so an in-place
  mutation here is picked up by the runner with no copy in between — exactly
  headroom's mechanism.
- `after_step` -> `store`: persist available salient content back to mem0.

Step 0 findings baked into this test module (see plugins/mem0.py module
docstring for the full trace):

(1) Recall injection field is `metadata["extra_prompt"]` (confirmed via
    `ClaudeRunner._build_prompt`, same mechanism headroom uses for
    `prior_context`/`extra_prompt`).
(2) `after_step` (`hivepilot/orchestrator.py:2027`,
    `self.plugins.run_hook("after_step", payload=payload)`) is called with
    the SAME `payload` object passed to `before_step` — the runner's return
    value is appended to a local `outputs` list inside `_execute_task` and
    is NEVER attached to `payload` or threaded into the `after_step` kwargs.
    `on_pipeline_end` (`hivepilot/orchestrator.py:1445`) is even sparser
    (`run_id`/`pipeline`/`status` only). So `store()` cannot persist the
    step's actual OUTPUT — it persists task/step identity + the original
    `extra_prompt` (captured before `recall` mutates it) + `prior_context`,
    which is documented as a known limitation, not fabricated as real output
    access.
(3) The `_mem0_recalled` sentinel key is `_`-prefixed and never rendered
    into a built prompt (`TestSentinelKeyNeverRenderedIntoPrompt` below,
    mirrors headroom's own test of the same shape).

Covers, per the sprint spec:
(a) `register()` exposes `before_step` (recall) and `after_step` (store).
(b) Opt-in gate: `mem0_enabled` defaults False -> recall/store never touch
    mem0; True -> they do.
(c) `recall` injects memories into `extra_prompt` in place (mocked
    `search()`), idempotent across two payloads sharing one metadata dict
    (search called once total).
(d) `recall` is a no-op when the library isn't installed / not configured.
(e) `recall`/`store` never raise, even when the mocked client raises.
(f) `store` calls `client.add(...)` with the available (non-output) content.
(g) Real `PluginManager` local-file discovery registers both hooks.
(h) Sentinel key never reaches a rendered prompt.
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
MEM0_PLUGIN_PATH = REPO_ROOT / "plugins" / "mem0.py"


def _load_mem0_module() -> ModuleType:
    """Load plugins/mem0.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (no dependency on `plugins`
    being an importable package on sys.path)."""
    spec = importlib.util.spec_from_file_location("hivepilot_plugin_mem0_test", MEM0_PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mem0_module() -> ModuleType:
    return _load_mem0_module()


@pytest.fixture(autouse=True)
def _mem0_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mem0_enabled` defaults to False (ships dormant). Every test in this
    module except `TestMem0EnabledGate` exercises recall/store behavior, so
    enable the opt-in flag by default here; the gate tests override it
    explicitly per-test."""
    monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)


def _payload(tmp_path: Path, **metadata: object) -> RunnerPayload:
    return RunnerPayload(
        project_name="proj",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata=dict(metadata),
        secrets={},
    )


class TestGetClient:
    """`_get_client()` selects the hosted (`MemoryClient`) or self-host
    (`Memory`) backend based on Settings, and degrades to `None` rather than
    raising when the library isn't installed or construction fails."""

    def test_hosted_path_uses_memory_client_with_api_key(
        self, mem0_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "mem0_api_key", "mk-test-123", raising=False)
        mock_memory_client_cls = MagicMock()
        with patch.object(mem0_module, "MemoryClient", mock_memory_client_cls):
            client = mem0_module._get_client()

        mock_memory_client_cls.assert_called_once_with(api_key="mk-test-123")
        assert client is mock_memory_client_cls.return_value

    def test_self_host_path_uses_memory_when_no_api_key(
        self, mem0_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "mem0_api_key", None, raising=False)
        monkeypatch.setattr(settings, "mem0_config", None, raising=False)
        mock_memory_cls = MagicMock()
        with patch.object(mem0_module, "Memory", mock_memory_cls):
            client = mem0_module._get_client()

        mock_memory_cls.assert_called_once_with()
        assert client is mock_memory_cls.return_value

    def test_self_host_path_uses_from_config_when_mem0_config_set(
        self, mem0_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "mem0_api_key", None, raising=False)
        monkeypatch.setattr(
            settings, "mem0_config", {"vector_store": {"provider": "chroma"}}, raising=False
        )
        mock_memory_cls = MagicMock()
        with patch.object(mem0_module, "Memory", mock_memory_cls):
            client = mem0_module._get_client()

        mock_memory_cls.from_config.assert_called_once_with(
            {"vector_store": {"provider": "chroma"}}
        )
        assert client is mock_memory_cls.from_config.return_value

    def test_returns_none_when_memory_client_absent_and_api_key_set(
        self, mem0_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "mem0_api_key", "mk-test-123", raising=False)
        with patch.object(mem0_module, "MemoryClient", None):
            client = mem0_module._get_client()

        assert client is None

    def test_returns_none_when_memory_absent_and_no_api_key(
        self, mem0_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "mem0_api_key", None, raising=False)
        with patch.object(mem0_module, "Memory", None):
            client = mem0_module._get_client()

        assert client is None

    def test_construction_failure_returns_none_not_raise(
        self, mem0_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "mem0_api_key", None, raising=False)
        monkeypatch.setattr(settings, "mem0_config", None, raising=False)
        mock_memory_cls = MagicMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(mem0_module, "Memory", mock_memory_cls),
            patch.object(mem0_module, "logger", MagicMock()) as mock_logger,
        ):
            client = mem0_module._get_client()

        assert client is None
        assert mock_logger.warning.called


class TestRegister:
    def test_register_exposes_before_and_after_step_hooks(self, mem0_module: ModuleType) -> None:
        hooks = mem0_module.register()
        assert hooks["before_step"] is mem0_module.recall
        assert hooks["after_step"] is mem0_module.store

    def test_register_exposes_health_check(self, mem0_module: ModuleType) -> None:
        hooks = mem0_module.register()
        assert "health" in hooks
        assert hooks["health"]["mem0"] is mem0_module.health


class TestHealth:
    """Sprint 2 (plugin-health): `health()` reflects lib-importable +
    `mem0_enabled` + client-buildable — never a secret/token value in the
    detail (Phase 19 discipline), only presence/mode booleans."""

    def test_error_when_lib_missing(self, mem0_module: ModuleType) -> None:
        with (
            patch.object(mem0_module, "Memory", None),
            patch.object(mem0_module, "MemoryClient", None),
        ):
            result = mem0_module.health()
        assert result.status == "error"
        assert "not installed" in result.detail

    def test_degraded_when_installed_but_disabled(
        self, mem0_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "mem0_enabled", False, raising=False)
        with patch.object(mem0_module, "Memory", MagicMock()):
            result = mem0_module.health()
        assert result.status == "degraded"
        assert "disabled" in result.detail

    def test_error_when_enabled_but_client_unbuildable(
        self, mem0_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        with patch.object(mem0_module, "_get_client", return_value=None):
            result = mem0_module.health()
        assert result.status == "error"

    def test_ok_hosted_mode_when_api_key_set(
        self, mem0_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        monkeypatch.setattr(settings, "mem0_api_key", "mk-super-secret-token", raising=False)
        mock_client = MagicMock()
        with (
            patch.object(mem0_module, "Memory", MagicMock()),
            patch.object(mem0_module, "_get_client", return_value=mock_client),
        ):
            result = mem0_module.health()
        assert result.status == "ok"
        assert "mk-super-secret-token" not in result.detail

    def test_ok_self_host_mode_when_no_api_key(
        self, mem0_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        monkeypatch.setattr(settings, "mem0_api_key", None, raising=False)
        mock_client = MagicMock()
        with (
            patch.object(mem0_module, "Memory", MagicMock()),
            patch.object(mem0_module, "_get_client", return_value=mock_client),
        ):
            result = mem0_module.health()
        assert result.status == "ok"
        assert "self-host" in result.detail

    def test_never_leaks_api_key_across_all_health_paths(
        self, mem0_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No health detail, across any status, ever contains the configured
        mem0 API key/token — even when it's set (Phase 19 no-leak discipline).
        Covers the `ok`/`degraded` branches (lib present, client buildable) —
        see `test_never_leaks_api_key_in_error_branches` below for the two
        `error` branches (lib missing / client unbuildable)."""
        secret_token = "mk-this-must-never-appear-anywhere"
        monkeypatch.setattr(settings, "mem0_api_key", secret_token, raising=False)

        for enabled in (True, False):
            monkeypatch.setattr(settings, "mem0_enabled", enabled, raising=False)
            with (
                patch.object(mem0_module, "Memory", MagicMock()),
                patch.object(mem0_module, "_get_client", return_value=MagicMock()),
            ):
                result = mem0_module.health()
            assert secret_token not in result.detail

    def test_never_leaks_api_key_in_error_branches(
        self, mem0_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sibling of `test_never_leaks_api_key_across_all_health_paths`,
        covering the two `error` branches specifically — a raising/degraded
        path is exactly where a careless implementation might dump the
        exception/config context (and, with it, a secret) into `detail`."""
        secret_token = "mk-this-must-never-appear-anywhere-either"
        monkeypatch.setattr(settings, "mem0_api_key", secret_token, raising=False)
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)

        # error branch: lib missing (Memory and MemoryClient both None).
        with (
            patch.object(mem0_module, "Memory", None),
            patch.object(mem0_module, "MemoryClient", None),
        ):
            result = mem0_module.health()
        assert result.status == "error"
        assert secret_token not in result.detail

        # error branch: lib present + enabled, but the client can't be built.
        with (
            patch.object(mem0_module, "Memory", MagicMock()),
            patch.object(mem0_module, "_get_client", return_value=None),
        ):
            result = mem0_module.health()
        assert result.status == "error"
        assert secret_token not in result.detail


class TestRecallInjectsMemories:
    def test_recall_injects_into_extra_prompt_in_place(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path)
        mock_client = MagicMock()
        mock_client.search.return_value = [{"memory": "user prefers concise commits"}]

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.recall(payload=payload)

        assert mock_client.search.called
        assert "user prefers concise commits" in payload.metadata["extra_prompt"]

    def test_recall_appends_to_existing_extra_prompt(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt="do the thing")
        mock_client = MagicMock()
        mock_client.search.return_value = [{"memory": "past outcome: shipped fine"}]

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.recall(payload=payload)

        assert "do the thing" in payload.metadata["extra_prompt"]
        assert "past outcome: shipped fine" in payload.metadata["extra_prompt"]

    def test_no_memories_found_leaves_extra_prompt_untouched(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt="do the thing")
        mock_client = MagicMock()
        mock_client.search.return_value = []

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.recall(payload=payload)

        assert payload.metadata["extra_prompt"] == "do the thing"

    def test_recall_saves_original_extra_prompt_for_store(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        """Before mutating extra_prompt, recall snapshots the original value
        under a private key so `store()` doesn't re-persist mem0's own
        recalled-memories block back into mem0 (a feedback loop)."""
        payload = _payload(tmp_path, extra_prompt="original instructions")
        mock_client = MagicMock()
        mock_client.search.return_value = [{"memory": "some memory"}]

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.recall(payload=payload)

        assert payload.metadata[mem0_module._ORIGINAL_EXTRA_PROMPT_KEY] == "original instructions"


class TestRecallIdempotency:
    def test_search_called_once_across_two_payloads_sharing_metadata(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        shared_metadata: dict[str, object] = {}
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
        mock_client = MagicMock()
        mock_client.search.return_value = [{"memory": "one memory"}]

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.recall(payload=payload_step_1)
            mem0_module.recall(payload=payload_step_2)

        assert mock_client.search.call_count == 1
        assert shared_metadata[mem0_module._SENTINEL_KEY] is True

    def test_sentinel_key_set_after_recall(self, mem0_module: ModuleType, tmp_path: Path) -> None:
        payload = _payload(tmp_path)
        mock_client = MagicMock()
        mock_client.search.return_value = []

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.recall(payload=payload)

        assert payload.metadata[mem0_module._SENTINEL_KEY] is True


class TestRecallLibAbsentOrUnconfiguredIsNoop:
    def test_no_client_leaves_payload_untouched(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt="untouched")

        with patch.object(mem0_module, "_get_client", return_value=None):
            mem0_module.recall(payload=payload)  # must not raise

        assert payload.metadata["extra_prompt"] == "untouched"

    def test_missing_payload_kwarg_is_a_noop(self, mem0_module: ModuleType) -> None:
        mock_client = MagicMock()
        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.recall()  # must not raise, keyword-tolerant

        assert not mock_client.search.called


class TestRecallInternalErrorIsSwallowed:
    def test_search_raising_does_not_propagate(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt="untouched")
        mock_client = MagicMock()
        mock_client.search.side_effect = RuntimeError("mem0 internal failure")

        with (
            patch.object(mem0_module, "_get_client", return_value=mock_client),
            patch.object(mem0_module, "logger", MagicMock()) as mock_logger,
        ):
            mem0_module.recall(payload=payload)  # must not raise

        assert payload.metadata["extra_prompt"] == "untouched"
        assert mock_logger.warning.called


class TestMem0EnabledGate:
    """`mem0_enabled` defaults to False — the plugin ships dormant even when
    the file is present and `mem0ai` is installed."""

    def test_disabled_by_default_recall_is_a_noop(
        self, mem0_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "mem0_enabled", False, raising=False)
        payload = _payload(tmp_path)
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.recall(payload=payload)

        assert not mock_client.search.called
        assert "extra_prompt" not in payload.metadata

    def test_disabled_by_default_store_is_a_noop(
        self, mem0_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "mem0_enabled", False, raising=False)
        payload = _payload(tmp_path, prior_context="some prior context")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload)

        assert not mock_client.add.called

    def test_enabled_recall_calls_search(
        self, mem0_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        payload = _payload(tmp_path)
        mock_client = MagicMock()
        mock_client.search.return_value = []

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.recall(payload=payload)

        assert mock_client.search.called


class TestStorePersistsAvailableContent:
    """`store()` (`after_step`) cannot see the step's real output (see the
    module docstring's Step 0 findings) — it persists task/step identity
    plus the original `extra_prompt` / `prior_context` context fields."""

    def test_store_calls_add_with_available_content(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt="original ask", prior_context="upstream output")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload)

        assert mock_client.add.called
        stored_text = mock_client.add.call_args.args[0]
        assert "original ask" in stored_text
        assert "upstream output" in stored_text
        assert payload.task_name in stored_text

    def test_store_prefers_original_extra_prompt_over_recalled_block(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        """Avoids a feedback loop: if recall already injected a "Relevant
        memories" block into extra_prompt, store() must persist the ORIGINAL
        user instructions (snapshotted by recall), not the recalled block
        mem0 itself produced."""
        payload = _payload(tmp_path, extra_prompt="original ask")
        recall_client = MagicMock()
        recall_client.search.return_value = [{"memory": "a recalled memory"}]
        with patch.object(mem0_module, "_get_client", return_value=recall_client):
            mem0_module.recall(payload=payload)

        assert "a recalled memory" in payload.metadata["extra_prompt"]

        store_client = MagicMock()
        with patch.object(mem0_module, "_get_client", return_value=store_client):
            mem0_module.store(payload=payload)

        stored_text = store_client.add.call_args.args[0]
        assert "original ask" in stored_text
        assert "a recalled memory" not in stored_text

    def test_store_no_salient_content_is_a_noop(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path)
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload)

        assert not mock_client.add.called

    def test_store_no_client_is_a_noop(self, mem0_module: ModuleType, tmp_path: Path) -> None:
        payload = _payload(tmp_path, prior_context="something")

        with patch.object(mem0_module, "_get_client", return_value=None):
            mem0_module.store(payload=payload)  # must not raise

    def test_store_missing_payload_kwarg_is_a_noop(self, mem0_module: ModuleType) -> None:
        mock_client = MagicMock()
        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store()  # must not raise, keyword-tolerant

        assert not mock_client.add.called


class TestStorePersistsRealOutput:
    """`store()` (`after_step`) now persists the step's real `output` when
    the caller supplies it (threaded in by `Orchestrator._execute_task` —
    hook-context-enrichment), in addition to the existing input-context
    fallback (`extra_prompt` / `prior_context`)."""

    def test_store_with_output_kwarg_persists_it(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path)
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload, output="the agent's real result")

        assert mock_client.add.called
        stored_text = mock_client.add.call_args.args[0]
        assert "the agent's real result" in stored_text

    def test_store_with_output_and_extra_prompt_persists_both(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt="original ask")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload, output="real outcome")

        stored_text = mock_client.add.call_args.args[0]
        assert "original ask" in stored_text
        assert "real outcome" in stored_text

    def test_store_without_output_kwarg_falls_back_as_before(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt="original ask", prior_context="upstream output")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload)

        stored_text = mock_client.add.call_args.args[0]
        assert "original ask" in stored_text
        assert "upstream output" in stored_text
        assert "output:" not in stored_text

    def test_store_non_string_output_is_ignored(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt="original ask")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload, output=None)

        stored_text = mock_client.add.call_args.args[0]
        assert "output:" not in stored_text


class TestMemoryKeyIncludesRole:
    """`_memory_key` (and therefore `recall`/`store`) include `role` in the
    mem0 `user_id` key when the caller supplies it (threaded in by
    `Orchestrator._execute_task` — hook-context-enrichment), falling back to
    the original `project:task` key when absent."""

    def test_memory_key_with_role(self, mem0_module: ModuleType, tmp_path: Path) -> None:
        payload = _payload(tmp_path)
        assert mem0_module._memory_key(payload, "developer") == "proj:t:developer"

    def test_memory_key_without_role_falls_back(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path)
        assert mem0_module._memory_key(payload, None) == "proj:t"
        assert mem0_module._memory_key(payload) == "proj:t"

    def test_recall_uses_role_in_search_user_id(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path)
        mock_client = MagicMock()
        mock_client.search.return_value = []

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.recall(payload=payload, role="developer")

        assert mock_client.search.call_args.kwargs["user_id"] == "proj:t:developer"

    def test_store_uses_role_in_add_user_id(self, mem0_module: ModuleType, tmp_path: Path) -> None:
        payload = _payload(tmp_path, extra_prompt="ask")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload, role="developer")

        assert mock_client.add.call_args.kwargs["user_id"] == "proj:t:developer"

    def test_recall_and_store_use_matching_key_when_role_supplied(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        """Recall/store must agree on the same key so stored memories are
        actually found again on a later recall — the whole point of keying
        by role."""
        payload = _payload(tmp_path, extra_prompt="ask")
        recall_client = MagicMock()
        recall_client.search.return_value = []
        with patch.object(mem0_module, "_get_client", return_value=recall_client):
            mem0_module.recall(payload=payload, role="developer")

        store_client = MagicMock()
        with patch.object(mem0_module, "_get_client", return_value=store_client):
            mem0_module.store(payload=payload, role="developer")

        assert (
            recall_client.search.call_args.kwargs["user_id"]
            == store_client.add.call_args.kwargs["user_id"]
        )

    def test_recall_without_role_kwarg_falls_back_as_before(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path)
        mock_client = MagicMock()
        mock_client.search.return_value = []

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.recall(payload=payload)

        assert mock_client.search.call_args.kwargs["user_id"] == "proj:t"


class TestStoreProvenanceMetadata:
    """`store()` (Sprint 1 of the mem0-typed-and-plugin-health spec) attaches
    a structured PROVENANCE `metadata` dict to `client.add(...)` — real
    values only, no fabrication. See `plugins/mem0.py::_provenance_metadata`
    for the exact rules on which keys are included vs. omitted."""

    def test_store_passes_provenance_metadata_to_add(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt="ask")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload, role="developer")

        assert mock_client.add.called
        metadata = mock_client.add.call_args.kwargs["metadata"]
        assert metadata["source"] == "hivepilot"
        assert metadata["project"] == "proj"
        assert metadata["task"] == "t"
        assert metadata["role"] == "developer"
        assert isinstance(metadata["ts"], str) and metadata["ts"]

    def test_store_ts_is_a_valid_iso8601_utc_timestamp(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        from datetime import datetime

        payload = _payload(tmp_path, extra_prompt="ask")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload)

        metadata = mock_client.add.call_args.kwargs["metadata"]
        parsed = datetime.fromisoformat(metadata["ts"])
        assert parsed.tzinfo is not None

    def test_store_omits_role_when_not_supplied(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt="ask")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload)

        metadata = mock_client.add.call_args.kwargs["metadata"]
        assert "role" not in metadata

    def test_store_never_fabricates_confidence(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt="ask")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload, role="developer", output="result")

        metadata = mock_client.add.call_args.kwargs["metadata"]
        assert "confidence" not in metadata

    def test_store_omits_run_id_when_not_threaded(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        """`run_id` isn't threaded into the `after_step` `run_hook(...)` call
        by `Orchestrator._execute_task` today (only `payload`/`dry_run`/
        `role`/`output` are) — a real, unavailable value must be OMITTED,
        never sent as `None`."""
        payload = _payload(tmp_path, extra_prompt="ask")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload, run_id=42)  # even if a caller passes it

        metadata = mock_client.add.call_args.kwargs["metadata"]
        assert "run_id" not in metadata

    def test_store_includes_step_name_when_present(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt="ask")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload)

        metadata = mock_client.add.call_args.kwargs["metadata"]
        assert metadata["step"] == "s"

    def test_store_category_defaults_to_run(self, mem0_module: ModuleType, tmp_path: Path) -> None:
        payload = _payload(tmp_path, extra_prompt="ask")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload)

        metadata = mock_client.add.call_args.kwargs["metadata"]
        assert metadata["category"] == "run"

    def test_store_category_from_step_metadata_when_set(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = RunnerPayload(
            project_name="proj",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="s", runner="claude", metadata={"memory_category": "incident"}),
            metadata={"extra_prompt": "ask"},
            secrets={},
        )
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload)

        metadata = mock_client.add.call_args.kwargs["metadata"]
        assert metadata["category"] == "incident"

    def test_store_provenance_metadata_gate_and_never_raise_still_hold(
        self, mem0_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new `metadata` kwarg doesn't disturb the opt-in gate or the
        never-raise contract."""
        monkeypatch.setattr(settings, "mem0_enabled", False, raising=False)
        payload = _payload(tmp_path, extra_prompt="ask")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload)  # must not raise

        assert not mock_client.add.called

        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        mock_client.add.side_effect = RuntimeError("boom")
        with (
            patch.object(mem0_module, "_get_client", return_value=mock_client),
            patch.object(mem0_module, "logger", MagicMock()) as mock_logger,
        ):
            mem0_module.store(payload=payload)  # must not raise

        assert mock_logger.warning.called

    def test_store_provenance_metadata_never_contains_content_keys(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        """Defense-in-depth: the provenance `metadata` dict must never carry
        memory CONTENT keys (`output`/`extra_prompt`/`prior_context` — those
        belong exclusively in the `content` string built from
        `content_parts`). Structurally impossible today (`_provenance_metadata`
        never reads those fields), but this guards against a future refactor
        that accidentally merges the content dict and the provenance dict."""
        payload = _payload(tmp_path, extra_prompt="original ask", prior_context="upstream")
        mock_client = MagicMock()

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload, role="developer", output="the agent's real result")

        metadata = mock_client.add.call_args.kwargs["metadata"]
        assert not ({"output", "extra_prompt", "prior_context"} & set(metadata.keys()))


class TestStoreInternalErrorIsSwallowed:
    def test_add_raising_does_not_propagate(self, mem0_module: ModuleType, tmp_path: Path) -> None:
        payload = _payload(tmp_path, prior_context="something salient")
        mock_client = MagicMock()
        mock_client.add.side_effect = RuntimeError("mem0 internal failure")

        with (
            patch.object(mem0_module, "_get_client", return_value=mock_client),
            patch.object(mem0_module, "logger", MagicMock()) as mock_logger,
        ):
            mem0_module.store(payload=payload)  # must not raise

        assert mock_logger.warning.called


class TestPluginManagerDiscoversMem0:
    def test_plugin_manager_registers_both_hooks(self, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert any(
            getattr(hook, "__module__", "").startswith("hivepilot_plugin_mem0")
            for hook in pm.hooks.get("before_step", [])
        )
        assert any(
            getattr(hook, "__module__", "").startswith("hivepilot_plugin_mem0")
            for hook in pm.hooks.get("after_step", [])
        )
        assert any(r.source == "local-file" and r.name == "mem0" for r in pm.loaded)


class TestSentinelKeyNeverRenderedIntoPrompt:
    """Confirms the `_mem0_recalled` sentinel key (and the private
    `_mem0_original_extra_prompt` snapshot key) left on `payload.metadata`
    are safe: `ClaudeRunner._build_prompt` only reads the specific
    `extra_prompt` / `prior_context` keys off `payload.metadata` — it never
    iterates/dumps the dict as a whole — so neither private key ever reaches
    the rendered prompt text."""

    def test_private_keys_absent_from_rendered_prompt(
        self, mem0_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, extra_prompt="do the thing", prior_context="upstream")
        mock_client = MagicMock()
        mock_client.search.return_value = [{"memory": "a memory"}]

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.recall(payload=payload)

        assert mem0_module._SENTINEL_KEY in payload.metadata
        assert mem0_module._ORIGINAL_EXTRA_PROMPT_KEY in payload.metadata

        runner = ClaudeRunner.__new__(ClaudeRunner)
        runner.settings = settings
        runner.definition = RunnerDefinition(kind="claude")
        prompt = ClaudeRunner._build_prompt(runner, payload, "instructions", None)

        assert mem0_module._SENTINEL_KEY not in prompt
        assert mem0_module._ORIGINAL_EXTRA_PROMPT_KEY not in prompt
        assert "_mem0_recalled" not in prompt
