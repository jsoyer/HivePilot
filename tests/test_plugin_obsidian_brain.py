"""
Tests for the Obsidian "brain" feature (Sprint 02 of the plugin-arch-overhaul
PRD): turns `plugins/obsidian.py` from a write-only notifier into a context
provider.

- `before_step` -> `recall`: simple ranked grep over the vault's `.md` notes
  for content relevant to the current task/role/step, appended into
  `RunnerPayload.metadata["extra_prompt"]` — mirrors `plugins/mem0.py`'s
  `recall`/`store` injection contract (same field, same append-not-overwrite
  discipline), but the Obsidian vault is the context source instead of a
  mem0 memory store.
- `after_step` -> `store`: appends a structured step-outcome entry to the
  SAME daily journal note `notify`/`on_pipeline_end`/`on_error` already
  write to (`ObsidianService.append_daily`, `hivepilot/services/
  obsidian_service.py`) — reuses the existing tested safe-append path
  rather than inventing a new note-path scheme.

Covers, per the sprint spec:
(a) `recall` appends bounded context to `extra_prompt`, respecting
    `obsidian_recall_max_bytes`.
(b) `recall`/`store` no-op when disabled (`obsidian_enabled` or
    `obsidian_recall_enabled` False) or the vault is absent.
(c) With both mem0-style pre-existing `extra_prompt` content AND obsidian
    recall enabled, `extra_prompt` contains BOTH (append, not overwrite).
(d) `recall` never emits `${secret:...}` content into the prompt even when a
    matched note contains one.
(e) `store` appends a run entry without truncating prior journal content.
(f) existing notifier/journal tests are untouched (see test_plugin_obsidian.py).
(g) `register()` returns `before_step`/`after_step` alongside the existing
    notifier/hook/health keys.
"""

from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path
from types import ModuleType

import conftest
import pytest

import hivepilot.config as config_mod
from hivepilot.models import ProjectConfig, TaskStep
from hivepilot.runners.base import RunnerPayload

REPO_ROOT = Path(__file__).parent.parent
OBSIDIAN_PLUGIN_PATH = REPO_ROOT / "plugins" / "obsidian.py"
_HIVEPILOT_SUBTREE = conftest.TEST_VAULT_HIVEPILOT_FOLDER


def _load_obsidian_module() -> ModuleType:
    """Load plugins/obsidian.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses."""
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_obsidian_brain_test", OBSIDIAN_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def obsidian_module() -> ModuleType:
    return _load_obsidian_module()


@pytest.fixture(autouse=True)
def _obsidian_recall_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`obsidian_enabled`/`obsidian_recall_enabled` both default True (opt-out).
    Set explicitly here so tests are independent of the real default value."""
    monkeypatch.setattr(config_mod.settings, "obsidian_enabled", True, raising=False)
    monkeypatch.setattr(config_mod.settings, "obsidian_recall_enabled", True, raising=False)
    monkeypatch.setattr(config_mod.settings, "obsidian_recall_max_bytes", 4000, raising=False)


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "Vault"
    (vault / _HIVEPILOT_SUBTREE / "Runs").mkdir(parents=True)
    return vault


def _today_journal(vault: Path) -> Path:
    today = datetime.date.today().isoformat()
    return vault / _HIVEPILOT_SUBTREE / "Runs" / f"{today}.md"


def _payload(tmp_path: Path, task_name: str = "deploy-api", **metadata: object) -> RunnerPayload:
    return RunnerPayload(
        project_name="proj",
        project=ProjectConfig(path=tmp_path),
        task_name=task_name,
        step=TaskStep(name="build", runner="claude"),
        metadata=dict(metadata),
        secrets={},
    )


class TestRegisterExposesRecallStore:
    def test_register_returns_before_and_after_step_alongside_existing_keys(
        self, obsidian_module: ModuleType
    ) -> None:
        hooks = obsidian_module.register()
        assert hooks["before_step"] is obsidian_module.recall
        assert hooks["after_step"] is obsidian_module.store
        # existing keys unchanged
        assert "obsidian" in hooks["notifiers"]
        assert hooks["on_pipeline_end"] is obsidian_module.on_pipeline_end
        assert hooks["on_error"] is obsidian_module.on_error
        assert "obsidian" in hooks["health"]


class TestRecallInjectsVaultContext:
    def test_recall_appends_bounded_context_to_extra_prompt(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        note = vault / _HIVEPILOT_SUBTREE / "Runs" / "deploy-api-notes.md"
        note.write_text("deploy-api uses blue-green rollout via terraform.\n", encoding="utf-8")
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        payload = _payload(tmp_path)
        obsidian_module.recall(payload=payload, role="developer")

        extra = payload.metadata.get("extra_prompt")
        assert extra is not None
        assert "blue-green rollout" in extra

    def test_recall_respects_max_bytes_cap(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        note = vault / _HIVEPILOT_SUBTREE / "Runs" / "deploy-api-notes.md"
        note.write_text("deploy-api " + ("x" * 5000) + "\n", encoding="utf-8")
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)
        monkeypatch.setattr(config_mod.settings, "obsidian_recall_max_bytes", 100, raising=False)

        payload = _payload(tmp_path)
        obsidian_module.recall(payload=payload, role="developer")

        extra = payload.metadata.get("extra_prompt")
        assert extra is not None
        assert len(extra.encode("utf-8")) <= 100

    def test_recall_appends_not_overwrites_existing_extra_prompt(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        note = vault / _HIVEPILOT_SUBTREE / "Runs" / "deploy-api-notes.md"
        note.write_text("deploy-api uses blue-green rollout.\n", encoding="utf-8")
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        payload = _payload(tmp_path, extra_prompt="Relevant memories:\n- from mem0")
        obsidian_module.recall(payload=payload, role="developer")

        extra = payload.metadata.get("extra_prompt")
        assert extra is not None
        assert "from mem0" in extra
        assert "blue-green rollout" in extra

    def test_recall_noop_when_obsidian_disabled(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        note = vault / _HIVEPILOT_SUBTREE / "Runs" / "deploy-api-notes.md"
        note.write_text("deploy-api uses blue-green rollout.\n", encoding="utf-8")
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)
        monkeypatch.setattr(config_mod.settings, "obsidian_enabled", False, raising=False)

        payload = _payload(tmp_path)
        obsidian_module.recall(payload=payload, role="developer")

        assert payload.metadata.get("extra_prompt") is None

    def test_recall_noop_when_recall_disabled(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        note = vault / _HIVEPILOT_SUBTREE / "Runs" / "deploy-api-notes.md"
        note.write_text("deploy-api uses blue-green rollout.\n", encoding="utf-8")
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)
        monkeypatch.setattr(config_mod.settings, "obsidian_recall_enabled", False, raising=False)

        payload = _payload(tmp_path)
        obsidian_module.recall(payload=payload, role="developer")

        assert payload.metadata.get("extra_prompt") is None

    def test_recall_noop_when_vault_absent(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", missing, raising=False)

        payload = _payload(tmp_path)
        obsidian_module.recall(payload=payload, role="developer")

        assert payload.metadata.get("extra_prompt") is None

    def test_recall_never_forwards_secret_ref_tokens(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        note = vault / _HIVEPILOT_SUBTREE / "Runs" / "deploy-api-notes.md"
        note.write_text(
            "deploy-api credential is ${secret:DEPLOY_TOKEN} — never share it.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        payload = _payload(tmp_path)
        obsidian_module.recall(payload=payload, role="developer")

        extra = payload.metadata.get("extra_prompt") or ""
        assert "${secret:" not in extra

    def test_recall_does_not_requery_for_second_step_sharing_metadata(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Idempotency guard mirroring mem0's `_mem0_recalled` sentinel — a
        multi-step task sharing one `metadata` dict must not re-append vault
        context on every step."""
        vault = _make_vault(tmp_path)
        note = vault / _HIVEPILOT_SUBTREE / "Runs" / "deploy-api-notes.md"
        note.write_text("deploy-api uses blue-green rollout.\n", encoding="utf-8")
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        payload = _payload(tmp_path)
        obsidian_module.recall(payload=payload, role="developer")
        first = payload.metadata.get("extra_prompt")
        obsidian_module.recall(payload=payload, role="developer")
        second = payload.metadata.get("extra_prompt")

        assert first == second


class TestStoreAppendsRunEntry:
    def test_store_appends_step_outcome_to_daily_journal(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        payload = _payload(tmp_path)
        obsidian_module.store(payload=payload, role="developer", output="build succeeded")

        content = _today_journal(vault).read_text(encoding="utf-8")
        assert "deploy-api" in content
        assert "developer" in content
        assert "build" in content

    def test_store_does_not_truncate_prior_journal_content(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        obsidian_module.notify("pre-existing journal line")
        payload = _payload(tmp_path)
        obsidian_module.store(payload=payload, role="developer", output="build succeeded")

        content = _today_journal(vault).read_text(encoding="utf-8")
        assert "pre-existing journal line" in content
        assert "deploy-api" in content

    def test_store_noop_when_obsidian_disabled(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)
        monkeypatch.setattr(config_mod.settings, "obsidian_enabled", False, raising=False)

        payload = _payload(tmp_path)
        obsidian_module.store(payload=payload, role="developer", output="build succeeded")

        assert not _today_journal(vault).exists()

    def test_store_noop_when_vault_absent(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", missing, raising=False)

        payload = _payload(tmp_path)
        # Must not raise.
        obsidian_module.store(payload=payload, role="developer", output="build succeeded")

    def test_store_honors_dry_run_does_not_write_vault(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        payload = _payload(tmp_path)
        obsidian_module.store(
            payload=payload, role="developer", output="build succeeded", dry_run=True
        )

        assert not _today_journal(vault).exists()

    def test_store_swallows_internal_error(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        from unittest.mock import MagicMock, patch

        with (
            patch.object(
                obsidian_module.ObsidianService,
                "append_daily",
                side_effect=RuntimeError("boom"),
            ),
            patch.object(obsidian_module, "logger", MagicMock()) as mock_logger,
        ):
            payload = _payload(tmp_path)
            # Must not raise — a hook must never crash a run.
            obsidian_module.store(payload=payload, role="developer", output="x")

        assert mock_logger.warning.called


# ---------------------------------------------------------------------------
# Corpus selection -- the scan cap decided WHICH notes exist, not just how many
# ---------------------------------------------------------------------------


class TestRecallReachesRecentNotes:
    """Measured on the production vault: 2035 notes against a 500 cap, sorted
    alphabetically. The 500 scanned were 24 Inbox + 476 Journal notes; the
    agent artifacts start at index 1796 and were NEVER read.

    So recall searched a corpus that structurally excluded every note the
    agents themselves had written -- and returned May journal entries for an
    August review step. A relevance heuristic cannot fix a corpus that does
    not contain the answer.
    """

    def _vault(self, tmp_path, count=40):
        import os
        import time

        vault = tmp_path / "vault"
        # Alphabetically FIRST and oldest: the decoys.
        (vault / "00 - Inbox").mkdir(parents=True)
        for i in range(count):
            note = vault / "00 - Inbox" / f"old-{i:03d}.md"
            note.write_text(f"# Old note {i}\ncto review of something ancient\n")
            os.utime(note, (1_000_000, 1_000_000))
        # Alphabetically LAST and newest: what recall should actually find.
        (vault / "13 - Artifacts" / "cto").mkdir(parents=True)
        recent = vault / "13 - Artifacts" / "cto" / "2026-08-09-run425-cto-review.md"
        recent.write_text(
            "---\nrole: cto\nstage: cto review\nrun_id: 425\n---\n"
            "The execution_plan and product_spec are empty; status BLOCKED.\n"
        )
        os.utime(recent, (time.time(), time.time()))
        return vault, recent

    def test_newest_note_survives_the_scan_cap(self, tmp_path, monkeypatch, obsidian_module):
        obsidian = obsidian_module
        vault, recent = self._vault(tmp_path)
        monkeypatch.setattr(obsidian, "_MAX_NOTES_SCANNED", 10)

        hits = obsidian._search_vault(vault, ["cto review", "cto"])

        assert any(p == recent for p, _ in hits), (
            "the newest note must be reachable; an alphabetical cap made the "
            "agents' own artifacts invisible in production"
        )

    def test_frontmatter_role_outranks_a_generic_term_match(self, tmp_path, obsidian_module):
        obsidian = obsidian_module
        vault = tmp_path / "vault"
        vault.mkdir()
        # Mentions the term far more often, but is not this role's work.
        noise = vault / "plan.md"
        noise.write_text("# Plan\n" + "cto cto cto cto cto cto cto cto\n" * 6)
        owned = vault / "review.md"
        owned.write_text("---\nrole: cto\n---\nstatus BLOCKED, spec empty\n")

        hits = obsidian._search_vault(vault, ["cto"], role="cto")

        assert hits[0][0] == owned, "a frontmatter role match is scoping, not a keyword hit"

    def test_no_frontmatter_still_works(self, tmp_path, obsidian_module):
        """Vaults without agent-written frontmatter must behave as before."""
        obsidian = obsidian_module
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "n.md").write_text("# Note\nsome cto content here\n")

        assert obsidian._search_vault(vault, ["cto"], role="cto")


class TestExcerptSkipsFrontmatter:
    """Scoping by frontmatter made the frontmatter the top keyword match.

    Once notes carried `role: cto`, the first line containing "cto" WAS that
    line -- so recall injected five copies of `role: cto` and none of the
    review. Right notes, empty payload: the fix for one layer created the
    defect in the next.
    """

    def test_excerpt_comes_from_the_body(self, tmp_path, obsidian_module):
        obsidian = obsidian_module
        note = tmp_path / "n.md"
        note.write_text(
            "---\nrole: cto\nstage: cto review\nrun_id: 425\n---\n"
            "The execution_plan and product_spec are empty; status BLOCKED.\n"
        )

        excerpt = obsidian._first_matching_excerpt(note.read_text(), ["cto"])

        assert "role:" not in excerpt
        assert "BLOCKED" in excerpt

    def test_frontmatter_only_note_yields_nothing_useful_not_a_crash(
        self, tmp_path, obsidian_module
    ):
        obsidian = obsidian_module
        excerpt = obsidian._first_matching_excerpt("---\nrole: cto\n---\n", ["cto"])

        assert "role:" not in excerpt


def test_recent_note_wins_a_score_tie(tmp_path, obsidian_module):
    """Among a role's own notes, ranking by term count favours the longest.

    Measured after the scoping fix: the CTO step got three notes from 26 July
    ahead of the run from 8 August, because the older ones were wordier. For a
    memory, "what happened last" beats "what said it most".
    """
    import os

    obsidian = obsidian_module
    vault = tmp_path / "v"
    vault.mkdir()
    old = vault / "old.md"
    old.write_text("---\nrole: cto\n---\n" + "cto review said things. " * 40)
    os.utime(old, (1_000_000, 1_000_000))
    new = vault / "new.md"
    new.write_text("---\nrole: cto\n---\ncto review: status BLOCKED\n")
    os.utime(new, (2_000_000_000, 2_000_000_000))

    hits = obsidian._search_vault(vault, ["cto review", "cto"], role="cto")

    assert hits[0][0] == new, "the newest of a role's own notes must come first"


def test_recall_is_recorded_like_mem0(tmp_path, monkeypatch, obsidian_module):
    """Until this existed, `memory_events` held mem0 rows only.

    Any comparison of the two backends therefore showed Obsidian at zero, and
    a zero meaning "never measured" is indistinguishable from one meaning
    "useless" -- which is exactly the wrong conclusion to hand an operator.
    """
    monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
    from hivepilot.services import memory_service

    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "n.md").write_text("---\nrole: cto\n---\nsomething about the review\n")

    class Payload:
        project_name = "p"
        task_name = "t"
        step = None
        metadata: dict = {}

    payload = Payload()
    payload.metadata = {}
    monkeypatch.setattr(obsidian_module, "_resolve_vault", lambda *a, **k: vault)
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "obsidian_enabled", True, raising=False)
    monkeypatch.setattr(settings, "obsidian_recall_enabled", True, raising=False)

    obsidian_module.recall(payload=payload, role="cto", step=None)

    assert memory_service.backend_stats()["obsidian"]["searches"] == 1
