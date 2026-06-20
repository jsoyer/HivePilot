"""Remote agent execution over SSH.

An agent's CLI can run on another machine (e.g. the CTO on host B, the developer
on host C). We wrap the locally-built argv in an ``ssh`` invocation that ``cd``s
into the repo on the remote host and runs the same command there.

Auth strategy: rely on the operator's ``~/.ssh/config`` + keys/agent — nothing
secret is stored by HivePilot. ``BatchMode=yes`` means a missing/!configured key
fails fast instead of prompting. The remote host must have the agent CLI
installed + authenticated and the repo checked out at the same path.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path


def ssh_wrap(
    args: Sequence[str],
    cwd: Path,
    env: dict[str, str] | None,
    *,
    host: str,
    ssh_options: Sequence[str] | None = None,
) -> list[str]:
    """Wrap *args* so they run on *host* via SSH, inside *cwd*, with *env* applied.

    *env* is forwarded as inline assignments in the remote command (task/project
    env only — secrets are NOT forwarded; the remote CLI uses its own local auth).
    """
    assigns = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (env or {}).items())
    cli = shlex.join(list(args))
    remote_cmd = f"cd {shlex.quote(str(cwd))} && " + (f"{assigns} " if assigns else "") + cli
    ssh: list[str] = ["ssh", "-o", "BatchMode=yes"]
    for opt in ssh_options or []:
        ssh += ["-o", opt]
    ssh += [host, remote_cmd]
    return ssh


def build_invocation(
    args: Sequence[str],
    project_path: Path,
    env: dict[str, str] | None,
    *,
    host: str | None = None,
    ssh_options: Sequence[str] | None = None,
) -> tuple[list[str], str | None, dict[str, str] | None]:
    """Return ``(argv, cwd, env)`` for ``subprocess.run``.

    Local (no host): the args run in *project_path* with *env*.
    Remote (host set): the args are SSH-wrapped and run locally as an ``ssh``
    process — ``cwd``/``env`` are ``None`` so ssh uses the operator's ambient
    environment (keys/agent); the remote cwd + env live inside the wrapped command.
    """
    if not host:
        return list(args), str(project_path), env
    return ssh_wrap(args, project_path, env, host=host, ssh_options=ssh_options), None, None
