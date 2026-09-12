## Summary

`configure_logging` fail-opens to stderr when the resolved `logs_dir` is not writable.

On noxysdevbot, `jeromesoyer@:~$ hivepilot --version` imported the CLI, resolved the default relative `runs/logs` to `~/runs/logs/hivepilot.log`, and died with `PermissionError` before printing a version. Units are fine (`WorkingDirectory=/` → `/runs/logs`). A login-shell CLI is not.

Replay: `HIVEPILOT_LOGS_DIR=/runs/logs hivepilot --version` (current box); after this lands, a bare `--version` from `$HOME` must start even if `~/runs/logs` is root-owned.

## Testing

- [ ] `env -u FORCE_COLOR -u EXEC_DAEMON_STARTUP_TRACEPARENT COLUMNS=200 pytest tests/test_logs_dir_is_resolved.py tests/test_logging_rotation.py -q`
