"""The doctor reported what was CONFIGURED. These checks report what runs.

Each check here exists because the same shape cost real time on this
deployment: a path resolves, nothing is behind it, nothing errors, so nothing
surfaces.

- a `state.db` that exists and holds zero runs, sitting beside the one with
  the history, answering every query plausibly;
- `plugin.obsidian.recalled` logged for months against a vault directory that
  did not exist;
- `HIVEPILOT_TOKEN_SAVIOR_ENABLED=true` in the file the services read, for a
  plugin whose file had never been installed;
- 23 forum-topic keys against a 20-role roster.

So the assertions below are mostly about the *quiet* cases: the ones where a
check must still emit something. A check that only speaks up on an error
would have stayed silent through every one of the incidents above.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hivepilot.services import doctor_liveness as dl


def _messages(findings) -> str:
    return " | ".join(f.message for f in findings)


def _severities(findings) -> set[str]:
    return {f.severity for f in findings}


def _make_db(path: Path, runs: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO runs (id) VALUES (?)", [(i,) for i in range(1, runs + 1)])
    conn.commit()
    conn.close()


class TestStateDbLiveness:
    """A path that resolves is not a database that has anything in it."""

    def test_a_populated_db_reports_its_row_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reported even when healthy — the count is what makes a future 0
        mean something. A path alone never could."""
        db = tmp_path / "state.db"
        _make_db(db, 7)
        monkeypatch.setattr(dl.settings, "state_db", db)

        findings = dl.check_state_db_liveness()

        assert _severities(findings) == {"info"}
        assert "7 runs" in _messages(findings)

    def test_an_empty_db_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The live decoy: /root/state.db exists, is valid SQLite, holds zero
        runs, and answers every query without error."""
        db = tmp_path / "state.db"
        _make_db(db, 0)
        monkeypatch.setattr(dl.settings, "state_db", db)

        findings = dl.check_state_db_liveness()

        assert _severities(findings) == {"warning"}
        assert "0 runs" in _messages(findings)

    def test_a_missing_db_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """SQLite CREATES a missing file on write, so a wrong path looks
        exactly like a fresh install and never raises."""
        monkeypatch.setattr(dl.settings, "state_db", tmp_path / "nope.db")

        findings = dl.check_state_db_liveness()

        assert _severities(findings) == {"warning"}
        assert "does not exist" in _messages(findings)

    def test_a_file_that_is_not_a_database_warns_instead_of_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A doctor check that raises takes down every check after it."""
        junk = tmp_path / "state.db"
        junk.write_text("not a database")
        monkeypatch.setattr(dl.settings, "state_db", junk)

        findings = dl.check_state_db_liveness()

        assert _severities(findings) == {"warning"}


class TestVaultLiveness:
    """Configured is not the same as present on disk."""

    def test_a_missing_vault_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Recall against a missing vault returns nothing and logs success —
        indistinguishable from a vault with nothing to say. That ran for
        months here."""
        monkeypatch.setattr(dl.settings, "obsidian_vault", str(tmp_path / "absent"), raising=False)

        findings = dl.check_vault_liveness()

        assert _severities(findings) == {"warning"}
        assert "does not exist" in _messages(findings)

    def test_an_existing_vault_reports_its_note_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = tmp_path / "vault"
        (vault / "sub").mkdir(parents=True)
        (vault / "a.md").write_text("x")
        (vault / "sub" / "b.md").write_text("y")
        monkeypatch.setattr(dl.settings, "obsidian_vault", str(vault), raising=False)

        findings = dl.check_vault_liveness()

        assert _severities(findings) == {"info"}
        assert "2 notes" in _messages(findings)

    def test_an_empty_vault_warns_because_it_recalls_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existence was never the question worth asking — an empty vault
        contributes exactly as much as a missing one."""
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(dl.settings, "obsidian_vault", str(vault), raising=False)

        findings = dl.check_vault_liveness()

        assert _severities(findings) == {"warning"}

    def test_an_unconfigured_vault_still_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Silence would be wrong: it is the enabled-plugin case that matters."""
        monkeypatch.setattr(dl.settings, "obsidian_vault", None, raising=False)

        findings = dl.check_vault_liveness()

        assert _severities(findings) == {"info"}
        assert "no obsidian vault" in _messages(findings)


class TestRegisteredHooks:
    """A plugin that loads is not a plugin whose hooks are wired."""

    def test_wired_hooks_are_counted(self) -> None:
        pm = SimpleNamespace(hooks={"before_step": [object(), object()], "after_step": [object()]})

        findings = dl.check_registered_hooks(pm)

        assert _severities(findings) == {"info"}
        assert "before_step=2" in _messages(findings)
        assert "after_step=1" in _messages(findings)

    def test_a_declared_but_empty_hook_is_not_counted_as_wired(self) -> None:
        """`plugins list` shows what a plugin CLAIMS to contribute. An empty
        list is a claim with nothing behind it."""
        pm = SimpleNamespace(hooks={"before_step": []})

        findings = dl.check_registered_hooks(pm)

        assert "no lifecycle hooks registered" in _messages(findings)

    def test_a_manager_without_the_attribute_warns_rather_than_crashing(self) -> None:
        """The attribute is `hooks`, not `_hooks` — reaching for the wrong one
        yields an empty result that reads as 'nothing is wired'."""
        findings = dl.check_registered_hooks(SimpleNamespace())

        assert _severities(findings) == {"warning"}


class TestPluginsWrittenVsInstalled:
    """Written, installed and enabled are three different states."""

    def test_explicitly_enabled_but_not_installed_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live case: HIVEPILOT_TOKEN_SAVIOR_ENABLED=true with no
        token_savior.py on disk. Every surface reported it as on."""
        import hivepilot.services.config_doctor as cd
        import hivepilot.services.plugin_installer as pi

        monkeypatch.setattr(pi, "is_installed", lambda name: False)
        monkeypatch.setattr(pi, "is_enabled", lambda name: name == "rtk")
        monkeypatch.setattr(cd, "_is_setting_explicit", lambda s, key: True)

        findings = dl.check_plugins_written_vs_installed(SimpleNamespace())

        assert any(f.severity == "warning" and "rtk" in f.message for f in findings)

    def test_a_default_true_flag_is_not_warned_about(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Many of these plugins default to enabled=True. Warning on every one
        of them buries the single case that matters under a dozen lines of
        noise -- which is exactly what the first draft of this check did."""
        import hivepilot.services.config_doctor as cd
        import hivepilot.services.plugin_installer as pi

        monkeypatch.setattr(pi, "is_installed", lambda name: False)
        monkeypatch.setattr(pi, "is_enabled", lambda name: True)
        monkeypatch.setattr(cd, "_is_setting_explicit", lambda s, key: False)

        findings = dl.check_plugins_written_vs_installed(SimpleNamespace())

        assert not [f for f in findings if f.severity == "warning"]

    def test_an_undeterminable_flag_stays_quiet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_is_setting_explicit` returns None when it cannot tell. This is a
        signal-to-noise judgement, not a security gate, so None means quiet."""
        import hivepilot.services.config_doctor as cd
        import hivepilot.services.plugin_installer as pi

        monkeypatch.setattr(pi, "is_installed", lambda name: False)
        monkeypatch.setattr(pi, "is_enabled", lambda name: True)
        monkeypatch.setattr(cd, "_is_setting_explicit", lambda s, key: None)

        findings = dl.check_plugins_written_vs_installed(SimpleNamespace())

        assert not [f for f in findings if f.severity == "warning"]

    def test_the_installed_gap_is_reported_even_with_nothing_wrong(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not installed is a normal state. Unnoticed is not — that is how
        twenty-odd written plugins sat inert."""
        import hivepilot.services.plugin_installer as pi

        monkeypatch.setattr(pi, "is_installed", lambda name: False)

        findings = dl.check_plugins_written_vs_installed(SimpleNamespace())

        assert any(
            f.severity == "info" and "curated plugins installed" in f.message for f in findings
        )

    def test_everything_installed_reports_no_gap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import hivepilot.services.plugin_installer as pi

        monkeypatch.setattr(pi, "is_installed", lambda name: True)

        findings = dl.check_plugins_written_vs_installed(SimpleNamespace())

        assert not [f for f in findings if "curated plugins installed" in f.message]


class TestOrphanTopicKeys:
    """Telegram cannot list forum topics, so the registry is the only
    inventory there is."""

    def _registry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mapping: dict) -> None:
        path = tmp_path / "stream_topics.json"
        path.write_text(json.dumps(mapping), encoding="utf-8")
        import hivepilot.services.notification_service as ns

        monkeypatch.setattr(ns, "_topics_registry_path", lambda: path)

    def test_a_key_that_is_no_role_is_named_with_its_thread_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Turns "delete some topics" into "delete these"."""
        import hivepilot.roles

        monkeypatch.setattr(hivepilot.roles, "ROLES", {"developer": object()})
        self._registry(tmp_path, monkeypatch, {"developer": 330, "refresh": 579})

        findings = dl.check_orphan_topic_keys()

        assert "refresh (thread 579)" in _messages(findings)
        assert "developer" not in _messages(findings)

    def test_a_declared_stream_key_is_not_an_orphan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hivepilot.roles

        monkeypatch.setattr(hivepilot.roles, "ROLES", {"developer": object()})
        self._registry(tmp_path, monkeypatch, {"hivepilot": 328})

        assert dl.check_orphan_topic_keys() == []

    def test_an_empty_roster_reports_nothing_rather_than_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mid-reload, every key would look orphaned. Reporting the whole
        registry as dead is worse than staying quiet for one tick."""
        import hivepilot.roles

        monkeypatch.setattr(hivepilot.roles, "ROLES", {})
        self._registry(tmp_path, monkeypatch, {"developer": 330, "refresh": 579})

        assert dl.check_orphan_topic_keys() == []

    def test_an_unreadable_registry_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable registry means every agent gets a NEW topic on the
        next send, silently doubling the group."""
        path = tmp_path / "stream_topics.json"
        path.write_text("{ this is not json", encoding="utf-8")
        import hivepilot.services.notification_service as ns

        monkeypatch.setattr(ns, "_topics_registry_path", lambda: path)

        findings = dl.check_orphan_topic_keys()

        assert _severities(findings) == {"warning"}
