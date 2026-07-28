"""Tests for directory-sourced skills (`<root>/skills/<name>/SKILL.md`).

Bug (live production box, `hivepilot config doctor`): every skill shipped as
a DIRECTORY inside a config repo was silently invisible -- 14
`dangling_reference` ERRORs naming nine skills that all existed on disk
under `<config-repo-clone>/skills/`.

Confirmed root cause: skills only ever existed as a PLUGIN contribution
(`register()["skills"] = [SkillSpec, ...]`, content inlined in the `files`
dict). There was no filesystem skill discovery of any kind, so no `skills/`
directory -- in a config repo or anywhere else -- was ever read.
`config_service.sync()` copies only `CONFIG_FILES` + `prompts/`, so a stale
copy was not the mechanism either.

Fix: a directory skill SOURCE whose roots mirror `plugins.plugin_scan_dirs`
one-for-one (`hivepilot/skill_dirs.py`), gated by exactly the same
`plugins_enabled` / `config_repo` / `config_repo_load_plugins` switches, so
a config repo can never inject a skill it could not already inject a plugin
from.

Every test here fails against unfixed `origin/main` (`hivepilot.skill_dirs`
does not exist there, and `PluginManager` never reads a `skills/` dir).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
]
for _mod in _STUBS:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from hivepilot import plugins as plugins_mod  # noqa: E402
from hivepilot import skill_dirs as skill_dirs_mod  # noqa: E402
from hivepilot.services import config_service as config_service_mod  # noqa: E402
from hivepilot.services import config_validation  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_skill_dir(
    skills_root: Path,
    name: str,
    *,
    frontmatter: dict | None = None,
    body: str = "# Skill\n",
    extra_files: dict[str, str] | None = None,
    manifest: bool = True,
) -> Path:
    """Create `<skills_root>/<name>/SKILL.md` (+ optional extra files)."""
    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    if manifest:
        text = ""
        if frontmatter is not None:
            text += "---\n" + yaml.dump(frontmatter) + "---\n"
        text += body
        (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill_dir


def _write_skill_plugin(plugins_dir: Path, *, skill_name: str, stem: str = "vendored") -> None:
    plugins_dir.mkdir(parents=True, exist_ok=True)
    skill = {
        "name": skill_name,
        "description": "from-plugin",
        "provider": "acme",
        "files": {"SKILL.md": "plugin content"},
    }
    (plugins_dir / f"{stem}.py").write_text(
        f"def register():\n    return {{'skills': [{skill!r}]}}\n",
        encoding="utf-8",
    )


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Neutralise every ambient discovery root so a test only ever sees what
    it explicitly creates: an empty `base_dir`, no config repo, an empty XDG
    data home (`~/.local/share/hivepilot/{plugins,skills}` on a real
    machine), and no extra plugin dirs."""
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setattr(plugins_mod.settings, "base_dir", base_dir, raising=False)
    monkeypatch.setattr(plugins_mod.settings, "config_repo", None, raising=False)
    monkeypatch.setattr(plugins_mod.settings, "config_repo_load_plugins", True, raising=False)
    monkeypatch.setattr(plugins_mod.settings, "plugins_enabled", True, raising=False)
    monkeypatch.setattr(plugins_mod.settings, "plugins_extra_dirs", [], raising=False)
    monkeypatch.setattr(plugins_mod.settings, "plugins_disabled", [], raising=False)
    monkeypatch.setattr(plugins_mod.settings, "plugins_entry", None, raising=False)
    return base_dir


def _use_config_repo_clone(clone_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        plugins_mod.settings, "config_repo", "https://example.com/cfg.git", raising=False
    )
    monkeypatch.setattr(config_service_mod, "_config_dir", lambda: clone_dir, raising=False)


def _write_config(base_dir: Path, *, tasks: dict | None = None) -> None:
    (base_dir / "projects.yaml").write_text(yaml.dump({"projects": {}}))
    (base_dir / "roles.yaml").write_text(
        yaml.dump({"roles": [{"name": "planner", "prompt_file": "planner.md"}]})
    )
    (base_dir / "policies.yaml").write_text(yaml.dump({"policies": {}}))
    (base_dir / "groups.yaml").write_text(yaml.dump({"groups": {}}))
    (base_dir / "tasks.yaml").write_text(yaml.dump({"tasks": tasks or {}}))
    (base_dir / "pipelines.yaml").write_text(yaml.dump({"pipelines": {}}))
    (base_dir / "prompts" / "agents").mkdir(parents=True)
    (base_dir / "prompts" / "agents" / "planner.md").write_text("# planner")


def _task_referencing(skill_name: str) -> dict:
    return {
        "task-a": {
            "role": "planner",
            "steps": [{"name": "s1", "runner": "claude", "skills": [skill_name]}],
        }
    }


# ---------------------------------------------------------------------------
# 1. A config repo shipping `skills/<name>` resolves that skill
# ---------------------------------------------------------------------------


class TestConfigRepoSkillsResolve:
    def test_config_repo_skill_directory_is_discovered(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE production bug: nine skill DIRECTORIES existed under the synced
        config repo and every reference to them was reported unknown."""
        clone = tmp_path / "clone"
        _write_skill_dir(
            clone / "skills",
            "code-review",
            frontmatter={"description": "Adversarial review pass"},
            body="# Code review\n",
            extra_files={"references/checklist.md": "- security\n"},
        )
        _use_config_repo_clone(clone, monkeypatch)

        skill = plugins_mod.PluginManager().get_skill("code-review")

        assert skill is not None, "config repo skills/<name>/ must resolve"
        assert skill["description"] == "Adversarial review pass"
        assert "SKILL.md" in skill["files"]
        assert "# Code review" in skill["files"]["SKILL.md"]
        assert skill["files"]["references/checklist.md"] == "- security\n"

    def test_provider_names_the_directory_it_came_from(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = tmp_path / "clone"
        skill_dir = _write_skill_dir(clone / "skills", "prd")
        _use_config_repo_clone(clone, monkeypatch)

        skill = plugins_mod.PluginManager().get_skill("prd")

        assert skill is not None
        assert str(skill_dir.resolve()) in skill["provider"]

    def test_optional_frontmatter_is_carried_through(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = tmp_path / "clone"
        _write_skill_dir(
            clone / "skills",
            "tdd-workflow",
            frontmatter={
                "description": "d",
                "system_prompt": "Write the failing test first.",
                "applies_to": ["claude"],
            },
        )
        _use_config_repo_clone(clone, monkeypatch)

        skill = plugins_mod.PluginManager().get_skill("tdd-workflow")

        assert skill is not None
        assert skill["system_prompt"] == "Write the failing test first."
        assert skill["applies_to"] == ["claude"]

    def test_config_repo_as_a_local_directory_also_resolves(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`config_repo` may be a plain local directory (the form
        `Settings.resolve_config_path` tier 2 honours), not only a git URL
        cloned into `xdg_data_home/config-repo`."""
        local_repo = tmp_path / "srv-config"
        _write_skill_dir(local_repo / "skills", "system-design")
        monkeypatch.setattr(plugins_mod.settings, "config_repo", str(local_repo), raising=False)

        assert plugins_mod.PluginManager().get_skill("system-design") is not None


# ---------------------------------------------------------------------------
# 2. A genuinely absent skill is STILL reported dangling (fail closed)
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_absent_skill_is_still_dangling(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        _write_config(config_dir, tasks=_task_referencing("ghost-skill"))
        _write_skill_dir(config_dir / "skills", "real-skill")

        problems = config_validation.validate_config(base_dir=config_dir)

        assert any("ghost-skill" in p for p in problems)

    def test_skill_directory_without_manifest_is_not_registered(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No `SKILL.md` -> not a skill. Loud dangling error, never a silently
        half-registered skill."""
        clone = tmp_path / "clone"
        _write_skill_dir(clone / "skills", "half-baked", manifest=False)
        (clone / "skills" / "half-baked" / "notes.md").write_text("x", encoding="utf-8")
        _use_config_repo_clone(clone, monkeypatch)

        assert plugins_mod.PluginManager().get_skill("half-baked") is None

    def test_invalid_min_role_rejects_the_skill(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrecognised `min_role` must never be stored (a `-1` rank would
        invert the gate to fail-open) -- the skill is refused entirely."""
        clone = tmp_path / "clone"
        _write_skill_dir(
            clone / "skills", "gated", frontmatter={"description": "d", "min_role": "wizard"}
        )
        _use_config_repo_clone(clone, monkeypatch)

        assert plugins_mod.PluginManager().get_skill("gated") is None

    def test_valid_min_role_is_stored(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = tmp_path / "clone"
        _write_skill_dir(
            clone / "skills", "gated-ok", frontmatter={"description": "d", "min_role": "admin"}
        )
        _use_config_repo_clone(clone, monkeypatch)

        skill = plugins_mod.PluginManager().get_skill("gated-ok")

        assert skill is not None
        assert skill["min_role"] == "admin"

    def test_symlink_escaping_the_skill_directory_rejects_the_skill(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = tmp_path / "outside-secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")
        clone = tmp_path / "clone"
        skill_dir = _write_skill_dir(clone / "skills", "escapee")
        (skill_dir / "leak.md").symlink_to(secret)
        _use_config_repo_clone(clone, monkeypatch)

        assert plugins_mod.PluginManager().get_skill("escapee") is None

    def test_description_is_sanitised_for_terminal_output(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`hivepilot skills list` renders `description` straight into a
        terminal table -- a config repo must not be able to smuggle ANSI /
        cursor escapes or extra lines through it."""
        clone = tmp_path / "clone"
        _write_skill_dir(
            clone / "skills",
            "noisy",
            frontmatter={"description": "red \x1b[31malert\x07\nsecond line"},
        )
        _use_config_repo_clone(clone, monkeypatch)

        skill = plugins_mod.PluginManager().get_skill("noisy")

        assert skill is not None
        assert skill["description"] == "red [31malert second line"
        assert "\x1b" not in skill["description"]
        assert "\n" not in skill["description"]

    def test_missing_description_falls_back_instead_of_rejecting(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = tmp_path / "clone"
        _write_skill_dir(clone / "skills", "plain", frontmatter=None)
        _use_config_repo_clone(clone, monkeypatch)

        skill = plugins_mod.PluginManager().get_skill("plain")

        assert skill is not None
        assert skill["description"] == "Skill directory plain"

    def test_directory_skill_never_shadows_a_plugin_skill(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config repo must not be able to REPLACE an installed plugin's
        skill (nor to abort the whole plugin load by colliding with it)."""
        base_dir = isolated_settings
        _write_skill_plugin(base_dir / "plugins", skill_name="shared-name")
        clone = tmp_path / "clone"
        _write_skill_dir(
            clone / "skills", "shared-name", frontmatter={"description": "from-directory"}
        )
        _use_config_repo_clone(clone, monkeypatch)

        skill = plugins_mod.PluginManager().get_skill("shared-name")

        assert skill is not None
        assert skill["description"] == "from-plugin"
        assert skill["files"]["SKILL.md"] == "plugin content"


# ---------------------------------------------------------------------------
# 3. Trust gates: identical to the sibling `plugins/` path
# ---------------------------------------------------------------------------


class TestTrustGates:
    def test_config_repo_load_plugins_false_also_blocks_skills(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = tmp_path / "clone"
        _write_skill_dir(clone / "skills", "blocked")
        _use_config_repo_clone(clone, monkeypatch)
        monkeypatch.setattr(plugins_mod.settings, "config_repo_load_plugins", False, raising=False)

        assert plugins_mod.PluginManager().get_skill("blocked") is None
        assert all("clone" not in str(d) for d in skill_dirs_mod.skill_scan_dirs()), (
            "the config repo root must not even be listed when auto-load is off"
        )

    def test_plugins_disabled_master_switch_blocks_skills(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = tmp_path / "clone"
        _write_skill_dir(clone / "skills", "blocked")
        _use_config_repo_clone(clone, monkeypatch)
        monkeypatch.setattr(plugins_mod.settings, "plugins_enabled", False, raising=False)

        assert skill_dirs_mod.skill_scan_dirs() == []
        assert plugins_mod.PluginManager().get_skill("blocked") is None


# ---------------------------------------------------------------------------
# 4. No config repo / empty skills dir behave exactly as before
# ---------------------------------------------------------------------------


class TestUnchangedWithoutConfigRepo:
    def test_no_config_repo_lists_no_config_repo_root(self, isolated_settings: Path) -> None:
        dirs = skill_dirs_mod.skill_scan_dirs()

        assert dirs == [isolated_settings / "skills"]
        assert skill_dirs_mod.discover_directory_skills() == []

    def test_empty_skills_directory_registers_nothing(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = tmp_path / "clone"
        (clone / "skills").mkdir(parents=True)
        _use_config_repo_clone(clone, monkeypatch)

        assert skill_dirs_mod.discover_directory_skills() == []
        assert plugins_mod.PluginManager().skills == {}

    def test_dot_directories_are_ignored(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = tmp_path / "clone"
        (clone / "skills" / ".git" / "objects").mkdir(parents=True)
        (clone / "skills" / ".git" / "objects" / "SKILL.md").write_text("x", encoding="utf-8")
        _use_config_repo_clone(clone, monkeypatch)

        assert skill_dirs_mod.discover_directory_skills() == []

    def test_first_root_wins_across_roots(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Precedence mirrors `_scan_plugin_dir`'s dedup-by-stem: the first
        root listed wins, no collision error."""
        base_dir = isolated_settings
        _write_skill_dir(base_dir / "skills", "dup", frontmatter={"description": "from-base"})
        clone = tmp_path / "clone"
        _write_skill_dir(clone / "skills", "dup", frontmatter={"description": "from-clone"})
        _use_config_repo_clone(clone, monkeypatch)

        found = skill_dirs_mod.discover_directory_skills()

        assert [s["name"] for s in found] == ["dup"]
        assert found[0]["description"] == "from-base"


# ---------------------------------------------------------------------------
# 5. The reported "searched:" list matches the roots actually consulted
# ---------------------------------------------------------------------------


class TestSearchedListHonesty:
    def test_unknown_skill_message_names_every_root_consulted(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        _write_config(config_dir, tasks=_task_referencing("ghost-skill"))

        report = config_validation.validate_config_report(base_dir=config_dir)

        matching = [p for p in report.problems if "ghost-skill" in p]
        assert matching, f"expected a dangling-skill problem, got {report.problems}"
        message = matching[0]
        for root in report.plugin_dirs + report.skill_dirs:
            assert str(root) in message, f"{root} was consulted but not named in: {message}"
        assert str((config_dir / "skills").resolve()) in message

    def test_report_exposes_the_skill_roots(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        _write_config(config_dir)

        report = config_validation.validate_config_report(base_dir=config_dir)

        assert report.skill_dirs == [
            d.resolve() for d in skill_dirs_mod.skill_scan_dirs(base_dir=config_dir)
        ]
        assert (config_dir / "skills").resolve() in report.skill_dirs


# ---------------------------------------------------------------------------
# 6. Isolated-directory mode (`validate --dir`, config-writer scratch copy)
# ---------------------------------------------------------------------------


class TestIsolatedDirectoryMode:
    def test_validate_dir_resolves_that_directorys_own_skills(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`hivepilot validate --dir <config-repo-clone>` must resolve the
        skills shipped in THAT clone -- the pre-sync review workflow."""
        config_dir = tmp_path / "clone"
        config_dir.mkdir()
        _write_config(config_dir, tasks=_task_referencing("market-research"))
        _write_skill_dir(config_dir / "skills", "market-research")
        outside = tmp_path / "outside"
        outside.mkdir()
        monkeypatch.chdir(outside)

        assert config_validation.validate_config(base_dir=config_dir) == []

    def test_isolated_mode_ignores_the_ambient_config_repo(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ambient global state must never change an isolated verdict --
        mirrors `plugin_scan_dirs`' explicit-base_dir contract."""
        clone = tmp_path / "clone"
        _write_skill_dir(clone / "skills", "ambient")
        _use_config_repo_clone(clone, monkeypatch)
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        _write_config(config_dir, tasks=_task_referencing("ambient"))

        problems = config_validation.validate_config(base_dir=config_dir)

        assert any("ambient" in p for p in problems)
        assert skill_dirs_mod.skill_scan_dirs(base_dir=config_dir) == [config_dir / "skills"]

    def test_config_writer_scratch_copy_carries_skills(
        self, isolated_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`config set`/`config edit` validate a prospective mutation against
        a scratch COPY of the config dir. Without copying `skills/` too, a
        perfectly valid skill reference would false-positive as unknown and
        block every write."""
        from hivepilot.services import config_writer

        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        _write_config(config_dir, tasks=_task_referencing("documentation-writer"))
        _write_skill_dir(config_dir / "skills", "documentation-writer")

        mutated = (config_dir / "tasks.yaml").read_text(encoding="utf-8")
        errors = config_writer._validate_prospective("tasks.yaml", mutated, config_dir)

        assert errors == []
