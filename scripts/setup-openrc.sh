#!/bin/sh
# Idempotent installer for HivePilot as OpenRC services on Alpine Linux.
# POSIX sh only (Alpine ships busybox ash as /bin/sh, not bash) -- safe to
# re-run any time (e.g. to rotate RUN_TOKEN) since it always overwrites the
# generated files and restarts the affected services.
#
# Installs up to 3 services:
#   hivepilot-api        HTTP API server                       (mandatory)
#   hivepilot-scheduler  scheduler daemon                       (mandatory)
#   hivepilot-telegram   Telegram bot (polling)                 (optional --
#                        entirely skipped, not written, not enabled, unless
#                        a TELEGRAM_BOT_TOKEN is provided)
#
# PREREQUISITES:
#   - HivePilot already installed into a venv (see scripts/install-alpine.sh)
#   - Your config repo is reachable (HIVEPILOT_CONFIG_REPO + auth if private
#     -- see docs/DEPLOY-PRODUCTION.md#3-wire-the-config)
#   - A `run`-role API token already bootstrapped (`hivepilot tokens add
#     --role run ...`) -- the scheduler daemon is fail-closed without one,
#     see docs/DEPLOY-PRODUCTION.md#7-api-tokens-bootstrap
#
# USAGE (interactive -- prompts for anything not already in the environment):
#   sh scripts/setup-openrc.sh
#
# USAGE (non-interactive -- every prompt is skipped when its var is already
# set, so this runs unattended in CI/automation too):
#   RUN_TOKEN=<run-role-token> \
#   HIVEPILOT_CONFIG_REPO=<your-config-repo> \
#   ANTHROPIC_API_KEY=<key> \
#     sh scripts/setup-openrc.sh
#
# ENV OVERRIDES:
#   HIVEPILOT_VENV_DIR    venv the `hivepilot` CLI lives in (default /opt/hivepilot/venv)
#   HIVEPILOT_CONFIG_REPO config repo URL (also pre-seeds/skips its prompt)
#   HIVEPILOT_INITD_DIR   where to write openrc-run scripts (default /etc/init.d)
#   HIVEPILOT_CONFD_DIR   where to write conf.d env files (default /etc/conf.d)
#   HIVEPILOT_LOG_DIR     where services log to (default /var/log/hivepilot)
#   RUN_TOKEN              run-role API token for the scheduler (REQUIRED --
#                          the scheduler daemon is fail-closed without one)
#   ANTHROPIC_API_KEY       optional; exported to all installed services if set
#   TELEGRAM_BOT_TOKEN      optional; the hivepilot-telegram service is
#                           skipped entirely (not written, not enabled) if unset
#   TELEGRAM_CHAT_IDS       optional, comma-separated Telegram chat IDs (only
#                           used/prompted if TELEGRAM_BOT_TOKEN is set); the
#                           first ID becomes the proactive-notification chat
#
# The last two env overrides (HIVEPILOT_INITD_DIR/HIVEPILOT_CONFD_DIR) exist
# primarily so this script's file-generation logic is exercisable by an
# unprivileged user / CI (see tests/test_setup_openrc.py) -- pointing them at
# a scratch directory also relaxes the root requirement below, since only
# writing to the REAL /etc and /var/log system paths needs root.
#
# Assumes it is run as root with HOME=/root -- the generated services run as
# root (no command_user override) so the daemon's PATH/HOME line up with a
# typical bare-metal Alpine bootstrap (this intentionally differs from the
# non-root hivepilot:hivepilot walkthrough in docs/DEPLOY-PRODUCTION.md's
# manual OpenRC example; adjust command_user in the generated /etc/init.d
# files yourself if you want to run as a dedicated unprivileged user).
set -eu

VENV="${HIVEPILOT_VENV_DIR:-/opt/hivepilot/venv}"
HP="$VENV/bin/hivepilot"
INITD_DIR="${HIVEPILOT_INITD_DIR:-/etc/init.d}"
CONFD_DIR="${HIVEPILOT_CONFD_DIR:-/etc/conf.d}"
LOG_DIR="${HIVEPILOT_LOG_DIR:-/var/log/hivepilot}"
CONFIG_REPO_DEFAULT="${HIVEPILOT_CONFIG_REPO:-git@github.com:noxys-eu/noxys-hivepilot-config.git}"

echo "== HivePilot OpenRC installer =="

# ---- Preconditions ---------------------------------------------------------

# Writing to the real /etc and /var/log system paths requires root. When the
# output dirs have been explicitly overridden away from those defaults
# (tests, a scratch-dir dry run) this requirement is relaxed so the
# file-generation logic stays exercisable by an unprivileged user/CI.
if [ "$INITD_DIR" = "/etc/init.d" ] && [ "$CONFD_DIR" = "/etc/conf.d" ]; then
  if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root (this writes to ${INITD_DIR} and ${CONFD_DIR})." >&2
    exit 1
  fi
fi

if [ ! -x "$HP" ]; then
  echo "ERROR: hivepilot CLI not found/executable at ${HP}." >&2
  echo "       Install HivePilot first (see scripts/install-alpine.sh), or" >&2
  echo "       set HIVEPILOT_VENV_DIR to point at an existing install." >&2
  exit 1
fi

if ! command -v rc-update >/dev/null 2>&1; then
  echo "ERROR: rc-update not found -- this script targets Alpine/OpenRC hosts only." >&2
  exit 1
fi

echo "OK preconditions (root/output dirs, hivepilot CLI, rc-update)"

# ---- Prompts ----------------------------------------------------------------
# Every value is env-overridable: a prompt is skipped entirely when its var
# is already set (non-empty) in the environment, so the whole script also
# runs non-interactively. A closed/empty stdin (no TTY -- e.g. under a test
# harness) makes `read` return immediately with an empty value rather than
# hang, which is treated the same as "left blank".

prompt_value() {
  # $1=var name to set  $2=prompt text  $3=default (may be empty)
  eval "_pv_current=\${$1:-}"
  if [ -n "$_pv_current" ]; then
    return 0
  fi
  if [ -n "$3" ]; then
    printf '%s [%s]: ' "$2" "$3" >&2
  else
    printf '%s: ' "$2" >&2
  fi
  _pv_input=""
  read -r _pv_input || _pv_input=""
  if [ -z "$_pv_input" ]; then
    _pv_input="$3"
  fi
  eval "$1=\$_pv_input"
}

prompt_secret() {
  # $1=var name to set  $2=prompt text  $3="1" if required else "0"
  eval "_ps_current=\${$1:-}"
  if [ -n "$_ps_current" ]; then
    return 0
  fi
  printf '%s: ' "$2" >&2
  stty -echo 2>/dev/null || true
  _ps_input=""
  read -r _ps_input || _ps_input=""
  stty echo 2>/dev/null || true
  printf '\n' >&2
  if [ -z "$_ps_input" ] && [ "$3" = "1" ]; then
    echo "ERROR: ${1} is required (the scheduler is fail-closed without it)." >&2
    exit 1
  fi
  eval "$1=\$_ps_input"
}

prompt_value HIVEPILOT_CONFIG_REPO "Config repo URL" "$CONFIG_REPO_DEFAULT"
prompt_secret RUN_TOKEN "Run-role API token (RUN_TOKEN, required)" 1
prompt_secret ANTHROPIC_API_KEY "Anthropic API key (optional, blank to skip)" 0
prompt_secret TELEGRAM_BOT_TOKEN "Telegram bot token (optional, blank to skip)" 0

TELEGRAM_ENABLED=0
TELEGRAM_CHAT_IDS_JSON=""
TELEGRAM_FIRST_CHAT_ID=""
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  TELEGRAM_ENABLED=1
  prompt_value TELEGRAM_CHAT_IDS "Telegram chat IDs (comma-separated)" ""
  _tg_ids_trimmed=$(printf '%s' "${TELEGRAM_CHAT_IDS:-}" | tr -d ' \t')
  # HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS maps a pydantic-settings `list[int]`
  # field, which parses its env value as STRICT JSON -- a bare/CSV value
  # (e.g. "123456" or "123,456", not wrapped in brackets) makes `Settings()`
  # raise at import time and takes the whole `hivepilot` process down. Only
  # emit a real JSON array ("[123456,789012]") when chat ids were actually
  # given; leave both this and the derived "first id" (notification chat)
  # empty -- and therefore the export lines omitted entirely below -- when
  # blank, rather than writing an explicit "[]" (open whitelist is already
  # HivePilot's own default for an unset value).
  if [ -n "$_tg_ids_trimmed" ]; then
    TELEGRAM_CHAT_IDS_JSON="[${_tg_ids_trimmed}]"
    TELEGRAM_FIRST_CHAT_ID=$(printf '%s' "$_tg_ids_trimmed" | cut -d',' -f1)
  fi
fi

echo ""
echo "-- Config repo:     ${HIVEPILOT_CONFIG_REPO}"
echo "-- Anthropic key:   $([ -n "${ANTHROPIC_API_KEY:-}" ] && echo "provided" || echo "not set (skipped)")"
echo "-- Telegram bot:    $([ "$TELEGRAM_ENABLED" = "1" ] && echo "enabled" || echo "not set (skipped)")"

# ---- File generation --------------------------------------------------------

mkdir -p "$INITD_DIR" "$CONFD_DIR" "$LOG_DIR"

# Print $1 shell-quoted (wrapped in single quotes, embedded ' escaped) so
# arbitrary secret content round-trips safely through a later `.` (source).
qval() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

emit_export() {
  # $1=conf.d file  $2=VARNAME  $3=value
  printf 'export %s=%s\n' "$2" "$(qval "$3")" >> "$1"
}

emit_common_env() {
  # $1=conf.d file -- HOME/PATH/config-repo/hot-reload flags shared by every
  # service, plus ANTHROPIC_API_KEY when provided.
  : > "$1"
  emit_export "$1" HOME "/root"
  # $VENV expands now (generation time); $PATH stays literal so it expands
  # at conf.d *source* time (openrc sourcing this file), appending onto
  # whatever PATH openrc already has -- this is what lets the daemon find
  # `claude`/other agent CLIs installed under /root/.local/bin.
  # shellcheck disable=SC2016 # $PATH is meant to stay literal here -- it is
  # expanded later, when openrc sources the generated conf.d file, not now.
  printf 'export PATH="/root/.local/bin:%s/bin:$PATH"\n' "$VENV" >> "$1"
  emit_export "$1" HIVEPILOT_CONFIG_REPO "$HIVEPILOT_CONFIG_REPO"
  emit_export "$1" HIVEPILOT_CONFIG_HOT_RELOAD "true"
  emit_export "$1" HIVEPILOT_PLUGINS_HOT_RELOAD "true"
  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    emit_export "$1" ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY"
  fi
}

write_initd() {
  # $1=service name  $2=command_args (may itself contain literal ${...}
  # shell parameter expansions meant to be evaluated by openrc at service
  # start, not by this generator)  $3=depend() body
  _wi_svc="$1"
  _wi_args="$2"
  _wi_dep="$3"
  _wi_file="$INITD_DIR/$_wi_svc"
  cat > "$_wi_file" <<EOF
#!/sbin/openrc-run

name="$_wi_svc"
description="HivePilot ($_wi_svc)"
supervisor="supervise-daemon"
command="$HP"
command_args="$_wi_args"
pidfile="/run/\${RC_SVCNAME}.pid"
output_log="$LOG_DIR/\${RC_SVCNAME}.log"
error_log="$LOG_DIR/\${RC_SVCNAME}.log"
directory="/root"

depend() {
    $_wi_dep
}
EOF
  chmod +x "$_wi_file"
}

# -- hivepilot-api --
emit_common_env "$CONFD_DIR/hivepilot-api"
emit_export "$CONFD_DIR/hivepilot-api" HIVEPILOT_ENABLE_WEBUI "true"
chmod 600 "$CONFD_DIR/hivepilot-api"
# shellcheck disable=SC2016 # ${HIVEPILOT_API_HOST:-...}/${HIVEPILOT_API_PORT:-...}
# are meant to stay literal -- they are evaluated by openrc-run at service
# start (against whatever this conf.d file exports), not by this generator.
write_initd hivepilot-api \
  'api serve --host ${HIVEPILOT_API_HOST:-127.0.0.1} --port ${HIVEPILOT_API_PORT:-8045}' \
  'need net'
echo "OK wrote hivepilot-api (conf.d + init.d)"

# -- hivepilot-scheduler --
emit_common_env "$CONFD_DIR/hivepilot-scheduler"
emit_export "$CONFD_DIR/hivepilot-scheduler" HIVEPILOT_API_TOKEN "$RUN_TOKEN"
chmod 600 "$CONFD_DIR/hivepilot-scheduler"
write_initd hivepilot-scheduler \
  'schedule daemon --interval 30' \
  'need net; after hivepilot-api'
echo "OK wrote hivepilot-scheduler (conf.d + init.d)"

# -- hivepilot-telegram (optional) --
SERVICES="hivepilot-api hivepilot-scheduler"
if [ "$TELEGRAM_ENABLED" = "1" ]; then
  emit_common_env "$CONFD_DIR/hivepilot-telegram"
  emit_export "$CONFD_DIR/hivepilot-telegram" HIVEPILOT_TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
  # Omit both lines entirely when no chat ids were given -- see the comment
  # above where TELEGRAM_CHAT_IDS_JSON is derived (JSON-strict field; open
  # whitelist is already the default for an unset value).
  if [ -n "$TELEGRAM_CHAT_IDS_JSON" ]; then
    emit_export "$CONFD_DIR/hivepilot-telegram" HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS "$TELEGRAM_CHAT_IDS_JSON"
  fi
  if [ -n "$TELEGRAM_FIRST_CHAT_ID" ]; then
    emit_export "$CONFD_DIR/hivepilot-telegram" HIVEPILOT_TELEGRAM_NOTIFICATION_CHAT_ID "$TELEGRAM_FIRST_CHAT_ID"
  fi
  chmod 600 "$CONFD_DIR/hivepilot-telegram"
  write_initd hivepilot-telegram \
    'telegram start' \
    'need net; after hivepilot-api'
  echo "OK wrote hivepilot-telegram (conf.d + init.d)"
  SERVICES="$SERVICES hivepilot-telegram"
else
  echo "OK skipped hivepilot-telegram (no TELEGRAM_BOT_TOKEN)"
fi

# ---- Enable + best-effort start ---------------------------------------------

for svc in $SERVICES; do
  rc-update add "$svc" default
  echo "OK enabled $svc (rc-update add $svc default)"
done

echo ""
echo "-- Starting services (best-effort -- a failed start here is reported"
echo "   but does NOT abort this script; check the service's log) --"
for svc in $SERVICES; do
  set +e
  rc-service "$svc" restart
  _rc_status=$?
  set -e
  if [ "$_rc_status" -ne 0 ]; then
    echo "WARN: ${svc} failed to (re)start -- check ${LOG_DIR}/${svc}.log" >&2
  else
    echo "OK ${svc} restarted"
  fi
done

echo ""
echo "== Done =="
echo "Services: $SERVICES"
echo "Check status:   rc-service <svc> status"
echo "Tail logs:      tail -f ${LOG_DIR}/hivepilot-*.log"
echo "Health check:   curl -s http://127.0.0.1:8045/health"
echo "Mirador UI (if HIVEPILOT_ENABLE_WEBUI=true) binds to 127.0.0.1 only by"
echo "default -- put a reverse proxy in front, or set HIVEPILOT_API_HOST=0.0.0.0"
echo "in ${CONFD_DIR}/hivepilot-api for remote access. See docs/DEPLOY-PRODUCTION.md."
