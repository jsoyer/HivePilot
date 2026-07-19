"""
Tests for Sprint 1 (auto-learning-lessons-loop PRD) — closing the store()
redaction hole.

Before this sprint, `Orchestrator._execute_task_body`'s `after_step`
`run_hook(...)` call (`hivepilot/orchestrator.py`) handed persistence hooks
(`plugins/mem0.py::store`, `plugins/obsidian.py::store`, and any future
lesson-distillation sink) the step's real captured `output` plus the live
`payload.metadata` (`extra_prompt`/`prior_context`) completely UNREDACTED —
unlike every other sink downstream of a resolved `${secret:NAME}` value
(`record_interaction`/`record_step`/`record_verdict`/exception logging),
which all route through `redact_text`/the resolved-secrets masking registry
(`hivepilot/services/config_provenance.py`) first. A resolved secret echoed
into a step's output or prompt context could therefore reach an external
mem0 store or a plaintext Obsidian vault note verbatim.

A follow-up opus adversarial review additionally caught that the hook-facing
payload copy still carried `payload.secrets` — the RAW `{ENV_NAME:
resolved_value}` map itself, not text `redact_text`/`redact_value` can mask —
verbatim. That is closed too: the hook-facing copy blanks `secrets={}`
unconditionally.

Covers THREE layers, per the sprint spec:
(A) The orchestrator `after_step` choke point (`_execute_task_body`) redacts
    `output` and `payload.metadata` (via `redact_value`, recursive) into a
    COPY before the hook fires — the shared `metadata` dict (reused across
    every step in the task, see `payload = RunnerPayload(...,
    metadata=metadata, ...)`) is never mutated in place, so later steps'
    real prompts are unaffected.
(B) Defense-in-depth inside `plugins/mem0.py::store` and
    `plugins/obsidian.py::store` — both redact the content they persist
    even if a future/other caller invokes `store()` directly without going
    through the orchestrator choke.
(C) `payload.secrets` is blanked (`secrets={}`) on the hook-facing copy for
    BOTH `before_step` and `after_step` — the raw resolved-secret env map
    must never reach a hook, regardless of whether the surrounding text was
    redacted. `before_step`'s copy keeps its `metadata` as the SAME dict
    reference as the live payload (no `metadata=` override), preserving
    mem0/obsidian `recall`'s in-place-mutation injection contract — see
    `tests/test_mem0.py` / `tests/test_plugin_obsidian_brain.py`'s recall
    tests, which this sprint must not regress.
"""

from __future__ import annotations

import datetime
import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

import hivepilot.config as config_mod
import hivepilot.orchestrator  # noqa: F401 — side-effect import for patch resolution
from hivepilot.models import PipelineConfig, PipelineStage
from hivepilot.services import config_provenance

MARKER = "LESSONS-S1-SECRET-MARKER-7e2f4a91-DO-NOT-LEAK"

REPO_ROOT = Path(__file__).parent.parent
MEM0_PLUGIN_PATH = REPO_ROOT / "plugins" / "mem0.py"
OBSIDIAN_PLUGIN_PATH = REPO_ROOT / "plugins" / "obsidian.py"
_HIVEPILOT_SUBTREE = "12 - HivePilot"


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


# ---------------------------------------------------------------------------
# (A) Orchestrator `after_step` choke point
# ---------------------------------------------------------------------------


class _Recorder:
    """Records every call's kwargs — stands in for a plugin-contributed hook
    (mirrors tests/test_plugin_hooks_lifecycle.py's `_Recorder`)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _bare_plugin_manager():
    from hivepilot.plugins import PluginManager

    pm = PluginManager.__new__(PluginManager)
    pm.loaded = []
    pm.hooks = {"before_step": [], "after_step": []}
    pm.declared_notifiers = {}
    pm.plugins = []
    return pm


def _make_pipeline(*stage_names: str) -> PipelineConfig:
    stages = [PipelineStage(name=n, task=n) for n in stage_names]
    return PipelineConfig(description="test pipeline", stages=stages)


def _make_orchestrator_with_pipeline(pipeline: PipelineConfig, plugin_manager=None):
    from hivepilot.models import PipelinesFile
    from hivepilot.orchestrator import Orchestrator

    pipelines_file = PipelinesFile(pipelines={"test-pipe": pipeline})
    pm = plugin_manager if plugin_manager is not None else _bare_plugin_manager()

    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch("hivepilot.orchestrator.load_pipelines", return_value=pipelines_file),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=pm),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
    ):
        orch = Orchestrator()

    return orch


def _resolve_secrets_stub(step, project=None, policy=None):
    """Mirrors the real `_resolve_secrets` contract (see
    tests/test_debate_judge.py::TestJudgeSecretMasking): a resolved secret
    value is registered globally for masking before being handed to the
    runner."""
    config_provenance.register_secret_value(MARKER)
    return {"API_KEY": MARKER}


class TestAfterStepChokeRedactsOutput:
    def test_output_containing_a_resolved_secret_is_redacted_before_the_hook_fires(
        self,
    ) -> None:
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        before_recorder = _Recorder()
        after_recorder = _Recorder()
        pm = _bare_plugin_manager()
        pm.hooks["before_step"] = [before_recorder]
        pm.hooks["after_step"] = [after_recorder]

        orch = _make_orchestrator_with_pipeline(_make_pipeline("x"), plugin_manager=pm)
        orch.registry = MagicMock()
        orch.registry.capture_definition.return_value = f"result leaked secret: {MARKER} done"

        task = TaskConfig(
            description="t",
            # A role-driven step routes through `self.registry.capture_definition`
            # (see `elif task.role:` in `_execute_task_body`), which our
            # `MagicMock().capture_definition.return_value` above configures.
            # "reviewer" mirrors tests/test_plugin_hooks_lifecycle.py's
            # `TestBeforeAfterStepHooksCarryEnrichedContext` — a real single-
            # model role, so no dual-model debate early-return is triggered.
            role="reviewer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=Path("/tmp/p"))

        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", _resolve_secrets_stub),
        ):
            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                dry_run=False,
                simulate=False,
            )

        assert len(after_recorder.calls) == 1
        redacted_output = after_recorder.calls[0]["output"]
        assert MARKER not in redacted_output
        assert config_provenance.REDACTED in redacted_output

    def test_extra_prompt_metadata_is_redacted_without_corrupting_the_shared_dict(
        self,
    ) -> None:
        """`payload.metadata` (`extra_prompt`) reaching the hook must be
        clean, but the SAME dict object driving the real prompt for later
        steps must be untouched (see the choke point's in-place-mutation
        hazard documented in orchestrator.py)."""
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        before_recorder = _Recorder()
        after_recorder = _Recorder()
        pm = _bare_plugin_manager()
        pm.hooks["before_step"] = [before_recorder]
        pm.hooks["after_step"] = [after_recorder]

        orch = _make_orchestrator_with_pipeline(_make_pipeline("x"), plugin_manager=pm)
        orch.registry = MagicMock()
        orch.registry.capture_definition.return_value = "ordinary output"

        task = TaskConfig(
            description="t",
            role="reviewer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=Path("/tmp/p"))

        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", _resolve_secrets_stub),
        ):
            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=f"use credential {MARKER} to proceed",
                auto_git=False,
                run_id=1,
                dry_run=False,
                simulate=False,
            )

        assert len(before_recorder.calls) == 1
        assert len(after_recorder.calls) == 1

        # The hook-facing copy is clean.
        after_metadata = after_recorder.calls[0]["payload"].metadata
        assert MARKER not in after_metadata["extra_prompt"]
        assert config_provenance.REDACTED in after_metadata["extra_prompt"]

        # The live dict driving the real prompt (what `before_step` saw, and
        # what any LATER step in this task would still read) was never
        # mutated in place — same raw value, different object identity from
        # the hook-facing copy.
        before_metadata = before_recorder.calls[0]["payload"].metadata
        assert before_metadata["extra_prompt"] == f"use credential {MARKER} to proceed"
        assert after_recorder.calls[0]["payload"].metadata is not before_metadata

    def test_after_step_hook_payload_secrets_is_empty_while_live_payload_keeps_them(
        self,
    ) -> None:
        """`payload.secrets` is the RAW `{ENV_NAME: resolved_value}` map — not
        text, so `redact_text`/`redact_value` can't mask it. The hook-facing
        copy must blank it entirely (`secrets={}`), while the REAL payload
        the runner actually dispatches with (captured here via
        `capture_definition`'s call args) must keep its real secrets — the
        runner needs them to build the subprocess environment."""
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        after_recorder = _Recorder()
        pm = _bare_plugin_manager()
        pm.hooks["after_step"] = [after_recorder]

        orch = _make_orchestrator_with_pipeline(_make_pipeline("x"), plugin_manager=pm)
        orch.registry = MagicMock()

        live_payloads: list = []

        def _capture(_runner_def, _payload):
            live_payloads.append(_payload)
            return "ordinary output"

        orch.registry.capture_definition.side_effect = _capture

        task = TaskConfig(
            description="t",
            role="reviewer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=Path("/tmp/p"))

        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", _resolve_secrets_stub),
        ):
            orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                dry_run=False,
                simulate=False,
            )

        assert len(after_recorder.calls) == 1
        assert len(live_payloads) == 1

        hook_payload = after_recorder.calls[0]["payload"]
        live_payload = live_payloads[0]

        assert hook_payload.secrets == {}
        assert live_payload.secrets == {"API_KEY": MARKER}
        assert hook_payload is not live_payload


# ---------------------------------------------------------------------------
# (B) Plugin defense-in-depth — mem0
# ---------------------------------------------------------------------------


def _load_mem0_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hivepilot_plugin_mem0_test", MEM0_PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMem0StoreDefenseInDepth:
    def test_store_redacts_a_resolved_secret_in_output_before_client_add(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.models import ProjectConfig, TaskStep
        from hivepilot.runners.base import RunnerPayload

        monkeypatch.setattr(config_mod.settings, "mem0_enabled", True, raising=False)
        config_provenance.register_secret_value(MARKER)

        mem0_module = _load_mem0_module()
        mock_client = MagicMock()
        payload = RunnerPayload(
            project_name="proj",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="s", runner="claude"),
            metadata={},
            secrets={},
        )

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload, output=f"the real result was {MARKER}")

        assert mock_client.add.called
        stored_text = mock_client.add.call_args.args[0]
        assert MARKER not in stored_text
        assert config_provenance.REDACTED in stored_text

    def test_store_redacts_a_resolved_secret_in_extra_prompt_before_client_add(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.models import ProjectConfig, TaskStep
        from hivepilot.runners.base import RunnerPayload

        monkeypatch.setattr(config_mod.settings, "mem0_enabled", True, raising=False)
        config_provenance.register_secret_value(MARKER)

        mem0_module = _load_mem0_module()
        mock_client = MagicMock()
        payload = RunnerPayload(
            project_name="proj",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="s", runner="claude"),
            metadata={"extra_prompt": f"use {MARKER} to authenticate"},
            secrets={},
        )

        with patch.object(mem0_module, "_get_client", return_value=mock_client):
            mem0_module.store(payload=payload)

        assert mock_client.add.called
        stored_text = mock_client.add.call_args.args[0]
        assert MARKER not in stored_text
        assert config_provenance.REDACTED in stored_text


# ---------------------------------------------------------------------------
# (B) Plugin defense-in-depth — obsidian
# ---------------------------------------------------------------------------


def _load_obsidian_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_obsidian_test", OBSIDIAN_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "Vault"
    (vault / _HIVEPILOT_SUBTREE / "Runs").mkdir(parents=True)
    return vault


def _today_journal(vault: Path) -> Path:
    today = datetime.date.today().isoformat()
    return vault / _HIVEPILOT_SUBTREE / "Runs" / f"{today}.md"


class TestObsidianStoreDefenseInDepth:
    def test_store_redacts_a_resolved_secret_in_output_before_writing_the_vault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.models import ProjectConfig, TaskStep
        from hivepilot.runners.base import RunnerPayload

        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)
        config_provenance.register_secret_value(MARKER)

        obsidian_module = _load_obsidian_module()
        payload = RunnerPayload(
            project_name="proj",
            project=ProjectConfig(path=tmp_path),
            task_name="deploy-api",
            step=TaskStep(name="build", runner="claude"),
            metadata={},
            secrets={},
        )

        obsidian_module.store(
            payload=payload, role="developer", output=f"leaked token {MARKER} in build log"
        )

        content = _today_journal(vault).read_text(encoding="utf-8")
        assert MARKER not in content
        assert config_provenance.REDACTED in content
