"""Running an argv inside a container, as a drop-in for `subprocess.run`.

The `workspace: container` primitive, and it was genuinely missing —
`grep run_in_container` returned nothing. `ContainerRunner` cannot serve here:
it demands an `image` AND a `command`, builds `[runtime, run, …, image, bash,
-lc, command]`, and calls `subprocess.run(check=True)` with no capture. It runs
a command of its OWN; it cannot wrap somebody else's argv or return their
output.

So this mirrors `run_in_pane`: same signature shape, same `CompletedProcess`
return. Both answer the same question from different axes — `surface:` asks
where the work can be WATCHED, `workspace:` asks what it can SEE — and a
runner's execution point should not have to know which is wrapping it.

The load-bearing test here is `TestItFailsClosed`. Running on the host because
no image was declared is the worst possible reading of `workspace: container`:
the operator asked for confinement and would silently get none.
"""

from __future__ import annotations

import subprocess

import pytest


def _fake_run(recorder: list):
    def _run(command, **kwargs):
        recorder.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    return _run


class TestItFailsClosed:
    """The half that matters. A missing image must REFUSE, never fall back to
    the host — the step asked to be confined."""

    def test_no_image_refuses(self):
        from hivepilot.runners.container_exec import ContainerExecutionError, run_in_container

        with pytest.raises(ContainerExecutionError, match="requires an image"):
            run_in_container(["claude", "--print"], image=None)

    def test_an_empty_image_refuses_too(self):
        from hivepilot.runners.container_exec import ContainerExecutionError, run_in_container

        with pytest.raises(ContainerExecutionError):
            run_in_container(["claude"], image="")

    def test_it_never_reaches_the_runtime_when_refusing(self):
        """Refusing after spawning something would not be refusing."""
        from hivepilot.runners.container_exec import ContainerExecutionError, run_in_container

        calls: list = []
        with pytest.raises(ContainerExecutionError):
            run_in_container(["claude"], image=None, run_cli=_fake_run(calls))

        assert calls == []

    def test_an_unsupported_runtime_refuses(self):
        from hivepilot.runners.container_exec import ContainerExecutionError, run_in_container

        with pytest.raises(ContainerExecutionError, match="runtime"):
            run_in_container(["x"], image="python:3.11", runtime="chroot")

    def test_a_hostile_engine_host_refuses(self):
        from hivepilot.runners.container_exec import ContainerExecutionError, run_in_container

        with pytest.raises(ContainerExecutionError):
            run_in_container(["x"], image="i", engine_host="--privileged evil")


class TestTheArgvIsWrappedNotRewritten:
    def test_the_agent_argv_survives_verbatim_at_the_tail(self):
        """The whole point of a wrapper. Any reordering here would corrupt the
        prompt, which is the last element."""
        from hivepilot.runners.container_exec import run_in_container

        calls: list = []
        argv = ["claude", "--print", "--model", "opus", "do the thing"]
        run_in_container(argv, image="agent:1", run_cli=_fake_run(calls))

        command = calls[0][0]
        assert command[-len(argv) :] == argv

    def test_the_image_comes_immediately_before_the_argv(self):
        from hivepilot.runners.container_exec import run_in_container

        calls: list = []
        run_in_container(["grok", "-p", "hi"], image="agent:1", run_cli=_fake_run(calls))

        command = calls[0][0]
        assert command[command.index("agent:1") + 1 :] == ["grok", "-p", "hi"]

    def test_it_returns_a_completed_process_like_subprocess_run(self):
        """A drop-in, so a runner's exec point does not branch on which
        wrapper it got."""
        from hivepilot.runners.container_exec import run_in_container

        result = run_in_container(["x"], image="i", run_cli=_fake_run([]))

        assert isinstance(result, subprocess.CompletedProcess)
        assert result.stdout == "ok"

    def test_it_captures_rather_than_inheriting(self):
        from hivepilot.runners.container_exec import run_in_container

        calls: list = []
        run_in_container(["x"], image="i", run_cli=_fake_run(calls))

        kwargs = calls[0][1]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False, "a non-zero exit is the caller's to read, not to raise"


class TestTheRepoIsMountedAtItsOwnPath:
    def test_cwd_is_mounted_onto_itself(self, tmp_path):
        """Same path inside and out, so the agent's own cwd handling, its
        diffs and its writes need no translation. A different in-container
        path makes every absolute path in a prompt wrong."""
        from hivepilot.runners.container_exec import run_in_container

        calls: list = []
        run_in_container(["x"], image="i", cwd=str(tmp_path), run_cli=_fake_run(calls))

        command = calls[0][0]
        assert "-v" in command
        assert f"{tmp_path}:{tmp_path}" in command
        assert command[command.index("-w") + 1] == str(tmp_path)

    def test_no_cwd_mounts_nothing_of_its_own(self):
        from hivepilot.runners.container_exec import run_in_container

        calls: list = []
        run_in_container(["x"], image="i", run_cli=_fake_run(calls))

        assert "-w" not in calls[0][0]

    def test_extra_volumes_are_validated_not_trusted(self):
        """Reuses `container_runner`'s blocked-prefix list rather than
        restating it — two copies of a security list are two chances to update
        only one."""
        from hivepilot.runners.container_exec import run_in_container

        with pytest.raises(ValueError):
            run_in_container(["x"], image="i", volumes=["/etc:/etc"], run_cli=_fake_run([]))

    def test_it_uses_the_same_blocked_list_as_the_runner(self):
        import inspect

        from hivepilot.runners import container_exec

        src = inspect.getsource(container_exec)

        assert "_BLOCKED_VOLUME_PREFIXES" in src
        assert "from hivepilot.runners.container_runner import" in src


class TestEnvironmentReachesTheContainer:
    def test_env_vars_are_passed_with_e(self):
        """This is how an agent's credentials get in — the same way
        `ContainerRunner` already does it. Choosing the image IS choosing who
        sees the key."""
        from hivepilot.runners.container_exec import run_in_container

        calls: list = []
        run_in_container(["x"], image="i", env={"K": "v"}, run_cli=_fake_run(calls))

        command = calls[0][0]
        assert command[command.index("-e") + 1] == "K=v"

    def test_a_remote_podman_uses_remote_url(self):
        from hivepilot.runners.container_exec import run_in_container

        calls: list = []
        run_in_container(
            ["x"], image="i", runtime="podman", engine_host="ssh://h", run_cli=_fake_run(calls)
        )

        command = calls[0][0]
        assert command[1:4] == ["--remote", "--url", "ssh://h"]

    def test_a_remote_docker_uses_the_env_var_not_a_flag(self):
        """docker has no `--remote`; it reaches another daemon through
        DOCKER_HOST. Passing a flag it does not know would fail obscurely."""
        from hivepilot.runners.container_exec import run_in_container

        calls: list = []
        run_in_container(
            ["x"], image="i", runtime="docker", engine_host="ssh://h", run_cli=_fake_run(calls)
        )

        command, kwargs = calls[0]
        assert "--remote" not in command
        assert kwargs["env"]["DOCKER_HOST"] == "ssh://h"


class TestItDoesNotLeakTheTask:
    def test_the_prompt_is_never_logged(self):
        """The argv TAIL carries the prompt, which carries the task. Only the
        head is logged."""
        import inspect

        from hivepilot.runners import container_exec

        src = inspect.getsource(container_exec)

        assert "argv0=argv[0]" in src
        assert "argv=argv" not in src
