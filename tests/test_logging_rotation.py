"""Logging must survive the log file being rotated out from under it.

Measured on the production box: **five** HivePilot processes hold write
descriptors on the same inode.

    hivepilot 77333 root 6w REG 2816256 22 /runs/logs/hivepilot.log
    hivepilot 77334 root 3w REG 2816256 22 /runs/logs/hivepilot.log
    ... (api, scheduler, telegram, slack, discord)

`logging.FileHandler` opens once and holds that descriptor forever. When
logrotate renames the file, every one of those processes keeps writing to
the renamed inode — and the fresh `hivepilot.log` stays empty for good.
The box already runs `logrotate.timer` daily, so this was a latent failure
waiting on the first rotation of this path.

The failure is the quiet kind this codebase keeps meeting: `watch` would
report *"empty, never written"*, the services would look idle, and every
line would still be landing in `hivepilot.log.1`.

`WatchedFileHandler` reopens when the inode changes, which is what makes
external rotation safe — and it is external rotation we want here, not
in-process. `RotatingFileHandler` would have five processes racing to
rename the same file, each holding a descriptor the others invalidated;
one writer wins and the rest are orphaned. Rotation belongs to a single
scheduled process, and the writers only have to notice.
"""

from __future__ import annotations

import json
import logging

import pytest

from hivepilot.utils import logging as hp_logging


@pytest.fixture
def fresh_logging(tmp_path, monkeypatch):
    """Configure logging into a temp dir, then put the world back.

    `configure_logging` is guarded by a module-level `_configured` flag and
    `basicConfig` is a no-op once handlers exist, so both have to be reset
    or the test silently measures the previous configuration.
    """
    previous_structlog = __import__("structlog").get_config()
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level

    monkeypatch.setattr(hp_logging.settings, "logs_dir", tmp_path)
    monkeypatch.setattr(hp_logging, "_configured", False)
    root.handlers = []

    hp_logging.configure_logging()
    yield tmp_path / "hivepilot.log"

    root.handlers = previous_handlers
    root.setLevel(previous_level)
    __import__("structlog").configure(**previous_structlog)
    hp_logging._configured = False


def _lines(path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class TestRotationDoesNotOrphanTheWriter:
    def test_writing_resumes_into_the_new_file(self, fresh_logging) -> None:
        """The whole point: after a rename, the next line lands in the file
        that now bears the name — not in the renamed one."""
        log = fresh_logging
        logger = hp_logging.get_logger("rotation-test")

        logger.info("before.rotation")
        rotated = log.with_suffix(".log.1")
        log.rename(rotated)
        logger.info("after.rotation")

        events = [e.get("event") for e in _lines(log)]
        assert "after.rotation" in events

    def test_the_pre_rotation_line_stays_in_the_rotated_file(self, fresh_logging) -> None:
        """Nothing is lost — the earlier records are in `.1`, where an
        operator looking for history expects them."""
        log = fresh_logging
        logger = hp_logging.get_logger("rotation-test")

        logger.info("before.rotation")
        rotated = log.with_suffix(".log.1")
        log.rename(rotated)
        logger.info("after.rotation")

        assert "before.rotation" in [e.get("event") for e in _lines(rotated)]

    def test_the_new_file_does_not_inherit_the_old_records(self, fresh_logging) -> None:
        log = fresh_logging
        logger = hp_logging.get_logger("rotation-test")

        logger.info("before.rotation")
        log.rename(log.with_suffix(".log.1"))
        logger.info("after.rotation")

        assert "before.rotation" not in [e.get("event") for e in _lines(log)]

    def test_deletion_is_survived_too(self, fresh_logging) -> None:
        """`logrotate` with no `create`, or a stray `rm`, removes the file
        outright. A writer that stops there is the same silent failure."""
        log = fresh_logging
        logger = hp_logging.get_logger("rotation-test")

        logger.info("first")
        log.unlink()
        logger.info("second")

        assert "second" in [e.get("event") for e in _lines(log)]


class TestTheFollowerCrossesARealRotation:
    def test_watch_reads_past_a_rotation_performed_by_the_logger(self, fresh_logging) -> None:
        """The two halves must work together, not merely each alone.

        `follow_lines` was tested against a synthetic `rename`; this drives
        the real handler, so the follower and the writer agree about what a
        rotation looks like.
        """
        from hivepilot.services import watch_service

        log = fresh_logging
        logger = hp_logging.get_logger("rotation-test")
        logger.info("pre.rotation")

        gen = watch_service.follow_lines(log, from_start=True, poll_seconds=0.01)
        assert "pre.rotation" in next(gen)

        log.rename(log.with_suffix(".log.1"))
        logger.info("post.rotation")

        assert "post.rotation" in next(gen)
        gen.close()
