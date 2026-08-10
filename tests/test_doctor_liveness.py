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
    """Configured is not the same as present on disk — and the GLOBAL setting
    is not the whole story.

    The first version of this check looked only at the global vault and
    concluded the plugin was dead. It was wrong: `_resolve_vault` prefers a
    project's own `obsidian_vault:` override, and the two projects carrying
    one had been recalling from real, populated vaults the entire time.
    Turning a partial gap into a false total-failure claim is worse than
    saying nothing — a check that cries wolf gets ignored.
    """

    def _projects(self, monkeypatch: pytest.MonkeyPatch, mapping: dict[str, object]) -> None:
        from types import SimpleNamespace

        import hivepilot.services.project_service as ps

        projects = {name: SimpleNamespace(obsidian_vault=vault) for name, vault in mapping.items()}
        monkeypatch.setattr(ps, "load_projects", lambda *a, **k: SimpleNamespace(projects=projects))

    def test_a_project_with_its_own_vault_is_reported_from_that_vault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The precedence the first version ignored."""
        vault = tmp_path / "noxys-vault"
        vault.mkdir()
        (vault / "a.md").write_text("x")
        monkeypatch.setattr(dl.settings, "obsidian_vault", None, raising=False)
        self._projects(monkeypatch, {"noxys": str(vault)})

        findings = dl.check_vault_liveness()

        assert _severities(findings) == {"info"}
        assert "noxys" in _messages(findings)
        assert "1 notes" in _messages(findings)

    def test_an_unset_global_is_silent_when_nothing_depends_on_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deliberately changed. Every project declaring its own vault means
        the global is genuinely unused, and warning about it is the noise that
        makes operators stop reading the report."""
        vault = tmp_path / "v"
        vault.mkdir()
        (vault / "a.md").write_text("x")
        monkeypatch.setattr(dl.settings, "obsidian_vault", None, raising=False)
        self._projects(monkeypatch, {"noxys": str(vault)})

        assert not [f for f in dl.check_vault_liveness() if f.severity == "warning"]

    def test_an_unset_global_warns_and_NAMES_the_projects_that_fall_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real gap, and the actionable form of it: those projects recall
        nothing and write no run notes while the plugin reports healthy."""
        monkeypatch.setattr(dl.settings, "obsidian_vault", None, raising=False)
        self._projects(monkeypatch, {"noxys-ml": None, "noxys-website": None})

        findings = dl.check_vault_liveness()

        assert _severities(findings) == {"warning"}
        assert "noxys-ml" in _messages(findings)
        assert "noxys-website" in _messages(findings)

    def test_a_missing_global_vault_warns_when_projects_rely_on_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dl.settings, "obsidian_vault", str(tmp_path / "absent"), raising=False)
        self._projects(monkeypatch, {"noxys-ml": None})

        findings = dl.check_vault_liveness()

        assert _severities(findings) == {"warning"}
        assert "does not exist" in _messages(findings)

    def test_an_empty_vault_warns_because_it_recalls_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existence was never the question worth asking — an empty vault
        contributes exactly as much as a missing one."""
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(dl.settings, "obsidian_vault", str(vault), raising=False)
        self._projects(monkeypatch, {})

        assert _severities(dl.check_vault_liveness()) == {"warning"}

    def test_an_unreadable_vault_is_reported_rather_than_crashing_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first rewrite raised PermissionError and took the whole check
        down — caught by `_run_check`, which is the safety net, not the fix."""
        monkeypatch.setattr(dl.settings, "obsidian_vault", "/root/someone-elses", raising=False)
        self._projects(monkeypatch, {})

        def boom(self: Path) -> bool:
            raise PermissionError("nope")

        monkeypatch.setattr(Path, "exists", boom)

        findings = dl.check_vault_liveness()

        assert all(f.severity in {"info", "warning"} for f in findings)


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


class TestAgentPrivilege:
    """Agents read a client's PR diff — untrusted input — and run shell
    commands. As root, the tool allowlist guards a door that is not the only
    way in: the agent can read `/etc/hivepilot/shared.env` directly.
    """

    def test_root_is_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("os.geteuid", lambda: 0)

        findings = dl.check_agent_privilege()

        assert _severities(findings) == {"warning"}
        assert "root" in _messages(findings)

    def test_non_root_is_reported_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Printed even when correct, so a future change back to root reads as
        a change rather than being discovered months later."""
        monkeypatch.setattr("os.geteuid", lambda: 1001)

        findings = dl.check_agent_privilege()

        assert _severities(findings) == {"info"}
        assert "1001" in _messages(findings)

    def test_root_is_a_warning_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only `error` makes `config doctor` exit non-zero. Root is a
        documented, supported deployment — failing the whole command on it
        would train people to stop running the doctor."""
        monkeypatch.setattr("os.geteuid", lambda: 0)

        assert all(f.severity != "error" for f in dl.check_agent_privilege())

    def test_the_fix_names_the_systemd_mechanism(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """systemd parses EnvironmentFile= as root BEFORE dropping privileges,
        which is what lets shared.env stay 0600 root:root and unreadable by the
        agent. Without that fact the advice looks like it would break the
        service."""
        monkeypatch.setattr("os.geteuid", lambda: 0)

        assert "EnvironmentFile" in dl.check_agent_privilege()[0].fix


class TestAgentPrivilegeReportsTheServiceUser:
    """The check reported the uid of whatever process ran `config doctor`.

    After the production migration to `User=hivepilot`, a root CLI run still
    printed "agents run as root" — true of that shell, false of the fleet.
    A check answering a different question than the one it appears to answer
    is the failure this module exists to remove, and it was mine.

    The services' user is declared in systemd, so it can be read rather than
    guessed.
    """

    def test_it_reports_the_unit_user_when_systemd_declares_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setattr(dl, "_systemd_service_user", lambda: "hivepilot")

        findings = dl.check_agent_privilege()

        assert _severities(findings) == {"info"}
        assert "hivepilot" in _messages(findings)

    def test_a_unit_declaring_root_is_still_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setattr(dl, "_systemd_service_user", lambda: "root")

        assert _severities(dl.check_agent_privilege()) == {"warning"}

    def test_it_falls_back_to_this_process_when_systemd_says_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No systemd, or a unit with no `User=`: the invoking process is the
        best evidence available, and the message must say which it is."""
        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setattr(dl, "_systemd_service_user", lambda: None)

        findings = dl.check_agent_privilege()

        assert _severities(findings) == {"warning"}
        assert "this process" in _messages(findings)

    def test_probing_systemd_never_breaks_the_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("os.geteuid", lambda: 0)

        def boom():
            raise OSError("no systemctl")

        monkeypatch.setattr(dl, "_systemd_service_user", boom)

        assert dl.check_agent_privilege()[0].severity in {"info", "warning"}
