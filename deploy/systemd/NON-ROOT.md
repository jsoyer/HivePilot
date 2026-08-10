# Running HivePilot as a non-root user

Agents read untrusted input — a client's PR diff — and run shell commands. As
root on a host holding `/etc/hivepilot/shared.env`, an agent can read every
secret directly, whatever its tool allowlist says. The allowlist is then
guarding a door that is not the only way in.

This document is the whole migration, including the parts that bit us. It is
written from a real cutover, not from theory: every failure below actually
happened, in this order.

## The property this buys

systemd parses `EnvironmentFile=` **as root, before dropping privileges**. So
the secrets file stays `0600 root:root` — unreadable by the service user —
while the service still receives its configuration. Combined with `scrub_env`,
which already allowlists what reaches an agent subprocess, the secrets become
genuinely unreachable rather than merely un-granted.

Verify it, do not assume it:

```bash
sudo -u hivepilot cat /etc/hivepilot/shared.env   # must be: Permission denied
```

## 1. The user

```bash
useradd --system --create-home --home-dir /var/lib/hivepilot/home \
        --shell /usr/sbin/nologin hivepilot
```

`nologin` is deliberate: this account runs services, it does not receive SSH
sessions. **Do not remove root's `authorized_keys` in the name of hardening** —
the service user cannot accept a login, so that leaves no way into the host at
all. If you want to stop logging in as root, create a *separate* admin user
with a shell first.

## 2. Everything the services read

The trap is that the list is longer than it looks, and three entries are
invisible until they fail. Enumerate from the running config, then follow the
symlinks — the resolved path and the real path differed here:

```bash
hivepilot config doctor        # state DB, vault, plugin dir
readlink -f /path/to/config/*.yaml
```

| what | where it was | note |
| --- | --- | --- |
| state DB, plugins | already outside `/root` | fine |
| project checkouts | `~/<name>` | follow `HOME`, so they move for free |
| Obsidian vaults | absolute `/root/...` | had to become `~/`-relative |
| **XDG config** | `XDG_CONFIG_HOME=/root/.config` | set in `shared.env`; one line |
| **config-repo clone** | `/root/.local/share/hivepilot/config-repo` | a *second*, legacy clone the YAML symlinks pointed at |
| **logs** | `logs_dir` is cwd-relative | services set `WorkingDirectory=/` → `/runs/logs` |

`~/`-relative paths are the right answer for project and vault paths: they
satisfy the portability rule *and* follow whoever the units run as. Verify:

```bash
HOME=/var/lib/hivepilot/home python -c \
  'from pathlib import Path; print(Path("~/noxys").expanduser())'
```

## 3. Credentials the agents need

Both are plain files, so both copy. Neither needs an interactive login — this
was the unknown that made the migration look risky, and it is not one.

```bash
H=/var/lib/hivepilot/home
cp -a /root/.claude   $H/.claude      # Claude CLI: .credentials.json
cp -a /root/.config/gh $H/.config/gh  # git auth goes through `gh auth git-credential`
cp -a /root/.ssh/id_ed25519{,.pub} $H/.ssh/   # OUTBOUND key only
chown -R hivepilot:hivepilot $H
chmod 700 $H/.ssh && chmod 600 $H/.ssh/id_ed25519
```

Prove each one rather than assuming:

```bash
sudo -u hivepilot env HOME=$H gh auth status              # Logged in
sudo -u hivepilot env HOME=$H IS_SANDBOX=1 claude --print \
  --permission-mode acceptEdits "Reply with exactly: OK"  # OK
```

The `claude --print` is the only test that proves the credentials moved. A
version string proves the binary runs, nothing more.

## 4. The units

A drop-in, never an edit — rollback is then deleting one file per unit:

```bash
for u in api scheduler telegram slack discord; do
  d=/etc/systemd/system/hivepilot-$u.service.d && mkdir -p $d
  printf '[Service]\nUser=hivepilot\nGroup=hivepilot\nEnvironment=HOME=/var/lib/hivepilot/home\n' \
    > $d/10-non-root.conf
done
systemctl daemon-reload && systemctl restart hivepilot-{api,scheduler,telegram}
```

## 5. Ownership

```bash
chown -R hivepilot:hivepilot /var/lib/hivepilot /runs
chmod 2775 /runs /runs/logs        # setgid — see below
chmod 700 /root                    # back to the default
```

**The setgid on `/runs` is not cosmetic.** Any `hivepilot` CLI command run as
root recreates the log file root-owned, and the services then fail to start on
the next restart with `PermissionError: /runs/logs/hivepilot.log`. This
happened twice during the cutover. setgid keeps the group, so both users can
write.

## 6. Verify — in this order

Each step catches a failure the previous one cannot:

```bash
systemctl is-active hivepilot-{api,scheduler,telegram}     # 1. three "active"
ps -o user= -p $(systemctl show -p MainPID --value hivepilot-api)   # 2. hivepilot
curl -s localhost:8045/health                              # 3. really serving
sudo -u hivepilot cat /etc/hivepilot/shared.env            # 4. Permission denied
hivepilot config doctor | grep state_db_liveness           # 5. the RUN COUNT
sudo -u hivepilot env HOME=… claude --print "…"            # 6. a real agent call
```

Step 1 is not enough on its own: with `Restart=always` and `Type=simple`,
`is-active` reports **active** for a process that is crash-looping. Ours did,
for several minutes, while reporting `activating`.

Step 5 checks the row count, not the path. A permissions failure on the
database shows up as a *fresh-looking* database — a plausible zero, not an
error.

## 7. Run CLI commands as the service user

After the cutover, `hivepilot <cmd>` typed into a root shell is a different
program than the one the services run:

- `~` expands to `/root`, so `config doctor` reports vaults that do not exist;
- it can take ownership of the log file (see §5);
- `git` refuses the service user's repositories as "dubious ownership"
  (`git config --system --add safe.directory …`).

Run them the way the services do — as `hivepilot`, from `cwd=/`, with the
service environment:

```bash
set -a; . /etc/hivepilot/shared.env; set +a
cd / && sudo -u hivepilot env HOME=/var/lib/hivepilot/home \
  XDG_CONFIG_HOME="$XDG_CONFIG_HOME" XDG_DATA_HOME="$XDG_DATA_HOME" \
  /opt/hivepilot/venv/bin/hivepilot <cmd>
```

## Rollback

```bash
rm /etc/systemd/system/hivepilot-*.service.d/10-non-root.conf
systemctl daemon-reload && systemctl restart hivepilot-{api,scheduler,telegram}
```

Nothing else has to be undone: root can read everything it owned before, so
the moved directories and copied credentials stay valid.

## What this does NOT fix

Every secret still on the host is still on the host. Non-root shrinks the
blast radius; it does not empty it. Moving secrets to a manager (1Password,
Vault, KMS — see `docs/PLUGINS.md`) is the complement: each one that leaves
`shared.env` is one fewer thing this migration has to protect.
