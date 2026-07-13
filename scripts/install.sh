#!/usr/bin/env bash
# Idempotent bootstrap script for a fresh HivePilot checkout.
# Safe to re-run at any time.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR=".venv"
MIN_MAJOR=3
MIN_MINOR=10

echo "== HivePilot bootstrap =="

# 1. Verify python3 >= 3.10 is available.
if ! command -v python3 >/dev/null 2>&1; then
  echo "⚠ python3 not found on PATH. Install Python ${MIN_MAJOR}.${MIN_MINOR}+ and re-run this script."
  exit 1
fi

PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_MAJOR="$(echo "$PY_VERSION" | cut -d. -f1)"
PY_MINOR="$(echo "$PY_VERSION" | cut -d. -f2)"

if [ "$PY_MAJOR" -lt "$MIN_MAJOR" ] || { [ "$PY_MAJOR" -eq "$MIN_MAJOR" ] && [ "$PY_MINOR" -lt "$MIN_MINOR" ]; }; then
  echo "⚠ Found python3 ${PY_VERSION}, but HivePilot requires >= ${MIN_MAJOR}.${MIN_MINOR}."
  exit 1
fi
echo "✓ python3 ${PY_VERSION} found"

# 2. Create the virtualenv if it doesn't already exist.
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtualenv at ${VENV_DIR}..."
  python3 -m venv "$VENV_DIR"
  echo "✓ virtualenv created"
else
  echo "✓ virtualenv already exists at ${VENV_DIR}"
fi

# 3. Install/upgrade pip and install the package with dev + notifications extras.
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip
pip install -e ".[dev,notifications]"
echo "✓ dependencies installed (dev,notifications extras)"

# 4. Seed a local .env from .env.example if one doesn't exist yet.
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp .env.example .env
  echo "✓ created .env from .env.example — please edit it with your real values"
elif [ -f ".env" ]; then
  echo "✓ .env already exists, leaving it untouched"
else
  echo "⚠ no .env.example found, skipping .env creation"
fi

# 5. Next steps.
echo ""
echo "== Bootstrap complete =="
echo "Next steps:"
echo "  1. Activate the virtualenv:  source ${VENV_DIR}/bin/activate"
echo "  2. Edit .env with your real secrets/config"
echo "  3. Verify your setup:        hivepilot doctor"
