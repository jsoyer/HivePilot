"""Following the event stream, without lying about which stream.

`hivepilot watch` is the process a herdr pane runs. herdr has no PTY-less
pane -- a pane node without `command` starts a shell, it does not become a
passive surface -- so the board is built from panes whose process is a
follower rather than an agent.

Two hazards drive most of this file.

**The log path is a cwd-relative silo.** `logs_dir` defaults to `runs/logs`
relative to the working directory, exactly like `state.db`. Measured on the
production box: five `hivepilot.log` files, one per directory the CLI was
ever run from, two of them empty since July. Only `/runs/logs/` is live,
because the units set `WorkingDirectory=/`. A follower that opens "the" log
picks a dead one and displays nothing -- which is indistinguishable from a
quiet system. So `watch` states the path it opened and whether that file has
ever been written, before any event.

**The stream is untrusted text about to enter a terminal.** Agent output is
quoted verbatim into `detail`. Control characters must not survive, and an
embedded newline must not be able to fabricate a line that looks like an
event the system emitted.
"""

from __future__ import annotations

import json

from hivepilot.services import watch_service


def _event(**kw) -> str:
    base = {
        "event": "state.step",
        "run_id": 42,
        "step": "review:ciso",
        "status": "success",
        "role": "ciso",
    }
    base.update(kw)
    return json.dumps(base)


class TestItNamesTheFileItOpened:
    def test_the_description_carries_the_resolved_path(self, tmp_path) -> None:
        log = tmp_path / "runs" / "logs" / "hivepilot.log"
        log.parent.mkdir(parents=True)
        log.write_text(_event() + "\n")

        source = watch_service.resolve_log_source(logs_dir=log.parent)

        assert source.path == log
        assert str(log) in watch_service.describe_source(source)

    def test_the_path_is_absolute_even_when_configured_relative(
        self, tmp_path, monkeypatch
    ) -> None:
        """The whole defect is relative resolution. A description holding a
        relative path would not tell the operator which of five files it is."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "runs" / "logs").mkdir(parents=True)

        source = watch_service.resolve_log_source(logs_dir="runs/logs")

        assert source.path.is_absolute()

    def test_a_missing_file_is_announced_not_swallowed(self, tmp_path) -> None:
        source = watch_service.resolve_log_source(logs_dir=tmp_path / "nowhere")

        assert not source.exists
        assert "does not exist" in watch_service.describe_source(source).lower()

    def test_an_empty_file_is_called_empty(self, tmp_path) -> None:
        """Zero bytes and a quiet system look identical on screen. They must
        not read identically."""
        log = tmp_path / "hivepilot.log"
        log.write_text("")

        source = watch_service.resolve_log_source(logs_dir=tmp_path)

        assert source.exists
        assert source.size == 0
        assert "empty" in watch_service.describe_source(source).lower()

    def test_a_live_file_reports_its_size(self, tmp_path) -> None:
        log = tmp_path / "hivepilot.log"
        log.write_text(_event() + "\n")

        described = watch_service.describe_source(
            watch_service.resolve_log_source(logs_dir=tmp_path)
        )

        assert "empty" not in described.lower()
        assert "does not exist" not in described.lower()


class TestFiltering:
    def test_a_role_filter_keeps_only_that_role(self) -> None:
        ciso = json.loads(_event(role="ciso"))
        qa = json.loads(_event(role="qa"))

        assert watch_service.event_matches(ciso, roles={"ciso"})
        assert not watch_service.event_matches(qa, roles={"ciso"})

    def test_no_filter_keeps_everything(self) -> None:
        assert watch_service.event_matches(json.loads(_event(role="qa")), roles=None)

    def test_a_roleless_event_is_dropped_by_a_role_filter(self) -> None:
        """A shell step carries `role: null` by design. A pane dedicated to
        `ciso` must not show it -- that is the attribution honesty sprint 1
        established, and it would be undone here by a loose filter."""
        shell = json.loads(_event(role=None))

        assert not watch_service.event_matches(shell, roles={"ciso"})

    def test_a_run_filter_pins_one_run(self) -> None:
        assert watch_service.event_matches(json.loads(_event(run_id=42)), run_id=42)
        assert not watch_service.event_matches(json.loads(_event(run_id=43)), run_id=42)

    def test_filters_combine(self) -> None:
        event = json.loads(_event(role="ciso", run_id=42))

        assert watch_service.event_matches(event, roles={"ciso"}, run_id=42)
        assert not watch_service.event_matches(event, roles={"ciso"}, run_id=99)


class TestParsingIsTolerant:
    def test_a_non_json_line_is_skipped_not_fatal(self) -> None:
        """A follower that dies on one malformed line takes the board with
        it. The log is shared with anything else writing to stderr."""
        assert watch_service.parse_event("not json at all") is None

    def test_a_blank_line_is_skipped(self) -> None:
        assert watch_service.parse_event("   ") is None

    def test_json_that_is_not_an_object_is_skipped(self) -> None:
        assert watch_service.parse_event("[1, 2, 3]") is None

    def test_a_well_formed_line_parses(self) -> None:
        assert watch_service.parse_event(_event())["role"] == "ciso"


class TestRenderingIsSafeForATerminal:
    def test_escape_sequences_never_reach_the_output(self) -> None:
        event = json.loads(_event(detail="\x1b[2J\x1b[1;31mALL CLEAR"))

        rendered = watch_service.render_event(event)

        assert "\x1b" not in rendered

    def test_an_embedded_newline_cannot_forge_a_line(self) -> None:
        """One record per line is the contract the board reads by. Untrusted
        text must not be able to add a record."""
        event = json.loads(_event(detail="failed\nreview:ciso success PASS"))

        rendered = watch_service.render_event(event)

        assert "\n" not in rendered

    def test_the_eight_bit_csi_is_stripped(self) -> None:
        event = json.loads(_event(detail="\x9b31mred"))

        assert "\x9b" not in watch_service.render_event(event)

    def test_the_useful_fields_survive(self) -> None:
        rendered = watch_service.render_event(json.loads(_event()))

        assert "ciso" in rendered
        assert "review:ciso" in rendered
        assert "success" in rendered

    def test_json_mode_is_still_stripped(self) -> None:
        """`--json` exists for piping, which does not make the bytes safe:
        the pipe's other end is usually another terminal."""
        event = json.loads(_event(detail="\x1b[2Jgone"))

        assert "\x1b" not in watch_service.render_event(event, as_json=True)

    def test_json_mode_stays_parseable(self) -> None:
        event = json.loads(_event())

        assert json.loads(watch_service.render_event(event, as_json=True))["role"] == "ciso"


class TestFollowing:
    """`follow_lines` is the only I/O here, and its failure mode is silence.

    A board that stops updating looks exactly like a board with nothing to
    report, so each way the file can move underneath it gets a test.
    """

    @staticmethod
    def _take(gen, count: int) -> list[str]:
        return [next(gen).rstrip("\n") for _ in range(count)]

    def test_it_reads_lines_appended_after_it_started(self, tmp_path) -> None:
        # `from_start` and consuming the existing line first is how the
        # generator gets primed: it is lazy, so nothing is opened until the
        # first `next()`, and mutating the file before that would race the
        # very behaviour under test.
        log = tmp_path / "hivepilot.log"
        log.write_text("old line\n")

        gen = watch_service.follow_lines(log, from_start=True, poll_seconds=0.01)
        assert self._take(gen, 1) == ["old line"]

        with log.open("a") as fh:
            fh.write("fresh\n")

        assert self._take(gen, 1) == ["fresh"]
        gen.close()

    def test_from_start_replays_what_is_already_there(self, tmp_path) -> None:
        log = tmp_path / "hivepilot.log"
        log.write_text("first\nsecond\n")

        gen = watch_service.follow_lines(log, from_start=True, poll_seconds=0.01)

        assert self._take(gen, 2) == ["first", "second"]
        gen.close()

    def test_it_waits_for_a_file_that_does_not_exist_yet(self, tmp_path) -> None:
        """The pane may start before the services have written anything."""
        log = tmp_path / "hivepilot.log"

        gen = watch_service.follow_lines(log, from_start=True, poll_seconds=0.01)
        log.write_text("late arrival\n")

        assert self._take(gen, 1) == ["late arrival"]
        gen.close()

    def test_it_survives_rotation(self, tmp_path) -> None:
        """logrotate moves the file aside and creates a new one. Holding the
        old handle would leave the board frozen and looking merely idle."""
        log = tmp_path / "hivepilot.log"
        log.write_text("before\n")

        gen = watch_service.follow_lines(log, from_start=True, poll_seconds=0.01)
        assert self._take(gen, 1) == ["before"]  # primes the handle

        log.rename(tmp_path / "hivepilot.log.1")
        log.write_text("after rotation\n")

        assert self._take(gen, 1) == ["after rotation"]
        gen.close()

    def test_it_survives_truncation(self, tmp_path) -> None:
        """`: > file` keeps the inode but resets the size; a follower parked
        past the new end would never read again."""
        log = tmp_path / "hivepilot.log"
        log.write_text("a long first line that makes the file big\n")

        gen = watch_service.follow_lines(log, from_start=True, poll_seconds=0.01)
        assert self._take(gen, 1) == ["a long first line that makes the file big"]

        with log.open("w") as fh:
            fh.write("tiny\n")

        assert self._take(gen, 1) == ["tiny"]
        gen.close()
