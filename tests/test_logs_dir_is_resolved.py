"""`logs_dir` must be resolved, not used raw.

`configure_logging` called `settings.logs_dir.mkdir(parents=True)` on the RAW
value. The default is the relative `runs/logs`, so pathlib resolved it against
the process CWD — not against `base_dir`, which every other path in this
codebase is anchored to.

It survived only because the units run with `WorkingDirectory=/` and `/runs`
happened to exist and be writable. Moving that directory to its proper home
under `/var/lib/hivepilot` broke logging outright: 81 `PermissionError:
'runs'` in three minutes, and the event log frozen — while `systemctl` still
reported all five units active.

(The error names `runs`, not `runs/logs`: `mkdir(parents=True)` fails on the
PARENT it tries to create first. That cost a wrong diagnosis of "two raw uses"
when there is one.)

Fourth instance of the shape the codebase already documents for `state.db`,
the plugin install dir and the `.env`.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def logging_module(monkeypatch):
    """`configure_logging` is guarded by a module-level `_configured` flag and
    no-ops after the first call — which importing anything already made. Reset
    it, and restore the handlers afterwards so the reconfiguration does not
    leak into the rest of the suite."""
    import logging as std_logging

    from hivepilot.utils import logging as hp_logging

    root = std_logging.getLogger()
    saved = list(root.handlers)
    monkeypatch.setattr(hp_logging, "_configured", False, raising=False)
    yield hp_logging
    root.handlers = saved


class TestTheLogDirectoryFollowsBaseDirNotTheCwd:
    def test_a_relative_logs_dir_is_anchored_to_base_dir(
        self, logging_module, tmp_path, monkeypatch
    ):
        """The whole defect: with base_dir pinned, a relative `runs/logs` must
        land under it — never under whatever directory the process started in."""
        from hivepilot.config import settings

        base = tmp_path / "anchor"
        base.mkdir()
        monkeypatch.setattr(settings, "base_dir", base, raising=False)
        monkeypatch.setattr(settings, "logs_dir", Path("runs/logs"), raising=False)
        monkeypatch.chdir(tmp_path)  # a DIFFERENT cwd, which must not win

        logging_module.configure_logging()

        assert (base / "runs" / "logs").is_dir()
        assert not (tmp_path / "runs").exists(), "the cwd must not receive the log directory"

    def test_an_absolute_logs_dir_is_left_alone(self, logging_module, tmp_path, monkeypatch):
        from hivepilot.config import settings

        target = tmp_path / "explicit" / "logs"
        monkeypatch.setattr(settings, "logs_dir", target, raising=False)
        monkeypatch.setattr(settings, "base_dir", tmp_path / "unused", raising=False)

        logging_module.configure_logging()

        assert target.is_dir()

    def test_the_handler_writes_where_the_directory_was_made(
        self, logging_module, tmp_path, monkeypatch
    ):
        """A directory created in one place and a handler opened in another is
        the same bug wearing a different hat."""
        from hivepilot.config import settings

        base = tmp_path / "anchor"
        base.mkdir()
        monkeypatch.setattr(settings, "base_dir", base, raising=False)
        monkeypatch.setattr(settings, "logs_dir", Path("runs/logs"), raising=False)
        monkeypatch.chdir(tmp_path)

        logging_module.configure_logging()
        logging_module.get_logger(__name__).info("probe.line")

        assert (base / "runs" / "logs" / "hivepilot.log").exists()
