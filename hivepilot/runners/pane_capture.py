"""Run an agent inside a terminal pane without losing what it printed.

An agent that runs as a bare child process is invisible: the operator cannot
watch it work, cannot see what it is stuck on, cannot type into it. Putting it
in a pane fixes that, and costs three things the engine depends on.

**Its output.** A pane's stdout belongs to the terminal, and `--print` mode's
JSON envelope -- the result text, the token counts, the cost -- goes with it. So
the envelope is redirected to a FILE and read back. Not scraped from the pane:
`pane read` returns a rendered terminal snapshot, line-wrapped, possibly
ANSI-laden and bounded by scrollback, and parsing JSON out of that would turn a
reliable capture into a guess that degrades as output grows.

**Its exit status.** `herdr pane run` reports whether the HERDR command
worked, not whether the agent did. So the status is written down too. Inferring
failure from the envelope instead would misread the one case that matters: a
crash that never produced an envelope would look like an empty success.

**Its environment.** The pane inherits the herdr SERVER's environment, not the
scrubbed, overlaid, sandboxed one the runner spent its whole invocation
building. Run the argv there as-is and the agent loses its API key, its OTel
destination, and every secret the step was granted.

This module only composes strings and writes one file. The wiring that spawns
the pane, waits and reads lives with the runner; keeping composition pure is
what lets the failure modes above be pinned by tests rather than by a live
server.
"""

from __future__ import annotations

import os
import re
import shlex
import uuid
from dataclasses import dataclass

#: Prefix of the end-of-command marker. Carries no shell metacharacters: it is
#: interpolated into a command line AND passed to `pane wait-output --match`.
SENTINEL_PREFIX = "HIVEPILOT_DONE_"

#: A POSIX shell name. Keys are NOT quoted when written -- `export` needs an
#: identifier, not a string -- so anything outside this shape is dropped rather
#: than written, because a key holding a newline injects a whole line that no
#: amount of quoting on the value side could contain.
_SHELL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class CapturePaths:
    """Where one step's streams and status land.

    Separate files, not one interleaved stream: stderr carries the CLI's own
    progress chatter, and a single byte of it inside the capture file makes the
    envelope unparseable -- which reads as "the agent returned nothing" rather
    than "we mixed two streams".
    """

    stdout: str
    stderr: str
    rc: str


def completion_sentinel() -> str:
    """A marker unique to this step.

    Unique per call on purpose: a second step running in the same pane would
    otherwise match the FIRST one's marker and be handed output that is not its
    own -- and, with it, another step's cost.
    """
    return f"{SENTINEL_PREFIX}{uuid.uuid4().hex}"


def capture_paths(base_dir: str) -> CapturePaths:
    """Three fresh paths under `base_dir`, unique per step.

    Reusing a name across steps would have the second step read the first
    one's envelope. The generated component is hex, so nothing here can be
    read as a shell metacharacter when interpolated below.
    """
    token = uuid.uuid4().hex
    base = base_dir.rstrip("/")
    return CapturePaths(
        stdout=f"{base}/{token}.stdout.json",
        stderr=f"{base}/{token}.stderr.log",
        rc=f"{base}/{token}.rc",
    )


def write_env_file(path: str, env: dict[str, str]) -> None:
    """Write `env` as sourceable `export` lines, readable only by us.

    Created 0600 *before* anything is written, not chmod'ed after: on a shared
    box the window between creation and chmod is enough to read a step's
    secrets.

    Values are `shlex.quote`d. Keys are validated instead, because a key is a
    shell identifier and cannot be quoted -- a key containing a newline would
    inject an entire statement the quoting of values does nothing about.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for key, value in env.items():
            if not _SHELL_NAME.match(key):
                continue
            handle.write(f"export {key}={shlex.quote(str(value))}\n")


def _with_environment(argv: list[str], env_file: str | None) -> str:
    """Quote `argv`, and give it exactly the environment we chose -- no more.

    `subprocess.run(env=...)` REPLACES the environment. Sourcing a file only
    adds to one, and the pane's starting environment is the herdr SERVER's. So
    a plain `. file; cmd` would hand the agent everything the server happens to
    carry ON TOP of what the runner decided -- including the variables
    `_apply_sandbox` deliberately took away. The sandbox would still be there,
    and would no longer be the thing deciding what the agent can see.

    `env -i` empties it first, so the file is the whole environment rather than
    an addition to somebody else's. `PATH` comes back from the file (the
    scrubbed env carries it), which is what lets `exec "$0"` still resolve a
    bare command name.

    The agent's argv is passed as positional parameters rather than
    interpolated into the `-c` script: it goes through one more layer of
    quoting than the direct path, and one level of nested escaping of a prompt
    full of quotes is where an injection would hide.
    """
    if not env_file:
        return shlex.join(argv)
    script = f'. {shlex.quote(env_file)}; exec "$0" "$@"'
    return f"env -i /bin/sh -c {shlex.quote(script)} {shlex.join(argv)}"


def build_pane_command(
    *,
    argv: list[str],
    paths: CapturePaths,
    sentinel: str,
    cwd: str | None = None,
    env_file: str | None = None,
) -> str:
    """Compose the one line the pane will run.

    `argv` is quoted, never concatenated: a prompt is agent- or
    operator-authored text and will contain quotes, semicolons and backticks;
    unquoted, the pane would execute them.

    Then three properties that each hide a wrong diagnosis:

    `;` and not `&&` before the echo -- a FAILING agent must still announce
    completion. Without that it hangs to the timeout and the operator is told
    "timed out" about something that died in a second.

    `$?` is captured into `__hp_rc` immediately, before anything else can
    clobber it, and re-raised at the end. `echo` succeeds; left last in the
    chain it would overwrite the agent's status and every failed step would
    report success.

    The sentinel is deliberately NOT redirected: it has to reach the terminal
    for `pane wait-output` to see it. Sent into a file, the wait would run to
    its timeout while the output it was waiting on sat complete on disk.

    And the environment REPLACES rather than overlays -- see `_with_environment`
    for why that distinction is the security-relevant one.
    """
    prefix = ""
    if cwd:
        # `state.db`, the plugin directory and `runs/` are all cwd-relative. A
        # step running from the server's directory reads a different database.
        # Set before the exec below, because a working directory survives one.
        prefix += f"cd {shlex.quote(cwd)}; "

    command = _with_environment(argv, env_file)
    out = shlex.quote(paths.stdout)
    err = shlex.quote(paths.stderr)
    rc = shlex.quote(paths.rc)
    return (
        f"{prefix}{command} > {out} 2> {err}; __hp_rc=$?; "
        f"echo $__hp_rc > {rc}; echo {sentinel}; (exit $__hp_rc)"
    )
