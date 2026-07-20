"""hivepilot.services.setup_wizard_extra -- the `hivepilot setup` steps that
don't need to share module-global state with `setup_wizard.py` itself
(welcome/environment probe, admin token bootstrap, runner CLI probe,
plugin install, service guidance, final summary).

Split out of `setup_wizard.py` purely to keep that module focused/under its
line budget -- every function here is re-exported by `setup_wizard.py` and
called the exact same way (`setup_wizard.step_admin_token(...)`, etc.). See
`setup_wizard_common.py`'s docstring for why the shared primitives
(`SetupOptions`, `_env_upsert`, ...) live in a separate leaf module rather
than being imported from `setup_wizard.py` (would create a circular
import).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from hivepilot.config import settings
from hivepilot.services import plugin_installer, token_service
from hivepilot.services.setup_wizard_common import (
    STEP_NAMES,
    SetupOptions,
    _detect_init_system,
    _mask_secret,
    _section_header,
    _status_line,
)
from hivepilot.utils.logging import get_logger

if TYPE_CHECKING:
    from rich.console import Console

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Step 1: welcome + environment probe
# ---------------------------------------------------------------------------


def step_welcome(console: "Console", env_path: Path) -> None:
    from rich.panel import Panel
    from rich.table import Table

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Python", sys.version.split()[0])
    table.add_row("Install path", sys.executable)
    table.add_row("Config dir", str(settings.resolve_config_path(".")))
    table.add_row(".env file", f"{'found' if env_path.exists() else 'not found yet'} ({env_path})")
    table.add_row("Init system", _detect_init_system())
    console.print(Panel(table, title="Environment", border_style="cyan"))


# ---------------------------------------------------------------------------
# Step 3: first admin token (bootstrap)
# ---------------------------------------------------------------------------


def step_admin_token(console: "Console", options: SetupOptions, interactive: bool) -> str:
    from rich.panel import Panel

    _section_header(console, "Admin token")
    existing = token_service.load_tokens()
    if existing:
        _status_line(
            console, True, f"{len(existing)} token(s) already configured -- skipping mint."
        )
        return "already-configured"

    console.print(
        "  [dim]○[/dim] no tokens found. The FIRST token must be admin (bootstrap requirement)."
    )

    # HIGH-2: minting is opt-in in non-interactive mode -- `assume_yes`/
    # `non_interactive` alone must NEVER trigger it. A headless run on a
    # fresh install would otherwise silently mint (and, historically,
    # cleartext-print) a standing admin credential straight into whatever
    # captured the process's stdout, e.g. CI logs. Only an explicit
    # `--mint-admin-token` opts in when there's no real TTY to confirm on;
    # the interactive confirm/`assume_yes` path is unchanged.
    if interactive:
        do_mint = options.assume_yes or typer.confirm("Mint a new admin token now?", default=True)
    else:
        do_mint = options.mint_admin_token

    if not do_mint:
        hint = "  [dim]skipped[/dim] -- run `hivepilot tokens add --role admin` later" + (
            "." if interactive else " (or re-run with --mint-admin-token)."
        )
        console.print(hint)
        return "skipped"

    raw_token, entry = token_service.add_token("admin", note="setup wizard", tenant="default")

    if interactive:
        # One-time cleartext display is allowed ONLY on a real interactive
        # TTY -- there is no other way to hand the operator this token.
        console.print(
            Panel(
                f"[bold]{raw_token}[/bold]\n\n"
                "[yellow]Store this now -- it will not be shown again.[/yellow]",
                title="New admin token",
                border_style="yellow",
            )
        )
    else:
        # Non-interactive: never print the raw value -- it would land
        # straight in CI/build logs. Masked confirmation + retrieval path
        # only.
        console.print(
            Panel(
                f"[bold]{_mask_secret(raw_token)}[/bold]\n\n"
                "[yellow]Minted non-interactively -- raw value not printed. "
                "Retrieve/rotate it via `hivepilot tokens list` / "
                "`hivepilot tokens rotate`.[/yellow]",
                title="New admin token (masked)",
                border_style="yellow",
            )
        )
    return "minted"


# ---------------------------------------------------------------------------
# Step 4: runners / agent CLIs (read-only probe)
# ---------------------------------------------------------------------------


def step_runners(console: "Console") -> str:
    from rich.table import Table

    from hivepilot.services.agent_checks import AGENT_RUNNER_KINDS
    from hivepilot.services.agent_install import AGENT_INSTALL_SPECS, get_install_spec, is_on_path

    _section_header(console, "Agent runners")
    table = Table(box=None)
    table.add_column("kind")
    table.add_column("status")

    kinds = sorted(set(AGENT_RUNNER_KINDS) | set(AGENT_INSTALL_SPECS))
    missing: list[str] = []
    found_count = 0
    for kind in kinds:
        spec = get_install_spec(kind)
        binary = spec.binary if spec is not None else kind
        on_path = is_on_path(binary)
        table.add_row(kind, "[green]✓[/green]" if on_path else "[dim]○[/dim]")
        if on_path:
            found_count += 1
        else:
            missing.append(kind)
    console.print(table)

    if missing:
        console.print(
            f"  [dim]Missing: {', '.join(missing)} -- run `hivepilot agents install <name>` "
            "to set one up.[/dim]"
        )

    return f"{found_count}/{len(kinds)} on PATH"


# ---------------------------------------------------------------------------
# Step 6: plugins
# ---------------------------------------------------------------------------


def step_plugins(
    console: "Console",
    options: SetupOptions,
    interactive: bool,
    env_path: Path,
    *,
    only_requested: bool,
) -> str:
    _section_header(console, "Plugins")

    names: list[str] = []
    if options.plugins:
        names = [n.strip() for n in options.plugins.split(",") if n.strip()]
    elif interactive:
        import questionary

        choices = [
            questionary.Choice(
                title=f"{name} -- {spec.description}",
                value=name,
                checked=plugin_installer.is_enabled(name),
            )
            for name, spec in sorted(plugin_installer.KNOWN_EXAMPLE_PLUGINS.items())
        ]
        picked = questionary.checkbox("Select plugins to install", choices=choices).ask()
        names = picked or []

    unknown = [n for n in names if n not in plugin_installer.KNOWN_EXAMPLE_PLUGINS]
    for name in unknown:
        console.print(f"  [yellow]unknown plugin {name!r} -- skipping[/yellow]")
    names = [n for n in names if n in plugin_installer.KNOWN_EXAMPLE_PLUGINS]

    if not names:
        if only_requested and options.non_interactive:
            console.print("  [dim]no plugins requested via --plugins -- nothing to do.[/dim]")
        console.print("  [dim]skipped[/dim] (no plugins selected)")
        return "skipped"

    installed: list[str] = []
    for name in names:
        try:
            plugin_installer.fetch_plugin(name)
            plugin_installer.persist_enabled(name, env_path=env_path)
        except Exception as exc:  # noqa: BLE001 -- report and continue with the rest
            console.print(f"  [red]✗[/red] {name}: {exc}")
            continue
        installed.append(name)
        flag = plugin_installer.KNOWN_EXAMPLE_PLUGINS[name].env_flag
        console.print(f"  [green]✓[/green] installed {name} ({flag}=true)")

    return f"installed {len(installed)}" if installed else "skipped"


# ---------------------------------------------------------------------------
# Step 8: services
# ---------------------------------------------------------------------------


def _print_foreground_commands(console: "Console") -> None:
    for cmd in ("hivepilot api serve", "hivepilot schedule daemon", "hivepilot telegram"):
        console.print(f"    $ {cmd}")


def _run_shell_script(console: "Console", rel_path: str) -> None:
    script = Path(rel_path)
    if not script.exists():
        console.print(f"  [yellow]{rel_path} not found in this working directory.[/yellow]")
        return
    try:
        result = subprocess.run(["sh", str(script)], timeout=300)
    except Exception as exc:  # noqa: BLE001 -- never abort the wizard
        console.print(f"  [yellow]failed to run {rel_path}: {exc}[/yellow]")
        return
    if result.returncode != 0:
        console.print(f"  [yellow]{rel_path} exited {result.returncode}[/yellow]")


def step_services(console: "Console", interactive: bool, options: SetupOptions) -> str:
    _section_header(console, "Services")

    if shutil.which("rc-service") is not None:
        console.print("  OpenRC detected. Scaffold script: [bold]scripts/setup-openrc.sh[/bold]")
        if interactive and typer.confirm("Run it now? (sh scripts/setup-openrc.sh)", default=False):
            _run_shell_script(console, "scripts/setup-openrc.sh")
        return "openrc"

    if shutil.which("systemctl") is not None:
        console.print(
            "  systemd detected. See docs/DEPLOY-PRODUCTION.md for unit files, or run these "
            "in the foreground:"
        )
        _print_foreground_commands(console)
        return "systemd"

    console.print("  No init system detected. Run these in the foreground (e.g. under tmux):")
    _print_foreground_commands(console)
    return "none"


# ---------------------------------------------------------------------------
# Step 9: final summary
# ---------------------------------------------------------------------------


def step_summary(console: "Console", results: dict[str, str]) -> None:
    from rich.panel import Panel

    from hivepilot.banner import render_banner

    lines = []
    todo = []
    for name in STEP_NAMES:
        status = results.get(name)
        if status is None:
            continue
        needs_followup = status in ("skipped", "unavailable")
        icon = "[dim]○[/dim]" if needs_followup else "[green]✓[/green]"
        lines.append(f"{icon} {name}: {status}")
        if needs_followup:
            todo.append(f"○ {name}: {status}")

    console.print(
        Panel(
            "\n".join(lines) if lines else "(nothing configured)",
            title="Setup summary",
            border_style="cyan",
        )
    )
    if todo:
        console.print(Panel("\n".join(todo), title="Still to do", border_style="yellow"))

    console.print(
        "\nNext: [bold]hivepilot doctor[/bold]  ·  [bold]hivepilot run <project> <task>[/bold]"
    )
    render_banner(console, subtitle="You're all set. Buzz your agents into formation.")
