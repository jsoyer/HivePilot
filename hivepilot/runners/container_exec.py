"""Run an argv inside a container and return what a subprocess would have.

The `workspace: container` primitive, and it was genuinely missing.
`ContainerRunner` cannot serve here: it demands an `image` AND a `command`,
builds `[runtime, run, …, image, bash, -lc, command]`, and calls
`subprocess.run(check=True)` with no capture. It runs a command of its OWN; it
cannot wrap somebody else's argv or hand back their output.

So this mirrors `run_in_pane` deliberately — same signature shape, same
`CompletedProcess` return — because both answer the same question from
different axes: `surface:` asks WHERE the work can be watched, `workspace:`
asks WHAT it can see. A runner's execution point should not need to know which
of the two is wrapping it.

What an AGENT in a container needs, and what the operator therefore owes:

  * the agent binary must be IN the image. Nothing here installs it, and
    nothing here could. A step whose image lacks `claude`/`grok` fails inside
    the container with a plain "not found" — the honest outcome.
  * its credentials arrive as environment variables, the same way
    `ContainerRunner` already passes them. They are visible to everything in
    that image: choosing the image IS choosing who sees the key.
  * the repository is mounted at the SAME path it holds on the host, so the
    agent's own cwd handling, its diffs and its writes need no translation.

Fail CLOSED on a missing image. Running on the HOST because none was declared
is the worst possible reading of `workspace: container`: the operator asked for
confinement and would silently get none.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - argv is assembled here from validated parts, never a shell string
from pathlib import Path
from typing import Any

# Imported rather than restated: two copies of a security list are two chances
# to update only one of them.
from hivepilot.runners.container_runner import (
    _BLOCKED_VOLUME_PREFIXES,
    _SUPPORTED_RUNTIMES,
    _validate_volume,
)
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = ["ContainerExecutionError", "run_in_container"]


class ContainerExecutionError(RuntimeError):
    """The step asked to be confined and could not be.

    Never raised for a failure INSIDE the container — that is an ordinary
    non-zero exit and comes back in the `CompletedProcess`, like any
    subprocess. This means the confinement itself could not be established,
    which is not a degraded run but a different one.
    """


def run_in_container(
    argv: list[str],
    *,
    image: str | None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    runtime: str = "docker",
    volumes: list[str] | None = None,
    engine_host: str | None = None,
    run_cli: Any | None = None,
) -> subprocess.CompletedProcess:
    """Run *argv* inside *image*, returning what `subprocess.run` would have.

    `run_cli` is injected so the build-and-dispatch sequence is testable
    without a container runtime; nothing else about the flow differs between
    the two. Same device as `run_in_pane`'s.
    """
    runner = run_cli if callable(run_cli) else subprocess.run

    if not image:
        raise ContainerExecutionError(
            "workspace: container requires an image, and none was declared. Set "
            "`options.image` on the runner definition. Refusing rather than "
            "running on the host: the step asked to be confined, and silently "
            "giving it no confinement is the one outcome nobody asked for."
        )
    if runtime not in _SUPPORTED_RUNTIMES:
        raise ContainerExecutionError(
            f"Unsupported container runtime {runtime!r}; expected one of {_SUPPORTED_RUNTIMES}."
        )
    if engine_host and (engine_host.startswith("-") or any(c.isspace() for c in engine_host)):
        raise ContainerExecutionError(f"Invalid container engine host: {engine_host!r}")

    work_dir = str(Path(cwd).resolve()) if cwd else None

    command: list[str] = [runtime]
    proc_env: dict[str, str] | None = None
    if engine_host:
        if runtime == "podman":
            command.extend(["--remote", "--url", engine_host])
        else:  # docker reaches another daemon through DOCKER_HOST, not a flag
            proc_env = {**os.environ, "DOCKER_HOST": engine_host}

    command.extend(["run", "--rm", "-i"])
    if work_dir:
        command.extend(["-w", work_dir])
        # Mounted at the SAME path it holds on the host. A different
        # in-container path would make every absolute path in a prompt wrong.
        _validate_volume(f"{work_dir}:{work_dir}")
        command.extend(["-v", f"{work_dir}:{work_dir}"])

    for volume in volumes or []:
        _validate_volume(volume)
        command.extend(["-v", volume])

    for key, value in (env or {}).items():
        command.extend(["-e", f"{key}={value}"])

    command.append(image)
    command.extend(argv)

    logger.info(
        "container_exec.start",
        runtime=runtime,
        image=image,
        # The HEAD only. The tail carries the prompt, which carries the task.
        argv0=argv[0] if argv else None,
        engine_host=engine_host,
        blocked_prefixes=len(_BLOCKED_VOLUME_PREFIXES),
    )
    return runner(
        command,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=proc_env,
        stdin=subprocess.DEVNULL,
    )
