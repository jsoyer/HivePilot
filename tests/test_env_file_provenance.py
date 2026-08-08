"""Which `.env` is governing this process, and can anyone find out?

`_resolve_env_file()` ends with `return ".env"` — a **relative** path. The
systemd units run with `WorkingDirectory=/`, so on the deployment box that
resolves to `/.env`, which is where `plugins install` wrote the plugin
activation flags. Nothing in any unit file names it, so "which plugins are
enabled here" was only answerable by archaeology.

This is the **fourth** cwd-relative silo found in this codebase, after
`state.db`, `logs_dir` and the plugin install directory. The pattern is the
defect, not each instance — and it bit again during this very session: a bare
`cache_summary()` on the box read an empty database because the shell had none
of the service environment, and returned three reassuring zeroes.

So: record where the file came from, and let `doctor` say so. Reporting, not
relocating — moving the file would strand whatever is already in it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.config import describe_env_file, resolve_env_file_with_provenance


class TestProvenanceIsRecorded:
    def test_explicit_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "custom.env"
        target.write_text("HIVEPILOT_X=1\n")
        monkeypatch.setenv("HIVEPILOT_ENV_FILE", str(target))

        path, provenance = resolve_env_file_with_provenance()

        assert path == str(target)
        assert provenance == "explicit"

    def test_xdg_is_used_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HIVEPILOT_ENV_FILE", raising=False)
        xdg = tmp_path / "hivepilot"
        xdg.mkdir(parents=True)
        (xdg / ".env").write_text("HIVEPILOT_X=1\n")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        path, provenance = resolve_env_file_with_provenance()

        assert path == str(xdg / ".env")
        assert provenance == "xdg"

    def test_the_fallback_is_named_as_relative(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dangerous case, and the one that has to be legible: a bare
        `.env` resolves against whatever the cwd happens to be."""
        monkeypatch.delenv("HIVEPILOT_ENV_FILE", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))

        path, provenance = resolve_env_file_with_provenance()

        assert path == ".env"
        assert provenance == "cwd-relative"


class TestTheDescriptionIsActionable:
    def test_a_relative_fallback_is_resolved_to_an_absolute_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`.env` tells an operator nothing. `/.env` tells them where to
        look — and that it is not where they expected."""
        monkeypatch.delenv("HIVEPILOT_ENV_FILE", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
        monkeypatch.chdir(tmp_path)

        text = describe_env_file()

        assert str(tmp_path) in text
        assert "cwd-relative" in text

    def test_it_says_whether_the_file_actually_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolved path that holds no file governs nothing, and reads very
        differently from one that is quietly setting flags."""
        monkeypatch.delenv("HIVEPILOT_ENV_FILE", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
        monkeypatch.chdir(tmp_path)

        absent = describe_env_file()
        (tmp_path / ".env").write_text("HIVEPILOT_X=1\n")
        present = describe_env_file()

        assert "not present" in absent
        assert "not present" not in present

    def test_an_explicit_path_is_reported_plainly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "svc.env"
        target.write_text("HIVEPILOT_X=1\n")
        monkeypatch.setenv("HIVEPILOT_ENV_FILE", str(target))

        text = describe_env_file()

        assert str(target) in text
        assert "explicit" in text
        assert "cwd-relative" not in text
