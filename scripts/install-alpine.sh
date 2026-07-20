#!/bin/sh
# Idempotent bare-metal installer for HivePilot on a fresh Alpine Linux host.
# POSIX sh only (Alpine ships busybox ash as /bin/sh, not bash) — safe to
# re-run at any time.
#
# Empirically verified on alpine:3.20 x86_64: all compiled deps (PyYAML,
# pydantic-core, charset-normalizer) resolve to musllinux wheels on PyPI and
# ruamel.yaml is pure-python, so NO build toolchain (gcc/rust/musl-dev) is
# required.
set -eu

VENV_DIR="${HIVEPILOT_VENV_DIR:-/opt/hivepilot/venv}"
REPO_URL="${HIVEPILOT_REPO_URL:-https://github.com/jsoyer/HivePilot.git}"
EXTRAS="${HIVEPILOT_EXTRAS:-api,notifications}"
# Pin the git install to a specific ref (branch, tag, or commit) for
# reproducible/supply-chain-safe installs instead of trusting whatever HEAD
# of the default branch happens to be at install time. Override with a
# release tag or a pinned commit sha for production, e.g.:
#   HIVEPILOT_REPO_REF=v0.2.0 sh scripts/install-alpine.sh
REPO_REF="${HIVEPILOT_REPO_REF:-main}"

echo "== HivePilot Alpine installer =="

# 1. Install proven OS-level dependencies. No compiler toolchain needed.
echo "-- Installing OS packages (python3, pip, git, curl, bash, ca-certificates)..."
apk add --no-cache python3 py3-pip git curl bash ca-certificates
echo "OK OS packages installed"

# 2. Create the venv if it doesn't already exist.
if [ ! -d "$VENV_DIR" ]; then
  echo "-- Creating virtualenv at ${VENV_DIR}..."
  mkdir -p "$(dirname "$VENV_DIR")"
  python3 -m venv "$VENV_DIR"
  echo "OK virtualenv created"
else
  echo "OK virtualenv already exists at ${VENV_DIR}"
fi

# 3. Install HivePilot + extras into the venv.
#
# HivePilot is NOT currently published on PyPI (verified: pypi.org/pypi/
# hivepilot/json returns 404), so `pip install hivepilot[...]` cannot
# resolve from the index yet. Support both install sources so this script
# keeps working once/if that changes:
#   - HIVEPILOT_SOURCE=git  (default) -> pip install from REPO_URL
#   - HIVEPILOT_SOURCE=pypi           -> pip install "hivepilot[$EXTRAS]"
#   - HIVEPILOT_SOURCE=local          -> pip install this checkout (editable)
#     when the script is run from inside a HivePilot working copy
#     (pyproject.toml present next to this script's parent directory)
SOURCE="${HIVEPILOT_SOURCE:-git}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"

echo "-- Installing hivepilot[$EXTRAS] (source: ${SOURCE})..."
"$VENV_DIR/bin/pip" install --upgrade pip

case "$SOURCE" in
  pypi)
    "$VENV_DIR/bin/pip" install --no-cache-dir "hivepilot[$EXTRAS]"
    ;;
  local)
    if [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
      echo "ERROR: HIVEPILOT_SOURCE=local but no pyproject.toml found at ${REPO_ROOT}" >&2
      exit 1
    fi
    "$VENV_DIR/bin/pip" install --no-cache-dir -e "${REPO_ROOT}[$EXTRAS]"
    ;;
  git)
    "$VENV_DIR/bin/pip" install --no-cache-dir "hivepilot[$EXTRAS] @ git+${REPO_URL}@${REPO_REF}"
    ;;
  *)
    echo "ERROR: unknown HIVEPILOT_SOURCE '${SOURCE}' (expected git|pypi|local)" >&2
    exit 1
    ;;
esac
echo "OK hivepilot[$EXTRAS] installed"

# 4. Symlink the CLI onto PATH for convenience (idempotent).
BIN_LINK="/usr/local/bin/hivepilot"
if [ -x "$VENV_DIR/bin/hivepilot" ]; then
  ln -sf "$VENV_DIR/bin/hivepilot" "$BIN_LINK" 2>/dev/null || {
    echo "NOTE: could not symlink ${BIN_LINK} (no permission?) — run with:"
    echo "      ${VENV_DIR}/bin/hivepilot ..."
  }
fi

echo ""
echo "== Install complete =="
echo "Next steps:"
echo "  1. export HIVEPILOT_CONFIG_REPO=<your config repo URL>"
echo "     (private https repo? also: export HIVEPILOT_CONFIG_TOKEN=<pat>)"
echo "  2. hivepilot config sync       # pull projects/tasks/roles/pipelines config"
echo "  3. hivepilot validate          # sanity-check the synced config"
echo "  4. hivepilot doctor            # verify the installation + external tools"
echo "  5. hivepilot agents install claude   # install the agent CLI(s) you plan to use"
echo ""
echo "If ${BIN_LINK} was not created, prefix the commands above with:"
echo "  ${VENV_DIR}/bin/"
