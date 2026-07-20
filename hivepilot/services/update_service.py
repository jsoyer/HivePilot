"""`hivepilot self-update` helpers: venv-targeted git+https pip install.

HivePilot is NOT published on PyPI -- it installs from git. This module keeps
the pip-spec construction and the two subprocess invocations (`pip install`,
best-effort service restart) as small, pure-ish, independently testable
functions, so `hivepilot.cli.self_update` stays a thin wrapper.

Venv targeting: `run_self_update` defaults `python` to `sys.executable` -- the
interpreter behind the currently-running `hivepilot` console script, i.e.
THIS install's venv python, not the system Python. Running
`<venv-python> -m pip install ...` always resolves pip inside that venv, so
it can never trip PEP 668's "externally-managed-environment" guard and never
needs (and must never pass) `--break-system-packages`.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 - only ever invoked with argv lists built from vetted

# constants (KNOWN_SERVICES) or config/CLI-flag strings passed straight through
# to pip/rc-service/systemctl -- never shell=True, never a shell-interpreted string.
import sys
from collections.abc import Iterable

KNOWN_SERVICES: list[str] = [
    "hivepilot-api",
    "hivepilot-scheduler",
    "hivepilot-telegram",
]

# Matches the userinfo (`user:secret@` or `user@`) segment of a URL, e.g. in
# `https://x-access-token:TOKEN@github.com/...` -- used by
# `mask_url_credentials` to redact any credential an operator embedded in
# `--repo`/`HIVEPILOT_UPDATE_REPO` before it is ever echoed to the terminal.
_URL_CREDENTIALS_RE = re.compile(r"(\w+://)[^/@\s]+@")


def mask_url_credentials(text: str) -> str:
    """Redact `scheme://user:secret@` -> `scheme://***@` anywhere in `text`.

    An operator may point `--repo`/`HIVEPILOT_UPDATE_REPO` at a private fork
    using an embedded-credential URL (e.g.
    `https://x-access-token:TOKEN@github.com/org/repo.git`). That token would
    otherwise appear in clear text both in the "Will install: <spec>" echo
    and in pip's own stdout/stderr (pip logs the exact clone URL, including
    credentials, when it resolves a `git+` requirement). Apply this to every
    string that may embed the repo URL before it is echoed. Idempotent and a
    no-op on URLs with no embedded credentials.
    """
    return _URL_CREDENTIALS_RE.sub(r"\1***@", text)


def build_update_spec(repo: str, ref: str, extras: str) -> str:
    """Build the pip install spec for a git-sourced HivePilot install.

    `build_update_spec("https://github.com/jsoyer/HivePilot.git", "v1.2.3",
    "api,notifications")` -> `"hivepilot[api,notifications] @
    git+https://github.com/jsoyer/HivePilot.git@v1.2.3"`.
    """
    return f"hivepilot[{extras}] @ git+{repo}@{ref}"


def run_self_update(spec: str, *, python: str | None = None) -> subprocess.CompletedProcess[str]:
    """Force-reinstall hivepilot from `spec`, then resolve any new deps.

    `python` defaults to `sys.executable` -- see module docstring for why
    that always targets the correct venv. Never passes
    `--break-system-packages`: the venv-targeted pip never needs it, and
    passing it would silently allow writes outside the venv if this were
    ever invoked with a system interpreter by mistake.

    Two pip invocations, not one -- this is the actual fix for a
    self-update-is-a-silent-no-op bug: `pip install -U <spec>` alone will
    NOT reinstall when `HIVEPILOT_UPDATE_REF` (`main` by default) is a
    moving branch whose HEAD commit changed but whose `pyproject.toml`
    `version` string did NOT (e.g. still `0.2.0`). pip resolves the new
    commit, sees the same version already satisfied, and reports
    "Requirement already satisfied" without touching the install --
    `self-update` runs "successfully" and changes nothing.

    1. `pip install --force-reinstall --no-deps --no-cache-dir <spec>` --
       unconditionally re-clones `ref` and reinstalls the hivepilot package
       itself, regardless of the resolved version. `--no-deps` keeps this
       step fast (it never re-downloads/reinstalls dependencies) and is
       what makes the force-reinstall safe to run on every `self-update`
       rather than something an operator has to opt into.
    2. `pip install --no-cache-dir <spec>` (no `-U`, no `--force-reinstall`)
       -- a plain resolve that installs any dependency a new version may
       have newly added. A cheap no-op when nothing changed (everything is
       already satisfied); does not touch hivepilot itself or already
       satisfied deps, so it can't undo step 1 or re-download the world.

    If step 1 fails, its result is returned immediately and step 2 is never
    run -- there is no point resolving deps for a package whose own
    reinstall just failed, and doing so would risk masking the real error.
    On success, the two steps' stdout/stderr are concatenated (in order)
    into a single `CompletedProcess` so callers -- which mask credentials
    before echoing -- only need to handle one result, and see the combined
    output of both invocations.
    """
    py = python or sys.executable
    # nosec B603 - argv list, no shell=True; `py` defaults to sys.executable
    # and `spec` is built by build_update_spec from config/CLI-flag strings,
    # never raw untrusted input.
    reinstall = subprocess.run(
        [py, "-m", "pip", "install", "--force-reinstall", "--no-deps", "--no-cache-dir", spec],
        check=False,
        capture_output=True,
        text=True,
    )
    if reinstall.returncode != 0:
        return reinstall

    deps = subprocess.run(  # nosec B603 - see above
        [py, "-m", "pip", "install", "--no-cache-dir", spec],
        check=False,
        capture_output=True,
        text=True,
    )
    return subprocess.CompletedProcess(
        args=deps.args,
        returncode=deps.returncode,
        stdout=reinstall.stdout + deps.stdout,
        stderr=reinstall.stderr + deps.stderr,
    )


def restart_services(names: Iterable[str]) -> list[str]:
    """Best-effort restart of `names` under the detected init system.

    Prefers `rc-service` (OpenRC/Alpine) when it is on PATH, else falls back
    to `systemctl` when that is on PATH. Returns the subset of `names` that
    were actually restarted (subprocess returncode 0) -- a service name that
    doesn't exist under the detected init system is silently skipped, never
    raised, so one absent service never fails the whole `self-update`
    command. Returns `[]` immediately, without invoking anything, when
    neither `rc-service` nor `systemctl` is present on PATH.
    """
    names = list(names)

    if shutil.which("rc-service"):

        def _cmd(name: str) -> list[str]:
            return ["rc-service", name, "restart"]

    elif shutil.which("systemctl"):

        def _cmd(name: str) -> list[str]:
            return ["systemctl", "restart", name]

    else:
        return []

    restarted: list[str] = []
    for name in names:
        result = subprocess.run(  # nosec B603 - argv list, no shell=True; command
            # prefix (rc-service/systemctl) is chosen from a fixed set above and
            # `name` only ever comes from KNOWN_SERVICES, a maintainer-vetted
            # in-repo constant -- never dynamic/user input.
            _cmd(name),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            restarted.append(name)
    return restarted
