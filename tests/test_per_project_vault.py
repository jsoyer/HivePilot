"""
End-to-end behaviour of the per-project Obsidian vault (per-project-vault PRD).

`tests/test_obsidian_vault_resolver.py` covers the resolver's contract in
isolation. THIS file proves the wiring: that the artifacts a project produces
actually land in that project's vault, that a project without an override is
completely unaffected, that two projects with different vaults never
cross-write, and that the `obsidian` plugin READS from the same vault it
WRITES to (a split-brain recall/store would be the natural failure mode of
this change).

The operator's request, verbatim: "il faut que le travail de hivepilot aille
dans mon vault personel et le travail du pipeline Noxys dans le vault projet".
"""

from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import conftest
import pytest
import yaml

import hivepilot.config as config_mod
from hivepilot.models import PipelineConfig, PipelineStage, ProjectConfig, TaskStep
from hivepilot.pipelines import write_stage_artifact
from hivepilot.runners.base import RunnerPayload
from hivepilot.services.obsidian_vault_resolver import (
    VaultResolutionError,
    resolve_prompt_vault,
    resolve_vault_path,
)

REPO_ROOT = Path(__file__).parent.parent
OBSIDIAN_PLUGIN_PATH = REPO_ROOT / "plugins" / "obsidian.py"
_HIVEPILOT_SUBTREE = conftest.TEST_VAULT_HIVEPILOT_FOLDER
_ARTIFACTS = conftest.TEST_VAULT_ARTIFACTS_FOLDER


def _load_obsidian_module() -> ModuleType:
    """Load plugins/obsidian.py by file path — the same mechanism
    `hivepilot.plugins._scan_local_plugins` uses."""
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_obsidian_per_project_test", OBSIDIAN_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vault(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project(tmp_path: Path, name: str, vault: Path | None = None) -> ProjectConfig:
    repo = tmp_path / f"repo-{name}"
    repo.mkdir(parents=True, exist_ok=True)
    if vault is None:
        return ProjectConfig(path=repo)
    return ProjectConfig(path=repo, obsidian_vault=Path(str(vault)))


def _md_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md"))


# ---------------------------------------------------------------------------
# Stage artifacts land in the project's own vault
# ---------------------------------------------------------------------------


class TestStageArtifactsHonourTheProjectVault:
    def test_project_with_override_writes_into_its_own_vault(self, tmp_path: Path) -> None:
        personal = _vault(tmp_path, "personal")
        project = _project(tmp_path, "hivepilot", personal)

        write_stage_artifact(
            vault_path=resolve_vault_path(project),
            run_id=7,
            stage_name="CTO Review",
            output="the spec",
            dry_run=False,
            role="cto",
        )

        written = _md_files(personal)
        assert written, "expected the stage artifact in the project's own vault"
        assert any(_HIVEPILOT_SUBTREE in str(p) for p in written)
        assert any(_ARTIFACTS in str(p) for p in written)

    def test_project_without_override_writes_into_the_global_vault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REGRESSION GUARD — unchanged behaviour for every existing deployment."""
        global_vault = _vault(tmp_path, "global")
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", global_vault, raising=False)
        project = _project(tmp_path, "legacy")

        write_stage_artifact(
            vault_path=resolve_vault_path(project),
            run_id=7,
            stage_name="CTO Review",
            output="the spec",
            dry_run=False,
            role="cto",
        )

        assert _md_files(global_vault), "expected the artifact in the global vault"

    def test_two_projects_with_different_vaults_never_cross_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The operator's actual ask: HivePilot's own work in the personal
        vault, the product pipeline's work in the project vault."""
        global_vault = _vault(tmp_path, "global")
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", global_vault, raising=False)
        personal = _vault(tmp_path, "personal")
        product = _vault(tmp_path, "product")

        hivepilot_project = _project(tmp_path, "hivepilot", personal)
        noxys_project = _project(tmp_path, "noxys", product)

        write_stage_artifact(
            vault_path=resolve_vault_path(hivepilot_project),
            run_id=1,
            stage_name="engine work",
            output="engine output",
            dry_run=False,
            role="cto",
        )
        write_stage_artifact(
            vault_path=resolve_vault_path(noxys_project),
            run_id=2,
            stage_name="product work",
            output="product output",
            dry_run=False,
            role="cto",
        )

        personal_text = "\n".join(p.read_text() for p in _md_files(personal))
        product_text = "\n".join(p.read_text() for p in _md_files(product))

        assert "engine output" in personal_text
        assert "product output" not in personal_text
        assert "product output" in product_text
        assert "engine output" not in product_text
        assert _md_files(global_vault) == [], "the global vault must stay untouched"

    def test_unresolvable_override_fails_loudly_and_creates_nothing(self, tmp_path: Path) -> None:
        missing = tmp_path / "not-created"
        project = _project(tmp_path, "typo", None)
        # Bypass the load-time validator to exercise the resolve-time guard.
        object.__setattr__(project, "obsidian_vault", missing)

        with pytest.raises(VaultResolutionError):
            resolve_vault_path(project)
        assert not missing.exists()


# ---------------------------------------------------------------------------
# Orchestrator: the run resolves the destination up front, per project
# ---------------------------------------------------------------------------


def _orchestrator_with(pipeline: PipelineConfig, projects: dict[str, ProjectConfig]):
    from hivepilot.models import PipelinesFile
    from hivepilot.orchestrator import Orchestrator

    pipelines_file = PipelinesFile(pipelines={"test-pipe": pipeline})
    with (
        patch(
            "hivepilot.orchestrator.load_projects",
            return_value=MagicMock(projects=projects),
        ),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch("hivepilot.orchestrator.load_pipelines", return_value=pipelines_file),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
    ):
        return Orchestrator()


def _run(orch, global_vault: Path, project_names: list[str]) -> MagicMock:
    """Run the single-stage pipeline, returning the patched
    `write_stage_artifact` mock so the caller can assert its `vault_path`."""
    from hivepilot.orchestrator import RunResult

    with (
        patch("hivepilot.orchestrator.state_service.record_run_start", return_value=99),
        patch("hivepilot.orchestrator.state_service.complete_run"),
        patch("hivepilot.orchestrator.state_service.record_step"),
        patch("hivepilot.orchestrator.write_stage_artifact", return_value=None) as mock_write,
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
        patch("hivepilot.orchestrator.InteractionService", return_value=MagicMock()),
        patch("hivepilot.orchestrator.settings") as mock_settings,
        patch.object(
            orch,
            "run_task",
            side_effect=lambda **kwargs: [RunResult("p", kwargs["task_name"], True, "out")],
        ),
    ):
        mock_settings.obsidian_vault = global_vault
        mock_settings.enable_challenge_rounds = False
        mock_settings.enable_agent_requests = False
        mock_settings.max_requests_per_run = 20
        mock_settings.prior_context_mode = "cap"
        mock_settings.max_prior_context_chars = 8000
        mock_settings.auditor_auto = False
        mock_settings.auto_commit_vault = False
        mock_settings.event_webhook_url = None
        orch.run_pipeline(
            project_names=project_names,
            pipeline_name="test-pipe",
            extra_prompt=None,
            auto_git=False,
            dry_run=True,
        )
    return mock_write


class TestOrchestratorResolvesPerProject:
    def test_run_writes_stage_artifacts_to_the_projects_override(self, tmp_path: Path) -> None:
        global_vault = _vault(tmp_path, "global")
        personal = _vault(tmp_path, "personal")
        pipeline = PipelineConfig(description="p", stages=[PipelineStage(name="s", task="t")])
        orch = _orchestrator_with(pipeline, {"hp": _project(tmp_path, "hp", personal)})

        mock_write = _run(orch, global_vault, ["hp"])

        assert mock_write.call_args is not None
        assert mock_write.call_args.kwargs["vault_path"] == personal.resolve()

    def test_run_without_override_still_uses_the_global_vault(self, tmp_path: Path) -> None:
        """REGRESSION GUARD for every existing deployment."""
        global_vault = _vault(tmp_path, "global")
        pipeline = PipelineConfig(description="p", stages=[PipelineStage(name="s", task="t")])
        orch = _orchestrator_with(pipeline, {"legacy": _project(tmp_path, "legacy")})

        mock_write = _run(orch, global_vault, ["legacy"])

        assert mock_write.call_args.kwargs["vault_path"] == global_vault

    def test_run_over_projects_with_divergent_vaults_fails_before_any_stage(
        self, tmp_path: Path
    ) -> None:
        """Fail closed: a run writes ONE aggregated artifact per stage, so it
        must refuse rather than silently file one project's work in the
        other's vault."""
        global_vault = _vault(tmp_path, "global")
        pipeline = PipelineConfig(description="p", stages=[PipelineStage(name="s", task="t")])
        orch = _orchestrator_with(
            pipeline,
            {
                "hp": _project(tmp_path, "hp", _vault(tmp_path, "personal")),
                "noxys": _project(tmp_path, "noxys", _vault(tmp_path, "product")),
            },
        )

        with pytest.raises(VaultResolutionError):
            _run(orch, global_vault, ["hp", "noxys"])


# ---------------------------------------------------------------------------
# obsidian plugin: recall READS the same vault store WRITES
# ---------------------------------------------------------------------------


def _payload(project: ProjectConfig, task: str = "ship-detection") -> RunnerPayload:
    return RunnerPayload(
        project_name=project.path.name,
        project=project,
        task_name=task,
        step=TaskStep(name="build", runner="claude"),
        metadata={},
    )


class TestPluginDirectionsAgree:
    def test_store_writes_into_the_projects_vault_not_the_global_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _load_obsidian_module()
        global_vault = _vault(tmp_path, "global")
        personal = _vault(tmp_path, "personal")
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", global_vault, raising=False)
        monkeypatch.setattr(config_mod.settings, "obsidian_enabled", True, raising=False)

        module.store(payload=_payload(_project(tmp_path, "hp", personal)), output="done")

        assert _md_files(personal), "step outcome must land in the project's vault"
        assert _md_files(global_vault) == [], "global vault must stay untouched"

    def test_recall_reads_the_projects_vault_not_the_global_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The read direction must follow the write direction — otherwise a
        project recalls context from a vault it never writes to."""
        module = _load_obsidian_module()
        global_vault = _vault(tmp_path, "global")
        personal = _vault(tmp_path, "personal")
        (personal / "ship-detection.md").write_text("project-vault-note about ship-detection")
        (global_vault / "ship-detection.md").write_text("global-vault-note about ship-detection")
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", global_vault, raising=False)
        monkeypatch.setattr(config_mod.settings, "obsidian_enabled", True, raising=False)
        monkeypatch.setattr(config_mod.settings, "obsidian_recall_enabled", True, raising=False)
        monkeypatch.setattr(config_mod.settings, "obsidian_recall_max_bytes", 4000, raising=False)

        payload = _payload(_project(tmp_path, "hp", personal))
        module.recall(payload=payload, role="cto")

        injected = payload.metadata.get("extra_prompt") or ""
        assert "ship-detection.md" in injected
        assert "global-vault-note" not in injected

    def test_store_then_recall_round_trip_in_the_same_project_vault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _load_obsidian_module()
        personal = _vault(tmp_path, "personal")
        monkeypatch.setattr(
            config_mod.settings, "obsidian_vault", tmp_path / "absent-global", raising=False
        )
        monkeypatch.setattr(config_mod.settings, "obsidian_enabled", True, raising=False)
        monkeypatch.setattr(config_mod.settings, "obsidian_recall_enabled", True, raising=False)
        monkeypatch.setattr(config_mod.settings, "obsidian_recall_max_bytes", 4000, raising=False)

        project = _project(tmp_path, "hp", personal)
        module.store(payload=_payload(project), output="detector shipped")

        today = datetime.date.today().isoformat()
        journal = personal / _HIVEPILOT_SUBTREE / "Runs" / f"{today}.md"
        assert journal.exists(), "store must have written into the project's own vault"

        recall_payload = _payload(project, task=today)
        module.recall(payload=recall_payload, role="cto")
        assert today in (recall_payload.metadata.get("extra_prompt") or "")

    def test_hooks_are_silent_no_ops_on_an_unusable_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lifecycle hook must never crash a run — the loud failure for the
        same misconfiguration is raised up front by the orchestrator."""
        module = _load_obsidian_module()
        monkeypatch.setattr(config_mod.settings, "obsidian_enabled", True, raising=False)
        project = _project(tmp_path, "hp")
        object.__setattr__(project, "obsidian_vault", tmp_path / "never-created")

        module.store(payload=_payload(project), output="x")  # must not raise

        assert not (tmp_path / "never-created").exists()


# ---------------------------------------------------------------------------
# {OBSIDIAN_VAULT} prompt variable
# ---------------------------------------------------------------------------


class TestPromptVariableFollowsTheProject:
    def test_prompt_var_uses_the_project_override(self, tmp_path: Path) -> None:
        personal = _vault(tmp_path, "personal")
        settings_obj = MagicMock()
        settings_obj.obsidian_vault = _vault(tmp_path, "global")

        value = resolve_prompt_vault(settings_obj, _project(tmp_path, "hp", personal))

        assert value == str(personal.resolve())

    def test_prompt_var_falls_back_to_the_global_setting(self, tmp_path: Path) -> None:
        global_vault = _vault(tmp_path, "global")
        settings_obj = MagicMock()
        settings_obj.obsidian_vault = global_vault

        value = resolve_prompt_vault(settings_obj, _project(tmp_path, "legacy"))

        assert value == str(global_vault)

    def test_prompt_var_is_empty_rather_than_wrong_on_a_broken_override(
        self, tmp_path: Path
    ) -> None:
        settings_obj = MagicMock()
        settings_obj.obsidian_vault = _vault(tmp_path, "global")
        project = _project(tmp_path, "hp")
        object.__setattr__(project, "obsidian_vault", tmp_path / "never-created")

        assert resolve_prompt_vault(settings_obj, project) == ""


# ---------------------------------------------------------------------------
# `config doctor` reflects the new reality
# ---------------------------------------------------------------------------


class TestDoctorFindingReflectsPerProjectVault:
    def _write_projects(self, tmp_path: Path, projects: dict) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": projects}))

    def test_finding_now_names_the_per_project_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It used to say "no config fix today". There IS one now."""
        from hivepilot.services import config_doctor

        monkeypatch.setattr(config_mod.settings, "obsidian_enabled", True, raising=False)
        self._write_projects(
            tmp_path, {"a": {"path": str(tmp_path / "a")}, "b": {"path": str(tmp_path / "b")}}
        )

        findings = config_doctor.check_shared_obsidian_vault(tmp_path)
        shared = [f for f in findings if f.check == "shared_obsidian_vault"]

        assert shared, f"expected the informational finding, got {findings}"
        assert "obsidian_vault" in shared[0].fix
        assert "no config fix today" not in shared[0].fix

    def test_finding_stops_firing_once_an_override_is_in_use(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A doctor that keeps reporting a solved limitation is noise."""
        from hivepilot.services import config_doctor

        monkeypatch.setattr(config_mod.settings, "obsidian_enabled", True, raising=False)
        self._write_projects(
            tmp_path,
            {
                "a": {"path": str(tmp_path / "a"), "obsidian_vault": str(tmp_path / "va")},
                "b": {"path": str(tmp_path / "b")},
            },
        )

        findings = config_doctor.check_shared_obsidian_vault(tmp_path)

        assert [f for f in findings if f.check == "shared_obsidian_vault"] == []

    def test_empty_override_is_reported_as_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.services import config_doctor

        monkeypatch.setattr(config_mod.settings, "obsidian_enabled", True, raising=False)
        self._write_projects(
            tmp_path,
            {"a": {"path": str(tmp_path / "a"), "obsidian_vault": ""}},
        )

        findings = config_doctor.check_shared_obsidian_vault(tmp_path)
        invalid = [f for f in findings if f.check == "project_vault_override_invalid"]

        assert invalid and invalid[0].severity == "error"
        assert "EMPTY" in invalid[0].message

    def test_relative_override_is_reported_as_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.services import config_doctor

        monkeypatch.setattr(config_mod.settings, "obsidian_enabled", True, raising=False)
        self._write_projects(
            tmp_path,
            {"a": {"path": str(tmp_path / "a"), "obsidian_vault": "obsidian-vault"}},
        )

        findings = config_doctor.check_shared_obsidian_vault(tmp_path)
        invalid = [f for f in findings if f.check == "project_vault_override_invalid"]

        assert invalid and invalid[0].severity == "error"
        assert "RELATIVE" in invalid[0].message

    def test_valid_override_yields_no_invalid_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.services import config_doctor

        monkeypatch.setattr(config_mod.settings, "obsidian_enabled", True, raising=False)
        self._write_projects(
            tmp_path,
            {"a": {"path": str(tmp_path / "a"), "obsidian_vault": str(tmp_path / "vault")}},
        )

        findings = config_doctor.check_shared_obsidian_vault(tmp_path)

        assert [f for f in findings if f.check == "project_vault_override_invalid"] == []
