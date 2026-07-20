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

# --- Seed the "packaged copy" of built-in agent prompts ---------------------
# `hivepilot/roles.py::_PROMPTS_DIR` and
# `hivepilot/services/auditor_service.py::AUDITOR_PROMPT` resolve the
# built-in prompt templates via `Path(__file__).parent.parent / "prompts"`
# (a package-relative sibling lookup — NOT declared in pyproject.toml's
# [tool.setuptools.package-data], which only ships hivepilot/webui/static).
# That lookup is the documented FINAL fallback in the role/prompt resolution
# chain (config_repo / base_dir take priority when present), so a plain
# `pip install .` into a venv needs `prompts/` copied next to the installed
# `hivepilot` package for that fallback to actually resolve anything. Compute
# the destination dynamically (not hardcoded to python3.12's site-packages
# path) so this keeps working across Python version bumps, and copy ONLY the
# tiny prompts/ directory — not the whole repository.
# Run the `import hivepilot` probe from outside /build: cwd is prepended to
# sys.path, so running it from /build (which itself contains a `hivepilot/`
# source subdirectory) would shadow the installed site-packages copy and
# resolve `hivepilot.__file__` to the source tree instead — computing the
# wrong destination (and looping `cp -r` into itself).
RUN PKG_PARENT="$(cd / && python3 -c 'import hivepilot, os; print(os.path.dirname(os.path.dirname(hivepilot.__file__)))')" \
    && cp -r /build/prompts "${PKG_PARENT}/prompts"


FROM python:3.12-alpine AS final

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    HIVEPILOT_BASE_DIR=/data

# Runtime OS deps only (no compiler toolchain needed — see builder stage note).
RUN apk add --no-cache git curl bash ca-certificates \
    && addgroup -S hivepilot \
    && adduser -S -G hivepilot -h /home/hivepilot hivepilot \
    && mkdir -p /data \
    && chown -R hivepilot:hivepilot /data

# `github-cli` (gh) — used by the github_pr merge gate and gh-based agent/plugin
# installers. It lives in Alpine's *community* repo, which IS enabled on the
# official python:3.12-alpine base, so this normally succeeds. It is installed
# BEST-EFFORT (`|| echo`, its own RUN layer) rather than chained into the hard
# apk line above, because the package is not guaranteed across every Alpine
# release/architecture — a missing package must NOT fail the image build. `gh`
# is optional: HivePilot degrades gracefully without it (see `hivepilot doctor`).
# Opt out by setting the build arg to 0: `--build-arg WITH_GH=0`.
ARG WITH_GH=1
RUN if [ "$WITH_GH" = "1" ]; then \
        apk add --no-cache github-cli 2>/dev/null \
            && echo "OK github-cli installed" \
            || echo "NOTE: github-cli unavailable for this Alpine release/arch — continuing without it (optional)"; \
    fi

# The venv (built + installed in the builder stage, including the seeded
# prompts/ "packaged copy" above) is self-contained — the application is
# installed into it, so the source tree is deliberately NOT copied into this
# final stage. This keeps the image slim and makes .dockerignore a
# defense-in-depth measure rather than the only leak control: nothing from
# the build context reaches this stage at all except via the venv install.
COPY --from=builder /opt/venv /opt/venv
WORKDIR /data

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
