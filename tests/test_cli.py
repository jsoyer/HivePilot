"""
Tests for the obsidian CLI sub-app added to hivepilot.cli.

The full CLI imports the Orchestrator which in turn imports optional heavy
dependencies (langchain, etc.).  We stub those out at the module level so
the test suite stays lightweight and doesn't require the full [full] extras.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import conftest
import pytest

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli
# ---------------------------------------------------------------------------

_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
    "langchain_openai",
    "openai",
    "boto3",
    "docker",
    "telegram",
    "telegram.ext",
    "fastapi",
    "fastapi.responses",
    "fastapi.security",
    "uvicorn",
    "textual",
    "slack_bolt",
    "slack_bolt.adapter",
    "slack_bolt.adapter.fastapi",
    "slack_bolt.adapter.socket_mode",
    "discord",
    "PyNaCl",
    "nacl",
    "nacl.exceptions",
    "nacl.signing",
]

import importlib  # noqa: E402

for _mod in _STUBS:
    if _mod in sys.modules:
        continue
    try:
        # Prefer the real module when installed so flat MagicMock stubs do not
        # shadow proper packages (e.g. fastapi) for later tests like test_pentest.
        importlib.import_module(_mod)
    except Exception:
        sys.modules[_mod] = MagicMock()

from typer.testing import CliRunner  # noqa: E402

from hivepilot.cli import app  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_vault(tmp_path: Path) -> Path:
    """Create a minimal fake Obsidian vault for CLI tests."""
    vault = tmp_path / "TestVault"
    vault.mkdir()
    # Folder names come from the pinned test layout (conftest `_pin_vault_layout`),
    # never from engine code. A deliberate SUBSET of the declared expected layout,
    # so the audit has both present and missing folders to report.
    for folder in [
        "Inbox",
        "Journal",
        conftest.TEST_VAULT_DECISIONS_FOLDER,
        conftest.TEST_VAULT_SECURITY_FOLDER,
        "Architecture",
        conftest.TEST_VAULT_HIVEPILOT_FOLDER,
        "Archive",
    ]:
        (vault / folder).mkdir()
    for sub in ["Agents", "Tasks", "Reports", "Runs", "Interactions"]:
        (vault / conftest.TEST_VAULT_HIVEPILOT_FOLDER / sub).mkdir(parents=True, exist_ok=True)
    return vault


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _write_minimal_valid_config(base_dir: Path) -> None:
    """Write the six required config files (+ a prompts/agents dir) so
    validate_config() reports zero cross-reference problems."""
    import yaml

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


class TestValidateCli:
    """`hivepilot validate` -- default (no --dir) must resolve the config
    that's actually active (XDG -> config_repo -> base_dir, matching
    `hivepilot config sync`'s real write target and every runtime loader),
    not literally `Path.cwd()`. An explicit `--dir` must keep validating
    that exact directory, unaffected by any unrelated XDG config."""

    def test_default_no_dir_resolves_config_synced_to_xdg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: after `hivepilot config sync` (writes to XDG_CONFIG_HOME),
        running bare `hivepilot validate` from an unrelated cwd must report OK,
        not false 'Missing required config file' errors."""
        xdg_dir = tmp_path / "xdg" / "hivepilot"
        xdg_dir.mkdir(parents=True)
        _write_minimal_valid_config(xdg_dir)

        empty_cwd = tmp_path / "empty-cwd"
        empty_cwd.mkdir()
        monkeypatch.chdir(empty_cwd)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        runner = CliRunner()
        result = runner.invoke(app, ["validate"])

        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    def test_explicit_dir_still_validates_that_exact_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--dir X` must keep validating X literally -- even when an
        unrelated XDG config exists -- so scaffold/pre-activation validation
        (and the documented `--dir /data` deploy flow) is unaffected."""
        xdg_dir = tmp_path / "xdg" / "hivepilot"
        xdg_dir.mkdir(parents=True)
        _write_minimal_valid_config(xdg_dir)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        explicit_dir = tmp_path / "explicit-target"
        explicit_dir.mkdir()  # deliberately empty -- no config files here

        runner = CliRunner()
        result = runner.invoke(app, ["validate", "--dir", str(explicit_dir)])

        assert result.exit_code == 1, result.output
        assert "Missing required config file" in result.output

    def test_explicit_dir_with_valid_config_still_passes(self, tmp_path: Path) -> None:
        """Regression guard: explicit --dir with a valid config must still
        report OK exactly as before this fix."""
        _write_minimal_valid_config(tmp_path)

        runner = CliRunner()
        result = runner.invoke(app, ["validate", "--dir", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    def test_header_prints_absolute_config_dir_and_plugin_dir(self, tmp_path: Path) -> None:
        """`validate` must print, at the top, the absolute config directory
        it read and the absolute plugin dir(s) it scanned -- so a cwd-
        dependent verdict is never mistaken for flakiness again."""
        _write_minimal_valid_config(tmp_path)

        runner = CliRunner()
        result = runner.invoke(app, ["validate", "--dir", str(tmp_path)])

        assert result.exit_code == 0, result.output
        resolved = str(tmp_path.resolve())
        assert resolved in result.output
        assert str((tmp_path / "plugins").resolve()) in result.output
        assert "OK" in result.output

    def test_skill_resolution_is_cwd_independent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The confirmed bug: a `skills:` reference must resolve identically
        whether the process cwd is inside or outside the directory being
        validated -- `--dir` must be threaded to plugin/skill discovery
        instead of `PluginManager` silently reading `Path.cwd()`."""
        config_dir = tmp_path / "config-repo"
        config_dir.mkdir()
        _write_minimal_valid_config(config_dir)

        plugin_dir = config_dir / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "cwd_skill.py").write_text(
            "def register():\n"
            "    return {'skills': [{'name': 'cwd_skill', 'description': 'd', "
            "'provider': 'acme', 'files': {'SKILL.md': 'hi'}}]}\n",
            encoding="utf-8",
        )

        import yaml

        (config_dir / "tasks.yaml").write_text(
            yaml.dump(
                {
                    "tasks": {
                        "task-a": {
                            "role": "planner",
                            "steps": [{"name": "s1", "runner": "claude", "skills": ["cwd_skill"]}],
                        }
                    }
                }
            )
        )

        outside_cwd = tmp_path / "somewhere-else"
        outside_cwd.mkdir()

        runner = CliRunner()

        monkeypatch.chdir(outside_cwd)
        result_outside = runner.invoke(app, ["validate", "--dir", str(config_dir)])

        monkeypatch.chdir(config_dir)
        result_inside = runner.invoke(app, ["validate", "--dir", str(config_dir)])

        assert result_outside.exit_code == 0, result_outside.output
        assert "OK" in result_outside.output
        assert result_inside.exit_code == 0, result_inside.output
        assert "OK" in result_inside.output
        assert result_outside.output == result_inside.output

    def test_unresolved_skill_error_names_searched_directories(self, tmp_path: Path) -> None:
        """An unresolved `skills:` reference must name the directories that
        were searched, not just say "unknown skill"."""
        config_dir = tmp_path / "config-repo"
        config_dir.mkdir()
        _write_minimal_valid_config(config_dir)

        import yaml

        (config_dir / "tasks.yaml").write_text(
            yaml.dump(
                {
                    "tasks": {
                        "task-a": {
                            "role": "planner",
                            "steps": [
                                {"name": "s1", "runner": "claude", "skills": ["ghost-skill"]}
                            ],
                        }
                    }
                }
            )
        )

        runner = CliRunner()
        result = runner.invoke(app, ["validate", "--dir", str(config_dir)])

        assert result.exit_code == 1, result.output
        assert "ghost-skill" in result.output
        assert str((config_dir / "plugins").resolve()) in result.output


class TestObsidianCli:
    def test_obsidian_audit_command_exists(self, fake_vault: Path) -> None:
        """hivepilot obsidian audit should exit 0 and print a report."""
        runner = CliRunner()
        result = runner.invoke(app, ["obsidian", "audit", "--vault", str(fake_vault)])
        assert result.exit_code == 0, result.output

    def test_obsidian_audit_shows_present_folders(self, fake_vault: Path) -> None:
        """Audit output mentions present folders."""
        runner = CliRunner()
        result = runner.invoke(app, ["obsidian", "audit", "--vault", str(fake_vault)])
        assert "present" in result.output.lower()
        assert conftest.TEST_VAULT_HIVEPILOT_FOLDER in result.output

    def test_obsidian_audit_shows_missing_folders(self, fake_vault: Path) -> None:
        """Audit output reports missing expected folders."""
        runner = CliRunner()
        result = runner.invoke(app, ["obsidian", "audit", "--vault", str(fake_vault)])
        assert result.exit_code == 0
        # We have a partial vault so some folders should be missing
        assert "missing" in result.output.lower()
        assert "Engineering" in result.output

    def test_obsidian_audit_reports_how_many_folders_it_examined(self, fake_vault: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["obsidian", "audit", "--vault", str(fake_vault)])
        assert "examined" in result.output.lower()

    def test_obsidian_audit_of_zero_folders_is_not_reported_as_a_pass(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """THE trap. An operator who declared no expected layout must not get an
        audit whose output reads as a clean bill of health.

        The exact fail-open shape found twice in one day elsewhere: `plugins
        audit` printing "No plugin source files found" on a host with seven, and
        `check_public_safe.py` printing "passed (0 files scanned)".
        """
        from hivepilot.services import vault_layout
        from hivepilot.services.vault_layout import SLOT_HIVEPILOT, VaultLayout

        vault = tmp_path / "Undeclared"
        vault.mkdir()
        monkeypatch.setattr(
            vault_layout,
            "VAULT_LAYOUT",
            VaultLayout(
                folders={SLOT_HIVEPILOT: "HivePilot"},
                expected_folders=(),
                frozen_folders=(),
            ),
        )

        result = CliRunner().invoke(app, ["obsidian", "audit", "--vault", str(vault)])

        assert "NOT CHECKED" in result.output
        assert "not a pass" in result.output.lower()
        # And it must not print a zero-count "present/missing" pair that reads clean.
        assert "Present folders (0)" not in result.output

    def test_obsidian_audit_strict_exits_nonzero_when_it_examined_nothing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from hivepilot.services import vault_layout
        from hivepilot.services.vault_layout import VaultLayout

        vault = tmp_path / "Undeclared"
        vault.mkdir()
        monkeypatch.setattr(
            vault_layout,
            "VAULT_LAYOUT",
            VaultLayout(folders={}, expected_folders=(), frozen_folders=()),
        )

        result = CliRunner().invoke(app, ["obsidian", "audit", "--vault", str(vault), "--strict"])
        assert result.exit_code == 1, result.output

    def test_obsidian_audit_strict_passes_on_a_fully_declared_vault(self, fake_vault: Path) -> None:
        """--strict must not fire merely because some declared folder is absent;
        it fires when the audit could not ESTABLISH anything."""
        result = CliRunner().invoke(
            app, ["obsidian", "audit", "--vault", str(fake_vault), "--strict"]
        )
        assert result.exit_code == 0, result.output

    def test_obsidian_audit_names_unconfigured_engine_slots(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The engine's own folders are reported independently of the declared
        layout, so the audit can never be blind to a write target."""
        from hivepilot.services import vault_layout
        from hivepilot.services.vault_layout import VaultLayout

        vault = tmp_path / "V"
        vault.mkdir()
        monkeypatch.setattr(
            vault_layout,
            "VAULT_LAYOUT",
            VaultLayout(folders={}, expected_folders=("Somewhere",), frozen_folders=()),
        )

        result = CliRunner().invoke(app, ["obsidian", "audit", "--vault", str(vault)])

        assert "NOT CONFIGURED" in result.output
        for slot in ("artifacts", "decisions", "security", "hivepilot"):
            assert slot in result.output


class TestCostsBackfillCli:
    """`hivepilot costs backfill` (usage-capture-modelusage fix) -- an
    explicit, opt-in one-off recompute, never an automatic migration side
    effect. Defaults to a dry run; `--apply` is required to actually write."""

    def _seed_unpriced_step(self) -> int:
        from hivepilot.services import db, state_service

        state_service.init_db()
        with db.connect() as conn:
            run_id = db.insert_returning_id(
                conn,
                "INSERT INTO runs (project, task, status, tenant) VALUES (?, ?, ?, ?)",
                ("proj", "task", "success", "default"),
            )
            return db.insert_returning_id(
                conn,
                db.ph(
                    "INSERT INTO steps (run_id, step, status, provider, model, "
                    "input_tokens, output_tokens, cost_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (run_id, "s1", "success", "claude", "claude-sonnet-4-6", 1_000_000, 500_000, None),
            )

    def test_dry_run_by_default_does_not_write(self) -> None:
        from hivepilot.services import db

        step_id = self._seed_unpriced_step()
        runner = CliRunner()
        result = runner.invoke(app, ["costs", "backfill"])

        assert result.exit_code == 0, result.output
        assert "dry" in result.output.lower() or "would" in result.output.lower()
        with db.connect() as conn:
            row = conn.execute(
                db.ph("SELECT cost_usd FROM steps WHERE id=?"), (step_id,)
            ).fetchone()
        assert row["cost_usd"] is None

    def test_apply_flag_writes_the_recomputed_cost(self) -> None:
        from hivepilot.services import db

        step_id = self._seed_unpriced_step()
        runner = CliRunner()
        result = runner.invoke(app, ["costs", "backfill", "--apply"])

        assert result.exit_code == 0, result.output
        with db.connect() as conn:
            row = conn.execute(
                db.ph("SELECT cost_usd FROM steps WHERE id=?"), (step_id,)
            ).fetchone()
        assert row["cost_usd"] == 10.5

    def test_reports_still_unpriced_count(self) -> None:
        from hivepilot.services import db, state_service

        state_service.init_db()
        with db.connect() as conn:
            run_id = db.insert_returning_id(
                conn,
                "INSERT INTO runs (project, task, status, tenant) VALUES (?, ?, ?, ?)",
                ("proj", "task", "success", "default"),
            )
            conn.execute(
                db.ph(
                    "INSERT INTO steps (run_id, step, status, provider, model, "
                    "input_tokens, output_tokens, cost_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (run_id, "s1", "success", "claude", "never-priced-model", 100, 100, None),
            )

        runner = CliRunner()
        result = runner.invoke(app, ["costs", "backfill"])

        assert result.exit_code == 0, result.output
        assert "1" in result.output

    def test_zero_candidates_says_nothing_to_backfill_when_db_is_empty(self) -> None:
        """`scanned 0` alone is ambiguous -- an operator can't tell 'the
        system is healthy' from 'this silently found nothing and I don't
        know why'. An empty database is the healthy case."""
        runner = CliRunner()
        result = runner.invoke(app, ["costs", "backfill"])

        assert result.exit_code == 0, result.output
        assert "0" in result.output
        assert "predate" not in result.output.lower()

    def test_zero_candidates_explains_unrecoverable_rows(self) -> None:
        """The operator's real box: rows with a model but NO tokens
        captured at all (pre-dating the usage-capture fix) -- `scanned 0`
        must say WHY, not just print a bare 0."""
        from hivepilot.services import db, state_service

        state_service.init_db()
        with db.connect() as conn:
            run_id = db.insert_returning_id(
                conn,
                "INSERT INTO runs (project, task, status, tenant) VALUES (?, ?, ?, ?)",
                ("proj", "task", "success", "default"),
            )
            for i in range(3):
                conn.execute(
                    db.ph(
                        "INSERT INTO steps (run_id, step, status, provider, model, "
                        "input_tokens, output_tokens, cost_usd) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (run_id, f"s{i}", "success", "claude", "opus", None, None, None),
                )

        runner = CliRunner()
        result = runner.invoke(app, ["costs", "backfill"])

        assert result.exit_code == 0, result.output
        assert "3" in result.output
        assert "predate" in result.output.lower() or "no tokens" in result.output.lower()


# ---------------------------------------------------------------------------
# schedule health / schedule list -- source: autopilot entries (task=None)
# ---------------------------------------------------------------------------


class TestScheduleHealthCommand:
    """`hivepilot schedule health` must not crash on `source: autopilot`
    entries, whose `task` is None (mutually exclusive with `source`).
    Before the fix, the f-string `task={entry.task:<15}` raised
    `TypeError: unsupported format string passed to NoneType.__format__`.
    """

    def test_autopilot_entry_does_not_crash(self) -> None:
        from hivepilot.services.schedule_service import ScheduleEntry

        entry = ScheduleEntry(
            name="autopilot-drain", projects=["p"], source="autopilot", interval_minutes=5
        )
        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch(
                "hivepilot.services.schedule_service.load_schedules",
                return_value={"autopilot-drain": entry},
            ),
            patch("hivepilot.services.state_service.get_schedule_last_run", return_value=None),
            patch("hivepilot.services.retry_service.list_queue", return_value=[]),
            patch("hivepilot.services.retry_service.list_dlq", return_value=[]),
        ):
            result = runner.invoke(app, ["schedule", "health"])

        assert result.exit_code == 0, result.output
        assert "<source:autopilot>" in result.output

    def test_task_entry_still_shows_task_name(self) -> None:
        """Normal task-based entries keep printing `task=<name>` unchanged."""
        from hivepilot.services.schedule_service import ScheduleEntry

        entry = ScheduleEntry(name="docs-weekly", projects=["p"], task="docs", interval_minutes=60)
        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch(
                "hivepilot.services.schedule_service.load_schedules",
                return_value={"docs-weekly": entry},
            ),
            patch("hivepilot.services.state_service.get_schedule_last_run", return_value=None),
            patch("hivepilot.services.retry_service.list_queue", return_value=[]),
            patch("hivepilot.services.retry_service.list_dlq", return_value=[]),
        ):
            result = runner.invoke(app, ["schedule", "health"])

        assert result.exit_code == 0, result.output
        assert "task=docs" in result.output

    def test_real_last_run_shows_formatted_date_not_literal_25(self, monkeypatch) -> None:
        """Regression: `last={last or 'never':<25}` applied the `<25`
        format spec directly to a `datetime` -- `datetime.__format__`
        interprets a spec with no `%` codes as an strftime pattern, so it
        rendered the literal string "<25" instead of left-padding a
        readable timestamp. `last` must be converted to a string FIRST,
        then padded.

        Updated for the display-timestamps-local fix: `last` is now
        rendered via `display_time.to_display` (local, marked) instead of
        the raw UTC `strftime` string -- asserting the OLD literal
        "2026-07-20 12:30:00" (raw UTC) would no longer hold by design,
        since a human-facing CLI table must show local time, not UTC. This
        test pins `display_timezone` explicitly so the conversion is
        deterministic regardless of the host running the suite.
        """
        from datetime import datetime, timezone

        from hivepilot.config import settings
        from hivepilot.services.schedule_service import ScheduleEntry

        monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
        entry = ScheduleEntry(name="docs-weekly", projects=["p"], task="docs", interval_minutes=60)
        last_run = datetime(2026, 7, 20, 12, 30, 0, tzinfo=timezone.utc)
        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch(
                "hivepilot.services.schedule_service.load_schedules",
                return_value={"docs-weekly": entry},
            ),
            patch(
                "hivepilot.services.state_service.get_schedule_last_run",
                return_value=last_run,
            ),
            patch("hivepilot.services.retry_service.list_queue", return_value=[]),
            patch("hivepilot.services.retry_service.list_dlq", return_value=[]),
        ):
            result = runner.invoke(app, ["schedule", "health"])

        assert result.exit_code == 0, result.output
        assert "12:30" not in result.output  # raw UTC clock time must not leak through
        assert "14:30" in result.output  # 12:30 UTC == 14:30 CEST
        assert "CEST" in result.output
        assert "<25" not in result.output

    def test_shows_expired_count(self) -> None:
        """fix/retry-queue-drain: `schedule health` must also surface the
        expired-by-TTL count -- a backlog that silently ages out must still
        be visible to the operator, not just 'pending'/'running'/'dead'."""

        def fake_list_queue(status=None):
            if status == "expired":
                return [{"id": 1}, {"id": 2}]
            return []

        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch("hivepilot.services.schedule_service.load_schedules", return_value={}),
            patch("hivepilot.services.retry_service.list_queue", side_effect=fake_list_queue),
            patch("hivepilot.services.retry_service.list_dlq", return_value=[]),
        ):
            result = runner.invoke(app, ["schedule", "health"])

        assert result.exit_code == 0, result.output
        assert "Expired" in result.output
        assert "2" in result.output


class TestScheduleListCommand:
    """`hivepilot schedule list` should print a readable label for
    `source: autopilot` entries instead of the misleading `task=None`."""

    def test_autopilot_entry_shows_source_label(self) -> None:
        from hivepilot.services.schedule_service import ScheduleEntry

        entry = ScheduleEntry(
            name="autopilot-drain", projects=["p"], source="autopilot", interval_minutes=5
        )
        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch(
                "hivepilot.services.schedule_service.load_schedules",
                return_value={"autopilot-drain": entry},
            ),
        ):
            result = runner.invoke(app, ["schedule", "list"])

        assert result.exit_code == 0, result.output
        assert "<source:autopilot>" in result.output
        assert "task=None" not in result.output

    def test_task_entry_still_shows_task_name(self) -> None:
        from hivepilot.services.schedule_service import ScheduleEntry

        entry = ScheduleEntry(name="docs-weekly", projects=["p"], task="docs", interval_minutes=60)
        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch(
                "hivepilot.services.schedule_service.load_schedules",
                return_value={"docs-weekly": entry},
            ),
        ):
            result = runner.invoke(app, ["schedule", "list"])

        assert result.exit_code == 0, result.output
        assert "task=docs" in result.output


# ---------------------------------------------------------------------------
# schedule retry-list / dlq-list -- fix/linear-sync-display-time sweep:
# `next_retry_at`/`created_at` were found echoing the raw stored value
# verbatim, same bug class as the pre-fix `schedule health` table.
# ---------------------------------------------------------------------------


class TestScheduleRetryListCommand:
    def test_next_retry_at_renders_local_time_with_marker(self, monkeypatch) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
        job = {
            "id": 1,
            "schedule_name": "docs-weekly",
            "task": "docs",
            "projects": '["p"]',
            "attempt": 1,
            "max_attempts": 3,
            "status": "pending",
            "next_retry_at": "2026-07-27 09:08:32",
        }
        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch("hivepilot.services.retry_service.list_queue", return_value=[job]),
        ):
            result = runner.invoke(app, ["schedule", "retry-list"])

        assert result.exit_code == 0, result.output
        assert "09:08" not in result.output
        assert "11:08" in result.output
        assert "CEST" in result.output


class TestScheduleDlqListCommand:
    def test_created_at_renders_local_time_with_marker(self, monkeypatch) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
        job = {
            "id": 1,
            "schedule_name": "docs-weekly",
            "task": "docs",
            "attempt": 3,
            "created_at": "2026-07-27 09:08:32",
        }
        runner = CliRunner()
        with (
            patch("hivepilot.cli._require_cli_role", return_value=MagicMock()),
            patch("hivepilot.services.retry_service.list_dlq", return_value=[job]),
        ):
            result = runner.invoke(app, ["schedule", "dlq-list"])

        assert result.exit_code == 0, result.output
        assert "09:08" not in result.output
        assert "11:08" in result.output
        assert "CEST" in result.output


# ---------------------------------------------------------------------------
# workers -- fix/linear-sync-display-time sweep: `last_seen` was found
# echoing the raw stored value verbatim, same bug class as the pre-fix
# `schedule health` table.
# ---------------------------------------------------------------------------


class TestWorkersCommand:
    def test_last_seen_renders_local_time_with_marker(self, monkeypatch) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
        worker = {
            "name": "worker-1",
            "url": "https://worker-1.example.com",
            "status": "up",
            "detail": None,
            "last_seen": "2026-07-27 09:08:32",
        }
        runner = CliRunner()
        with patch("hivepilot.services.state_service.list_workers", return_value=[worker]):
            result = runner.invoke(app, ["workers", "--no-check"])

        assert result.exit_code == 0, result.output
        assert "09:08" not in result.output
        assert "11:08" in result.output
        assert "CEST" in result.output

    def test_no_last_seen_renders_unknown_not_fabricated(self) -> None:
        worker = {
            "name": "worker-1",
            "url": "https://worker-1.example.com",
            "status": "unknown",
            "detail": None,
            "last_seen": None,
        }
        runner = CliRunner()
        with patch("hivepilot.services.state_service.list_workers", return_value=[worker]):
            result = runner.invoke(app, ["workers", "--no-check"])

        from hivepilot.utils import display_time

        assert result.exit_code == 0, result.output
        assert display_time.UNKNOWN_DISPLAY in result.output


# ---------------------------------------------------------------------------
# linear sync -- fix/linear-sync-display-time: the `started_at` column was
# the one sink PR #349 explicitly left unconverted (raw naive-UTC string,
# same bug class as the pre-fix `schedule health`/`drift status` tables).
# ---------------------------------------------------------------------------


class TestLinearSyncCommand:
    def _run(self, runs: list[dict]):
        runner = CliRunner()
        with (
            patch("hivepilot.services.state_service.list_recent_runs", return_value=runs),
        ):
            return runner.invoke(app, ["linear", "sync"])

    def test_started_at_renders_local_time_with_marker_not_raw_utc(self, monkeypatch) -> None:
        """The exact bug: a stored `09:08` UTC run must render as `11:08`
        CEST in the table -- a bare, unmarked `09:08` is precisely how the
        original bug hid from an operator reading their local clock."""
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
        run = {
            "id": 1,
            "project": "acme-web",
            "task": "deploy",
            "status": "success",
            "started_at": "2026-07-27 09:08:32",
        }
        result = self._run([run])

        assert result.exit_code == 0, result.output
        assert "09:08" not in result.output
        assert "11:08" in result.output
        assert "CEST" in result.output

    def test_dst_correctness_summer_vs_winter(self, monkeypatch) -> None:
        """Same wall-clock UTC time, rendered in a July row and a January
        row, must produce DIFFERENT local times/markers -- a fixed-offset
        shortcut (always +2h, or always +1h) would fail this."""
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
        summer_run = {
            "id": 1,
            "project": "acme-web",
            "task": "deploy",
            "status": "success",
            "started_at": "2026-07-27 09:08:32",
        }
        winter_run = {
            "id": 2,
            "project": "acme-web",
            "task": "deploy",
            "status": "success",
            "started_at": "2026-01-27 09:08:32",
        }

        summer_result = self._run([summer_run])
        winter_result = self._run([winter_run])

        assert "11:08" in summer_result.output and "CEST" in summer_result.output
        assert "10:08" in winter_result.output and "CET" in winter_result.output
        assert summer_result.output != winter_result.output

    def test_missing_started_at_renders_unknown_not_fabricated(self, monkeypatch) -> None:
        """Fail-closed: a row with no `started_at` (or an unparseable one)
        must render an explicit unknown marker, never a fabricated or
        silently-wrong local time."""
        from hivepilot.config import settings
        from hivepilot.utils import display_time

        monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
        run = {
            "id": 3,
            "project": "acme-web",
            "task": "deploy",
            "status": "pending",
            # no started_at key at all
        }
        result = self._run([run])

        assert result.exit_code == 0, result.output
        assert display_time.UNKNOWN_DISPLAY in result.output

    def test_no_runs_still_reports_nothing_recorded(self) -> None:
        result = self._run([])
        assert result.exit_code == 0, result.output
        assert "No runs recorded." in result.output
