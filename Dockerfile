# syntax=docker/dockerfile:1
#
# Production image — Alpine Linux (musl libc). Empirically verified on
# alpine:3.20 x86_64: PyYAML, pydantic-core, and charset-normalizer all
# resolve to musllinux wheels on PyPI, and ruamel.yaml is pure-python, so
# NO compiler toolchain (gcc/rust/musl-dev) is required at all. Keep it
# that way — if a future dependency forces a source build, prefer finding
# a wheel-friendly alternative over reintroducing build tools here.
#
# Multi-stage: the builder stage installs into a venv; the final stage
# copies only that venv + the app, keeping the runtime image slim and
# free of pip/setuptools caches.

FROM python:3.12-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# git is needed here only if pip ever needs to fetch a VCS dependency;
# it is cheap and keeps this stage self-sufficient.
RUN apk add --no-cache git

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY . .

# Runtime image: core package + API server + notifications (telegram) extras.
# Extend/swap the extras list as needed, e.g. `.[full]` for every integration.
RUN pip install --no-cache-dir ".[api,notifications]"


FROM python:3.12-alpine AS final

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    HIVEPILOT_BASE_DIR=/data

# Runtime OS deps only (no compiler toolchain needed — see builder stage note).
# `github-cli` is NOT reliably available across Alpine releases/architectures
# in the community repo, so it is intentionally NOT installed here to avoid a
# hard build failure on hosts where the package is missing. `gh` is optional:
# HivePilot degrades gracefully when it is absent (see `hivepilot doctor`);
# install it later via the distro's package manager or see
# https://github.com/cli/cli/blob/trunk/docs/install_linux.md, or run
# `hivepilot agents install claude` for the agent CLIs HivePilot itself needs.
RUN apk add --no-cache git curl bash ca-certificates \
    && addgroup -S hivepilot \
    && adduser -S -G hivepilot -h /home/hivepilot hivepilot \
    && mkdir -p /app /data \
    && chown -R hivepilot:hivepilot /app /data

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=hivepilot:hivepilot . .

# Config files (projects.yaml, tasks.yaml, roles.yaml, ...) and state.db live
# under /data so they survive container recreation via a mounted volume.
# `hivepilot config sync` and the app's XDG config resolution both honor
# HIVEPILOT_BASE_DIR, so pointing it at /data is sufficient — no extra
# symlinking required.
VOLUME ["/data"]

USER hivepilot

EXPOSE 8045

# `hivepilot doctor` exits non-zero on hard failures (missing base dir,
# missing external binaries flagged as mandatory), making it a reasonable
# process-level healthcheck without requiring the API server to be the
# process running in this particular container (scheduler daemon included).
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD hivepilot doctor >/dev/null 2>&1 || exit 1

# Default to showing help — this image is shared by the API and scheduler
# services (see docker-compose.yml), which override `command:` explicitly:
#   API:       hivepilot api serve --host 0.0.0.0 --port 8045
#   scheduler: hivepilot schedule daemon --interval 30
ENTRYPOINT ["hivepilot"]
CMD ["--help"]
