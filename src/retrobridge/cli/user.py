import json
import time
from itertools import groupby
from pathlib import Path
import typer
from typing import Optional, List
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.text import Text
from rich.markup import escape

from retrobridge.services.catalog import list_games
from retrobridge.services.status import get_system_status
from retrobridge.services.sync import run_sync
from retrobridge.services.setup import (
    check_environment,
    initialize_directories,
    install_timer,
    uninstall_timer,
)
from retrobridge.storage.lock import LockError
from retrobridge.services.removal import request_removal
from retrobridge.services.pin import set_game_pin
from retrobridge.services.search import search_candidates, _title_match_bonus
from retrobridge.services.find import (
    expand_disc_candidates,
    find_existing_game,
    refresh_cache_if_needed,
    same_disc_family,
)
from retrobridge.services.add import add_game, AddGameError
from retrobridge.services.export import export_to_target, ExportError
from retrobridge.config import LANGUAGE_NAMES, TargetConfig, load_config, normalize_languages, save_config
from retrobridge.storage.db import init_db, get_db_connection, is_ttl_expired
from retrobridge.cli.tools import app as tools_app
from retrobridge.cli.formatting import (
    bar_parts,
    basename,
    format_size,
    format_size_compact,
    progress_label,
    truncate,
)
from retrobridge.domain.text import strip_emoji
from retrobridge.domain.scoring import normalize_text
from retrobridge.adapters.transmission import TransmissionClient

app = typer.Typer(help="RetroBridge v2 - game collection manager and bridge")
app.add_typer(tools_app, name="tools")
console = Console()


def _show_version(value: bool) -> None:
    if not value:
        return
    try:
        from importlib.metadata import version as package_version
        label = package_version("retrobridge")
    except Exception:
        label = "2.0.0"
    typer.echo(f"retrobridge {label}")
    raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_show_version,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """RetroBridge v2 — game collection management and bridge."""

# Sub-application for pack commands
pack_app = typer.Typer(help="Game pack (torrent) management commands")
app.add_typer(pack_app, name="pack")


@pack_app.command("add")
def cmd_pack_add(
    torrent_file: str = typer.Argument(..., help="Path to the .torrent file to add"),
    keep_all: bool = typer.Option(False, "--keep-all", help="Keep all files in the pack selected"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
    config_path: Optional[str] = typer.Option(None, "--config", help="Path to the configuration file"),
):
    """Add a paused game pack and deselect all of its files."""

    cfg = load_config(config_path)
    torrent_path = Path(torrent_file).expanduser().resolve()
    if not torrent_path.is_file():
        if as_json:
            typer.echo(json.dumps({"error": f"Torrent file '{torrent_file}' does not exist."}))
        else:
            console.print(f"[bold red]Error:[/bold red] Torrent file '{torrent_file}' does not exist.")
        raise typer.Exit(code=1)

    client = TransmissionClient(
        rpc_url=cfg.transmission.rpc_url,
        username=cfg.transmission.user,
        password=cfg.transmission.password,
        timeout=cfg.transmission.timeout,
    )
    try:
        torrent_id = client.add_torrent(str(torrent_path), pause=True)
    except Exception as e:
        if as_json:
            typer.echo(json.dumps({"error": str(e)}))
        else:
            console.print(f"[bold red]Error while adding torrent:[/bold red] {escape(str(e))}")
        raise typer.Exit(code=1)

    deselected = 0
    if not keep_all:
        try:
            deselected = client.select_nothing(torrent_id)
        except Exception as e:
            console.print(f"[yellow]Warning: unable to deselect files:[/yellow] {escape(str(e))}")

    res = {
        "torrent_id": torrent_id,
        "file": str(torrent_path),
        "paused": True,
        "deselected_files": deselected,
    }
    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return

    console.print(f"[bold green]Pack added (paused) with ID {torrent_id}.[/bold green]")
    console.print(f"  • File: {torrent_path}")
    if deselected:
        console.print(f"  • Files deselected: {deselected} (no unnecessary downloads)")
    console.print("[dim]Run 'retrobridge tools scan' to index it, then 'retrobridge add' to select games.[/dim]")


STEPS = [
    ("paths", "Path and destination configuration"),
    ("transmission", "Transmission connection configuration (host and RPC port)"),
    ("scoring", "Scoring preferences configuration (region and languages)"),
    ("automation", "Automation (sync timer)"),
]


def _run_setup_step(step_name: str, cfg) -> None:
    """Run an interactive step of the configuration wizard."""

    if step_name == "paths":
        dl_input = typer.prompt("Downloads directory (Download-Dir)", default=str(cfg.paths.download_dir))
        lib_input = typer.prompt("Game library directory (Library-Dir)", default=str(cfg.paths.library_dir))
        cfg.paths.download_dir = Path(dl_input)
        cfg.paths.library_dir = Path(lib_input)

        if cfg.targets:
            console.print(
                "[dim]Targets already configured: "
                + ", ".join(f"{name} ({t.profile})" for name, t in cfg.targets.items())
                + "[/dim]"
            )
        else:
            profile = typer.prompt(
                "Destination profile (esde, knulli or none)",
                default="esde",
            ).strip().lower()
            if profile not in {"aucun", "none", ""}:
                mount = typer.prompt(
                    "SD card mount point",
                    default=str(Path.home() / "SDCard"),
                )
                cfg.targets["sd"] = TargetConfig(
                    name="sd",
                    profile=profile,
                    mount_path=Path(mount).expanduser(),
                )
    elif step_name == "transmission":
        cfg.transmission.host = typer.prompt("Transmission RPC host", default=cfg.transmission.host)
        cfg.transmission.port = int(typer.prompt("Transmission RPC port", default=str(cfg.transmission.port)))
    elif step_name == "scoring":
        cfg.scoring.region_preference = typer.prompt(
            "Region preference [NTSC/PAL]", default=cfg.scoring.region_preference
        ).upper()
        langs_default = ",".join(cfg.scoring.preferred_languages or ["fr", "en"])
        langs_input = typer.prompt(
            "Preferred languages (comma-separated codes, in order of importance)",
            default=langs_default,
        )
        langs = normalize_languages(
            [part.strip() for part in langs_input.split(",") if part.strip()],
            fallback=cfg.scoring.pal_language,
        )
        cfg.scoring.preferred_languages = langs
        cfg.scoring.pal_language = LANGUAGE_NAMES.get(langs[0], langs[0])
    elif step_name == "automation":
        if not typer.confirm(
            "Install the automatic sync timer (10 min)?",
            default=True,
        ):
            console.print("[dim]Timer not installed. You can rerun 'retrobridge setup --step 4'.[/dim]")
            return
        res = install_timer()
        if res.get("systemctl_ok"):
            status = res.get("status", {})
            next_run = status.get("next_run") or "?"
            console.print(f"[green]Timer installed.[/green] Next sync: {next_run}")
        else:
            console.print(
                "[yellow]Units written, but systemd --user could not activate them.[/yellow]\n"
                f"[dim]Check: {res['service_path']} and {res['timer_path']}[/dim]"
            )

@app.command("setup")
def cmd_setup(
    check: bool = typer.Option(False, "--check", "-c", help="Only check prerequisites and environment"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
    config_path: Optional[str] = typer.Option(None, "--config", help="Path to the configuration file"),
    step: Optional[int] = typer.Option(None, "--step", help="Run only numbered step N (1 to 4)"),
    timer: Optional[bool] = typer.Option(
        None,
        "--timer/--no-timer",
        help="Install (--timer) or skip (--no-timer) the sync timer; asks interactively by default",
    ),
    uninstall: bool = typer.Option(False, "--uninstall-timer", help="Uninstall the sync timer"),
):
    """Check the environment and configure RetroBridge."""
    cfg = load_config(config_path)

    if uninstall:
        result = uninstall_timer()
        if as_json:
            typer.echo(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            console.print("[green]Timer uninstalled.[/green]")
        return

    if check:
        report = check_environment(cfg)
        if as_json:
            typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
            return

        table = Table(title="RetroBridge environment diagnostics")
        table.add_column("Component", style="cyan")
        table.add_column("State", justify="center")
        table.add_column("Detail", style="white")

        def status_badge(s: str) -> str:
            if s == "ok":
                return "[bold green]OK[/bold green]"
            elif s == "warn":
                return "[bold yellow]WARN[/bold yellow]"
            return "[bold red]FAIL[/bold red]"

        # Python
        table.add_row("Python", status_badge(report["python"]["status"]), f"{report['python']['version']} ({report['python']['message']})")
        # Transmission
        table.add_row("Transmission RPC", status_badge(report["transmission"]["status"]), f"{report['transmission']['url']} - {report['transmission']['message']}")
        # Tools
        chd = report["tools"]["chdman"]
        table.add_row("Tool: chdman", status_badge(chd["status"]), chd["message"])
        dol = report["tools"]["dolphin_tool"]
        table.add_row("Tool: dolphin-tool", status_badge(dol["status"]), dol["message"])
        # Paths
        dl = report["paths"]["download_dir"]
        table.add_row("Download-Dir", status_badge(dl["status"]), f"{dl['path']} ({dl['free_gb']} GB free)")
        lib = report["paths"]["library_dir"]
        table.add_row("Library-Dir", status_badge(lib["status"]), f"{lib['path']} ({lib['free_gb']} GB free)")
        fs = report["paths"]["same_filesystem"]
        table.add_row("Hardlinks partition", status_badge(fs["status"]), fs["message"])
        # Sync timer
        timer_info = report.get("timer", {})
        if timer_info.get("installed"):
            timer_state = "active" if timer_info.get("active") else "inactive"
            timer_detail = f"{timer_state}"
            if timer_info.get("next_run"):
                timer_detail += f" — next: {timer_info['next_run']}"
            table.add_row("Timer sync", status_badge("ok" if timer_info.get("active") else "warn"), timer_detail)
        else:
            table.add_row(
                "Timer sync",
                status_badge("warn"),
                "not installed — run 'retrobridge setup --step 4'",
            )

        console.print(table)
        if not report["overall_ok"]:
            console.print("[bold yellow]Warning: some components or directories need attention.[/bold yellow]")
        else:
            console.print("[bold green]All fundamental prerequisites are satisfied![/bold green]")
        return

    # Interactive configuration mode
    console.print(Panel("[bold cyan]RetroBridge v2 configuration wizard[/bold cyan]\nConfigure your directories and Transmission connection."))

    # If --step is specified, run only that step
    if step is not None:
        if step < 1 or step > len(STEPS):
            console.print(f"[bold red]Error:[/bold red] Step must be between 1 and {len(STEPS)}.")
            raise typer.Exit(code=1)

        step_name, step_description = STEPS[step - 1]
        console.print(f"\n[bold cyan]Step {step}/{len(STEPS)}: {step_description}[/bold cyan]")
        _run_setup_step(step_name, cfg)
        console.print(f"[green]Step '{step_name}' updated.[/green]")

        initialize_directories(cfg)
        save_config(cfg, config_path)
        init_db()
        console.print("[bold green]Configuration saved successfully![/bold green]")
        return

    # By default, run all steps
    for i, (step_name, step_description) in enumerate(STEPS, start=1):
        if step_name == "automation":
            if timer is False:
                console.print("\n[dim]Timer not installed (--no-timer).[/dim]")
                continue
            if timer is True:
                console.print(f"\n[bold cyan]Step {i}/{len(STEPS)}: {step_description}[/bold cyan]")
                res = install_timer()
                if res.get("systemctl_ok"):
                    console.print(f"[green]Timer installed.[/green] Next sync: {res.get('status', {}).get('next_run') or '?'}")
                else:
                    console.print("[yellow]Units written, systemd activation failed.[/yellow]")
                continue
        console.print(f"\n[bold cyan]Step {i}/{len(STEPS)}: {step_description}[/bold cyan]")
        _run_setup_step(step_name, cfg)

    initialize_directories(cfg)
    save_config(cfg, config_path)
    init_db()
    console.print("[bold green]Configuration saved successfully![/bold green]")
    console.print("Recommended next steps:")
    console.print("  • [cyan]retrobridge setup --check[/cyan] to check the state")
    console.print("  • [cyan]retrobridge tools scan[/cyan] to index active torrents")
    console.print("  • [cyan]retrobridge status[/cyan] to see the overview")
    console.print("  • [cyan]retrobridge --install-completion[/cyan] to enable shell completion")


@app.command("list")
def cmd_list(
    system: Optional[str] = typer.Option(None, "--system", "-s", help="Filter by system"),
    state: Optional[str] = typer.Option(None, "--state", help="Filter by state (downloading, downloaded, linked)"),
    sort: str = typer.Option("system", "--sort", help="Sort: system (default), title, state, score"),
    wide: bool = typer.Option(False, "--wide", "-w", help="Show full titles and game files (may span multiple lines)"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database")
):
    """List the collection's games and their current state."""
    cfg = load_config()
    games = list_games(system=system, state=state, db_path=db_path, download_dir=cfg.paths.download_dir)

    if as_json:
        typer.echo(json.dumps(games, indent=2, ensure_ascii=False))
        return

    if not games:
        console.print("[yellow]No games found.[/yellow]")
        return

    sort_key = sort.strip().lower()
    if sort_key not in {"system", "title", "state", "score"}:
        console.print(f"[bold red]Error:[/bold red] unknown sort '{sort}'. Use system, title, state or score.")
        raise typer.Exit(code=1)

    if sort_key == "title":
        games.sort(key=lambda g: ((g["title"] or "").lower(), g["system"]))
    elif sort_key == "state":
        games.sort(key=lambda g: (g.get("download_status") or g["state"] or "", (g["system"] or ""), (g["title"] or "").lower()))
    elif sort_key == "score":
        games.sort(key=lambda g: (-(g["match_score"] or 0), (g["system"] or ""), (g["title"] or "").lower()))
    else:
        games.sort(key=lambda g: ((g["system"] or ""), (g["title"] or "").lower()))

    if sort_key == "system":
        console.print(f"[bold]RetroBridge collection[/bold] — {len(games)} games")
        for sys_name, group in groupby(games, key=lambda g: g["system"] or "?"):
            group_list = list(group)
            console.rule(f"[bold cyan]{sys_name}[/bold cyan] ({len(group_list)})", style="cyan")
            _print_list_table(group_list, wide=wide, show_system=False)
    else:
        console.print(f"[bold]RetroBridge collection[/bold] — {len(games)} games (sort: {sort_key})")
        _print_list_table(games, wide=wide, show_system=True)

    console.print(
        "[dim]✅ linked · 🟡 pending/partial · 📥 in progress · ⏳ queued · "
        "❌ broken · 🗑️ removal[/dim]"
    )


DOWNLOAD_SYMBOL = {
    "queued": ("⏳", "yellow"),
    "downloading": ("📥", "green"),
    "complete": ("✅", "green"),
    "broken": ("❌", "red"),
    "orphan_partial": ("🟡", "yellow"),
    "present_unwanted": ("🟡", "yellow"),
    "unknown": ("❓", "red"),
    "linked": ("✅", "green"),
    "downloaded": ("🟡", "cyan"),
    "removing": ("🗑️", "red"),
}


def _rich_bar(percent: float, width: int = 8) -> Text:
    full, partial, empty = bar_parts(percent, width)
    text = Text(full)
    if partial:
        text.append(partial)
    text.append(empty)
    return text


def _completion_cell(percent: float) -> Text:
    cell = _rich_bar(percent, width=6)
    cell.append(f"  {int(round(percent)):3d}%", style="green")
    return cell


def _download_progress_cell(ad: dict, color: str) -> Text:
    status_key = ad.get("download_status") or "unknown"
    if status_key in ("broken", "unknown"):
        return Text("—", style="dim")
    pct = ad.get("progress_pct") or 0
    cell = _rich_bar(pct, width=6)
    cell.append("\n")
    cell.append(progress_label(pct), style=color)
    return cell


def _print_list_table(games: List[dict], wide: bool, show_system: bool) -> None:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, expand=True)
    if show_system:
        table.add_column("System", style="cyan", no_wrap=True, min_width=9)
    table.add_column("Title", style="bold white", no_wrap=not wide, overflow="ellipsis" if not wide else "fold", ratio=6)
    table.add_column("State", justify="center", no_wrap=True, width=4)
    table.add_column("Score", justify="right", no_wrap=True, width=5)
    table.add_column("Language", style="magenta", no_wrap=True, width=16)
    table.add_column("Game File", style="dim", no_wrap=not wide, overflow="ellipsis" if not wide else "fold", ratio=6)

    for g in games:
        title = g.get("title") or "-"
        rom = g.get("matched_file_name") or "-"
        if not wide:
            title = truncate(title, 48)
            rom = truncate(basename(rom), 48)

        status_key = g.get("download_status") or g.get("state") or "unknown"
        symbol, color = DOWNLOAD_SYMBOL.get(status_key, ("❓", "white"))
        lang = strip_emoji(g.get("match_lang_tag") or "")
        if not wide:
            lang = truncate(lang, 16)

        row = [
            Text(title),
            Text(symbol, style=color),
            Text(str(g.get("match_score") or 0)),
            Text(lang),
            Text(rom),
        ]
        if show_system:
            row.insert(0, Text(g.get("system") or "?", style="cyan"))
        table.add_row(*row)

    console.print(table)

@app.command("status")
def cmd_status(
    compact: bool = typer.Option(False, "--compact", "-c", help="Compact view without per-system detail"),
    refresh: bool = typer.Option(
        True,
        "--refresh/--no-refresh",
        help="Automatic (safe) sync if the cache is stale; --no-refresh for strictly read-only status",
    ),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
    no_live: bool = typer.Option(False, "--no-live", help="Do not query Transmission (local snapshot only)"),
    full_path: bool = typer.Option(False, "--full-path", help="Show the full game file path instead of the file name"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database")
):
    """Show the global view: collection states, system health, per-system breakdown and downloads."""
    cfg = load_config()
    refresh_info = None

    # Auto-sync stays enabled by default (including in --json); --no-refresh
    # guarantees a strictly read-only status for scripts.
    refresh_enabled = refresh

    if refresh_enabled:
        init_db(db_path)
        with get_db_connection(db_path) as conn:
            stale = is_ttl_expired(conn, "last_sync", cfg.ttl.status_ttl)

        if stale:
            if not as_json:
                console.print("[dim]Last check > 10 min ago: automatic sync…[/dim]")
            started = time.perf_counter()
            try:
                sync_kwargs = dict(
                    db_path=db_path,
                    # Never run a full scan from status: the timer/`sync` handles it.
                    skip_scan=True,
                    # No change to the Transmission selection.
                    apply=False,
                    reconcile_transmission=False,
                    # No destructive action (deletions, cleanup).
                    execute_pending=False,
                )
                if not as_json and console.is_terminal:
                    with console.status("[dim]Syncing…[/dim]"):
                        sync_res = run_sync(**sync_kwargs)
                else:
                    sync_res = run_sync(**sync_kwargs)

                refresh_info = {
                    "ran": True,
                    "mode": "safe",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "newly_downloaded": sync_res["newly_downloaded"],
                    "links_created_or_verified": sync_res["links_created_or_verified"],
                    "errors": sync_res["errors"],
                }
                if not as_json:
                    console.print(
                        f"[green]Auto-sync (safe):[/green] {sync_res['newly_downloaded']} downloaded, "
                        f"{sync_res['links_created_or_verified']} links verified "
                        f"in {refresh_info['duration_ms']} ms."
                    )
                    for err in sync_res["errors"]:
                        console.print(f"  [yellow]! {escape(str(err))}[/yellow]")
            except Exception as e:
                # An already running sync (lock) or unreachable Transmission must
                # never prevent status from displaying.
                refresh_info = {
                    "ran": False,
                    "mode": "safe",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "error": str(e),
                }
                if not as_json:
                    console.print(f"[yellow]Auto-sync failed:[/yellow] {escape(str(e))}")

    status = get_system_status(db_path=db_path, live=not no_live)
    if refresh_info is not None:
        status["refresh"] = refresh_info

    if as_json:
        typer.echo(json.dumps(status, indent=2, ensure_ascii=False))
        return

    games = status["games"]
    acquired = games.get("acquired", games["downloaded"] + games["linked"])
    pending_link = games.get("downloaded_pending", games["downloaded"])
    pending_suffix = f", {pending_link} pending link" if pending_link else ""
    console.print(Panel(
        f"[bold yellow]Downloading:[/bold yellow] {games['downloading']}   "
        f"[bold green]Downloaded:[/bold green] {acquired} "
        f"[dim](including linked: {games['linked']}{pending_suffix})[/dim]   "
        f"[bold white](Total: {games['total']})[/bold white]",
        title="[bold]📊 Collection State[/bold]"
    ))
    console.print(
        "[dim]Downloaded = acquired (linked + pending link). "
        "Exclusive states: downloading → downloaded → linked.[/dim]"
    )
    console.print(
        f"[bold]Size:[/bold] {format_size(games.get('acquired_bytes', 0))} acquired · "
        f"{format_size(games.get('downloading_bytes', 0))} in progress"
    )

    # Snapshot freshness (UTC in DB, local display)
    freshness = status.get("freshness") or {}
    scan_info = freshness.get("scan") or {}
    sync_info = freshness.get("sync") or {}
    if scan_info or sync_info:
        stale_badge = " [bold yellow]\\[stale][/bold yellow]" if freshness.get("stale") else ""
        scan_at = scan_info.get("at_local") or "?"
        console.print(
            f"[bold]Snapshot:[/bold] scan {scan_info.get('age_label', 'unknown')} "
            f"({scan_at}) · sync {sync_info.get('age_label', 'unknown')}{stale_badge}"
        )

    timer_info = status.get("timer") or {}
    if timer_info.get("installed"):
        timer_state = "active" if timer_info.get("active") else "inactive"
        next_run = timer_info.get("next_run")
        suffix = f" — next: {next_run}" if next_run else ""
        console.print(f"[bold]Auto-sync:[/bold] timer {timer_state}{suffix}")
    else:
        console.print("[dim]Auto-sync: timer not installed (retrobridge setup --step 4)[/dim]")

    # Per-system table
    if not compact and status.get("systems"):
        table = Table(
            title=f"Per-system breakdown ({len(status['systems'])} systems configured)",
            expand=True,
        )
        table.add_column("System", style="cyan bold", no_wrap=True, min_width=12)
        table.add_column("Total", justify="right", style="bold white", no_wrap=True)
        table.add_column("Downloaded", justify="right", style="green", no_wrap=True)
        table.add_column("In progress", justify="right", style="bold yellow", no_wrap=True)
        table.add_column("Size", justify="right", no_wrap=True)
        table.add_column("Completeness", justify="right", no_wrap=True, min_width=18)

        for s in status["systems"]:
            pct = s["progress_pct"]
            acquired = s.get("acquired", s["downloaded"] + s["linked"])
            acquired_cell = str(acquired)
            if s["linked"] > 0:
                acquired_cell += f" ({s['linked']} linked)"
            table.add_row(
                s["system"],
                str(s["total"]),
                acquired_cell if acquired > 0 else "[dim]-[/dim]",
                str(s["downloading"]) if s["downloading"] > 0 else "[dim]-[/dim]",
                format_size_compact(s.get("size_bytes")),
                _completion_cell(pct),
            )
        console.print(table)

    # Active downloads
    if status.get("target_issues"):
        console.print(
            f"[bold red]⚠️ {status['target_issues']} broken target(s)[/bold red] "
            "[dim](file missing and unselected — see 'retrobridge sync')[/dim]"
        )

    if status.get("active_downloads"):
        total_active = status.get("active_downloads_total", games["downloading"])
        dl_table = Table(title=f"Active downloads ({total_active} in progress)", expand=True)
        dl_table.add_column("System", style="cyan", no_wrap=True, min_width=9)
        dl_table.add_column("Game / File", style="bold white", ratio=1, no_wrap=True, overflow="ellipsis")
        dl_table.add_column("State", justify="center", no_wrap=True, width=4)
        dl_table.add_column("Size", justify="right", no_wrap=True, width=16)
        dl_table.add_column("Progress", justify="right", no_wrap=True, width=18)

        for ad in status["active_downloads"]:
            if full_path:
                rom = ad.get("matched_file_path") or ad.get("matched_file_name") or "-"
            else:
                rom = ad.get("file_name") or basename(ad.get("matched_file_name")) or "-"

            status_key = ad.get("download_status") or "unknown"
            symbol, color = DOWNLOAD_SYMBOL.get(status_key, ("❓", "white"))

            title = ad.get("title") or "-"
            discs_total = ad.get("discs_total", 1)
            if discs_total > 1:
                title = f"{title} ({discs_total} discs)"
            if not full_path:
                title = truncate(title, 56)
                rom = truncate(rom, 56)
            game_cell = Text(title)
            game_cell.append("\n")
            game_cell.append(rom, style="dim")
            if ad.get("issue"):
                game_cell.append(" !", style="bold red")

            if status_key in ("broken", "unknown"):
                size_cell = Text("—", style="dim")
            else:
                size_cell = Text(
                    f"{format_size_compact(ad.get('bytes_completed'))} / "
                    f"{format_size_compact(ad.get('file_length'))}",
                    style="dim",
                )

            dl_table.add_row(
                ad.get("system") or "?",
                game_cell,
                Text(symbol, style=color),
                size_cell,
                _download_progress_cell(ad, color),
            )

        console.print(dl_table)
        console.print(
            "[dim]⏳ queued · 📥 in progress · ✅ complete · ❌ broken · "
            "🟡 partial/orphan — 'broken' = missing + unselected.[/dim]"
        )
        if total_active > len(status["active_downloads"]):
            console.print(f"[dim]... and {total_active - len(status['active_downloads'])} more being acquired.[/dim]")

    # Transmission & Storage
    t_info = status["transmission"]
    if t_info.get("live") is False:
        t_status = "[dim]live disabled (--no-live)[/dim]"
    elif t_info["reachable"]:
        rate_dl_kb = round(t_info.get("download_rate_bytes", 0) / 1024, 1)
        rate_up_kb = round(t_info.get("upload_rate_bytes", 0) / 1024, 1)
        speed_str = f" | Rate: [bold green]↓ {rate_dl_kb} KB/s[/bold green] [bold cyan]↑ {rate_up_kb} KB/s[/bold cyan]"
        ratio_str = f" | Global ratio: [bold]{t_info.get('ratio', '-')}[/bold]"
        torrents_str = f" | Torrents: [bold]{t_info.get('torrents_active', 0)}/{t_info.get('torrents_total', 0)}[/bold] active"
        t_status = f"[green]Connected[/green]{speed_str}{ratio_str}{torrents_str}"
    else:
        t_status = "[red]Unreachable[/red]"
        if status.get("torrent_cache_size"):
            t_status += " [dim](slow or busy RPC — state from last scan)[/dim]"

    console.print(f"\n[bold]Transmission:[/bold] {t_status}")

    # Gap between what Transmission requests and what is actually selected
    sel = status.get("selection", {})
    wanted_bytes = sel.get("wanted_bytes", 0)
    selected_bytes = sel.get("selected_bytes", 0)
    wanted_files = sel.get("wanted_files", 0)
    if wanted_bytes > max(selected_bytes * 1.5, 10 * 1024**3):
        wanted_tb = round(wanted_bytes / 1024**4, 2)
        selected_gb = round(selected_bytes / 1024**3, 1)
        console.print(
            f"[bold yellow]⚠️ Transmission requests {wanted_tb} TB ({wanted_files} files) "
            f"for {selected_gb} GB of selected games.[/bold yellow]"
        )
        console.print("[dim]   Run [cyan]retrobridge tools apply[/cyan] to deselect the rest of the packs.[/dim]")

    st = status["storage"]
    dl = st["download_dir"]
    lib = st["library_dir"]
    fs_ok = "[green]OK[/green] (same partition)" if st["same_partition_for_hardlinks"] else "[yellow]Different partitions[/yellow]"
    console.print(f"[bold]Storage:[/bold] Download: {dl['free_gb']} GB free | Library: {lib['free_gb']} GB free (Hardlinks: {fs_ok})")

    if status.get("targets"):
        console.print("\n[bold]Export targets:[/bold]")
        for name, t in status["targets"].items():
            mounted = "[green]Mounted[/green]" if t["mounted"] else "[red]Not mounted[/red]"
            console.print(f"  • {name} ({t['profile']}): {mounted} - {t['free_gb']} GB free, {t['exported_games']} games exported")

    if status.get("pending_actions"):
        console.print(f"\n[bold yellow]Pending actions:[/bold yellow] {status['pending_actions']}")


@app.command("sync")
def cmd_sync(
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate operations without modifying files or the database"),
    force_scan: bool = typer.Option(False, "--force-scan", help="Force the Transmission scan, ignoring the TTL"),
    profile: str = typer.Option("esde", "--profile", "-p", help="Target profile for Library-Dir organization"),
    apply: bool = typer.Option(True, "--apply/--no-apply", help="Reapply file selection in Transmission"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="No output except errors"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database")
):
    """Full local reconciliation: scan if needed, update states and create links."""
    try:
        res = run_sync(profile_name=profile, dry_run=dry_run, force_scan=force_scan, db_path=db_path, apply=apply)
    except LockError:
        # Normal case when the timer is already running: do not fail systemd.
        if as_json:
            typer.echo(json.dumps({"skipped": True, "reason": "locked"}, ensure_ascii=False))
        elif not quiet:
            console.print("[yellow]Sync already in progress — skipped.[/yellow]")
        return

    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return
    if quiet:
        return

    mode = "[yellow](DRY RUN)[/yellow] " if dry_run else ""
    console.print(f"{mode}[bold green]Sync complete.[/bold green]")
    console.print(f"  • Newly downloading: {res['newly_downloading']}")
    console.print(f"  • Newly downloaded: {res['newly_downloaded']}")
    console.print(f"  • Links verified / created: {res['links_created_or_verified']}")

    for action in res["actions"]:
        console.print(f"    [dim]-> {action}[/dim]")

    if res["errors"]:
        console.print("[bold red]Errors:[/bold red]")
        for err in res["errors"]:
            console.print(f"    [red]! {escape(str(err))}[/red]")

@app.command("remove")
def cmd_remove(
    title: str = typer.Argument(..., help="Title of the game to remove"),
    system: Optional[str] = typer.Option(None, "--system", "-s", help="Game system"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database")
):
    """Mark a game for asynchronous removal (Transmission, Library, Downloads, Targets)."""
    actions = request_removal(title=title, system=system, db_path=db_path)
    if not actions:
        console.print(f"[yellow]No game found matching '{escape(str(title))}'.[/yellow]")
        return
    console.print(f"[bold green]Game marked for removal ({len(actions)} game(s)).[/bold green]")
    console.print("[dim]Local files will be cleaned up on the next 'sync', targets on the next 'export'.[/dim]")

@app.command("pin")
def cmd_pin(
    title: str = typer.Argument(..., help="Title of the game to pin"),
    system: Optional[str] = typer.Option(None, "--system", "-s", help="Game system"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database")
):
    """Pin the selected game file for a game."""
    if set_game_pin(title=title, system=system, pinned=True, db_path=db_path):
        console.print(f"[bold green]Game '{escape(str(title))}' pinned.[/bold green]")
    else:
        console.print(f"[red]Game '{escape(str(title))}' not found.[/red]")

@app.command("unpin")
def cmd_unpin(
    title: str = typer.Argument(..., help="Title of the game to unpin"),
    system: Optional[str] = typer.Option(None, "--system", "-s", help="Game system"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database")
):
    """Unpin the selected game file for a game."""
    if set_game_pin(title=title, system=system, pinned=False, db_path=db_path):
        console.print(f"[bold green]Game '{escape(str(title))}' unpinned.[/bold green]")
    else:
        console.print(f"[red]Game '{escape(str(title))}' not found.[/red]")

@app.command("find")
def cmd_find(
    query: str = typer.Argument(..., help="Title or keyword"),
    system: str = typer.Option(..., "--system", "-s", help="Target system (gba, snes, ps2...)"),
    add: Optional[bool] = typer.Option(
        None,
        "--add/--no-add",
        help="Add the game after selection (default); --no-add for search only",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Automatically pick the best candidate (exact match required)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the selection without adding it"),
    update: bool = typer.Option(False, "--update", help="Replace the game file of an existing game"),
    refresh: bool = typer.Option(True, "--refresh/--no-refresh", help="Refresh the torrent cache if stale"),
    limit: int = typer.Option(10, "--limit", "-n", min=1, max=50, help="Number of candidates shown"),
    full_path: bool = typer.Option(False, "--full-path", help="Show full game file paths"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database"),
):
    """Search for a game and add it after selection (adds by default)."""
    cfg = load_config()
    refresh_info = None
    # In --json mode, never prompt: read-only unless --yes is explicit.
    should_add = (add if add is not None else True) and (not as_json or yes)

    try:
        if refresh and not as_json and console.is_terminal:
            with console.status("[dim]Checking torrent cache…[/dim]"):
                refresh_info = refresh_cache_if_needed(cfg, db_path, refresh=True)
        else:
            refresh_info = refresh_cache_if_needed(cfg, db_path, refresh=refresh)
    except Exception as exc:
        refresh_info = {"ran": False, "error": str(exc)}
        if not as_json:
            console.print(f"[yellow]Refresh failed:[/yellow] {escape(str(exc))}")

    if not as_json and refresh_info.get("ran"):
        console.print(
            f"[dim]Cache refreshed: {refresh_info.get('torrents_count', '?')} torrents, "
            f"{refresh_info.get('files_cached', '?')} files.[/dim]"
        )

    existing = find_existing_game(query, system, db_path=db_path)
    if existing and not update:
        payload = {
            "query": query,
            "system": system,
            "existing": {**existing, "file_name": basename(existing.get("matched_file_name"))},
            "candidates": [],
            "refresh": refresh_info,
        }
        if as_json:
            typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
            return
        console.print(f"[bold green]Already present:[/bold green] {escape(str(existing['title']))} ({existing['state']})")
        console.print(f"  Game file: {escape(basename(existing.get('matched_file_name')) or '')}")
        console.print("[dim]Use --update to change the selected game file.[/dim]")
        return

    candidates = search_candidates(query=query, system=system, config=cfg, db_path=db_path)
    if not candidates:
        message = f"No matching game file found for '{query}' ({system})."
        if as_json:
            typer.echo(json.dumps({"error": message, "refresh": refresh_info}, ensure_ascii=False))
        else:
            console.print(f"[bold red]Error:[/bold red] {escape(str(message))}")
        raise typer.Exit(code=1)

    shown = candidates[:limit]
    if not as_json:
        if existing:
            console.print(
                f"[bold yellow]Replacement:[/bold yellow] {existing['title']} "
                f"({existing['state']}) — current game file: {basename(existing.get('matched_file_name'))}"
            )
        table = Table(title=f"Candidates for '{query}' ({len(candidates)})")
        table.add_column("No.", justify="right", style="cyan")
        table.add_column("Score", justify="right")
        table.add_column("Language", style="magenta")
        table.add_column("Size", justify="right")
        table.add_column("Game File", style="bold white")
        for index, candidate in enumerate(shown, start=1):
            rom_display = candidate["file_name"] if full_path else basename(candidate["file_name"])
            table.add_row(
                str(index),
                str(candidate["score"]),
                candidate.get("lang_label") or "-",
                f"{candidate.get('size_mb', 0):.1f} MB",
                Text(rom_display),
            )
        console.print(table)
        if len(candidates) > len(shown):
            console.print(f"[dim]... and {len(candidates) - len(shown)} more candidates.[/dim]")

    if not should_add:
        if as_json:
            typer.echo(json.dumps({
                "query": query,
                "system": system,
                "existing": existing,
                "candidates": candidates[:limit],
                "refresh": refresh_info,
            }, indent=2, ensure_ascii=False))
        return

    selected = candidates[0] if yes else None
    if yes:
        exact = _title_match_bonus(selected["file_name"], normalize_text(query)) == 3000
        if not exact:
            message = "The best candidate is not an exact match. Rerun without --yes to choose."
            if as_json:
                typer.echo(json.dumps({"error": message, "candidate": selected}, ensure_ascii=False))
            else:
                console.print(f"[bold red]Error:[/bold red] {escape(str(message))}")
            raise typer.Exit(code=1)
        if len(candidates) > 1:
            tie = next(
                (
                    candidate
                    for candidate in candidates[1:]
                    if candidate["score"] == selected["score"]
                    and candidate["match_bonus"] == selected["match_bonus"]
                    and not same_disc_family(selected, candidate)
                ),
                None,
            )
            if tie is not None:
                message = "Two candidates are tied. Rerun without --yes to choose."
                if as_json:
                    typer.echo(json.dumps({"error": message, "candidates": [selected, tie]}, ensure_ascii=False))
                else:
                    console.print(f"[bold red]Error:[/bold red] {escape(str(message))}")
                raise typer.Exit(code=1)
    else:
        choice = typer.prompt("Candidate number to add (q = cancel)", default="1")
        choice = choice.strip()
        if not choice.isdigit():
            console.print("[yellow]Nothing added.[/yellow]")
            return
        index = int(choice)
        if not 1 <= index <= len(shown):
            console.print(f"[bold red]Error:[/bold red] Invalid selection '{escape(str(choice))}'.")
            raise typer.Exit(code=1)
        selected = shown[index - 1]

    if selected["score"] <= -5000:
        message = "The selected candidate is a bad dump / hack / demo. Refused."
        if as_json:
            typer.echo(json.dumps({"error": message, "candidate": selected}, ensure_ascii=False))
        else:
            console.print(f"[bold red]Error:[/bold red] {escape(str(message))}")
        raise typer.Exit(code=1)

    discs = expand_disc_candidates(selected, system, config=cfg, db_path=db_path)
    if not as_json and len(discs) > 1:
        console.print(f"[bold]Multi-disc:[/bold] {len(discs)} discs will be selected.")
        for item in discs:
            console.print(f"  • {escape(basename(item['file_name']) or '')}")

    if dry_run:
        if as_json:
            typer.echo(json.dumps({"dry_run": True, "candidate": selected, "discs": discs, "existing": existing, "refresh": refresh_info}, indent=2, ensure_ascii=False))
        elif len(discs) > 1:
            console.print(f"[bold]Simulated add:[/bold] {escape(str(query))} ({len(discs)} discs)")
        else:
            rom_display = selected["file_name"] if full_path else basename(selected["file_name"])
            console.print(f"[bold]Simulated add:[/bold] {escape(str(rom_display))}")
        return

    title = existing["title"] if existing else query
    try:
        result = add_game(
            title=title,
            system=system,
            selected_candidates=discs,
            config=cfg,
            db_path=db_path,
        )
    except AddGameError as exc:
        if as_json:
            typer.echo(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps({
            "added": result,
            "candidate": selected,
            "existing": existing,
            "refresh": refresh_info,
        }, indent=2, ensure_ascii=False))
        return

    console.print(f"[bold green]Game added:[/bold green] [bold white]{escape(str(result['title']))}[/bold white] ({result['system']})")
    console.print(f"  • Selected game file: {escape(basename(result['matched_file']) or '')}")
    console.print(f"  • Score: {result['score']} ({result['lang_label']}) - {result['size_mb']} MB")
    if result.get("discs", 1) > 1:
        console.print(f"  • Selected discs: {result['discs']}")
    if result.get("download_started"):
        console.print("  • [green]Download started in Transmission.[/green]")
    elif result.get("rpc_error"):
        console.print(
            "  • [yellow]Added to the database, but Transmission could not be updated:[/yellow] "
            f"{result['rpc_error']}"
        )
        console.print("    [dim]Run 'retrobridge sync' to (re)start the download.[/dim]")
    else:
        console.print("    [yellow]Download not started (RPC disabled).[/yellow]")


@app.command("search", hidden=True)
def cmd_search(
    query: str = typer.Argument(..., help="Title or keyword to search"),
    system: str = typer.Option(..., "--system", "-s", help="System (e.g. snes, gba, psx)"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database")
):
    """Search game files in active torrent packs using regional and version scoring."""
    candidates = search_candidates(query=query, system=system, db_path=db_path)

    if as_json:
        typer.echo(json.dumps(candidates, indent=2, ensure_ascii=False))
        return

    if not candidates:
        console.print(f"[yellow]No matching file found for '{escape(str(query))}' in the packs.[/yellow]")
        return

    table = Table(title=f"Candidates found ({len(candidates)}) for '{query}'")
    table.add_column("Score", justify="right", style="cyan")
    table.add_column("Region/Language", style="magenta")
    table.add_column("File found", style="bold white")
    table.add_column("Size (MB)", justify="right")

    for c in candidates[:15]:
        table.add_row(str(c["score"]), c["lang_label"], c["file_name"], str(c["size_mb"]))

    console.print(table)
    if len(candidates) > 15:
        console.print(f"[dim]... and {len(candidates) - 15} more hidden results.[/dim]")

@app.command("add", hidden=True)
def cmd_add(
    title: str = typer.Argument(..., help="Title of the game to add"),
    system: str = typer.Option(..., "--system", "-s", help="Target system"),
    auto_yes: bool = typer.Option(False, "--yes", "-y", help="Automatically select the best candidate"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database")
):
    """Add a game to the collection by selecting the best available game file."""
    try:
        candidates = search_candidates(query=title, system=system, db_path=db_path)
        if not candidates:
            raise AddGameError(f"No matching game file found in the torrent packs for '{title}'.")

        selected = candidates[0]
        if not auto_yes and not as_json and len(candidates) > 1:
            preview = Table(title=f"Candidates for '{title}' ({len(candidates)})")
            preview.add_column("No.", justify="right", style="cyan")
            preview.add_column("Score", justify="right")
            preview.add_column("Region/Language", style="magenta")
            preview.add_column("File", style="bold white")
            shown = candidates[:10]
            for i, c in enumerate(shown, start=1):
                preview.add_row(str(i), str(c["score"]), c["lang_label"], c["file_name"])
            console.print(preview)
            if len(candidates) > len(shown):
                console.print(f"[dim]... and {len(candidates) - len(shown)} more hidden results.[/dim]")

            choice = typer.prompt("Candidate number to add", default="1")
            try:
                idx = int(choice)
                if not 1 <= idx <= len(shown):
                    raise ValueError
            except ValueError:
                raise AddGameError(f"Invalid selection: '{choice}'.")
            selected = shown[idx - 1]

        res = add_game(
            title=title,
            system=system,
            selected_candidates=expand_disc_candidates(selected, system, db_path=db_path),
            db_path=db_path,
        )
    except AddGameError as e:
        if as_json:
            typer.echo(json.dumps({"error": str(e)}))
        else:
            console.print(f"[bold red]Error:[/bold red] {escape(str(e))}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return

    console.print(f"[bold green]Game added:[/bold green] [bold white]{escape(str(res['title']))}[/bold white] ({res['system']})")
    console.print(f"  • Selected game file: {escape(basename(res['matched_file']) or '')}")
    console.print(f"  • Score: {res['score']} ({res['lang_label']}) - {res['size_mb']} MB")
    if res.get("discs", 1) > 1:
        console.print(f"  • Selected discs: {res['discs']}")
    if res.get("download_started"):
        console.print("  • [green]Download started in Transmission.[/green]")
    elif res.get("rpc_error"):
        console.print(
            "  • [yellow]Added to the database, but Transmission could not be updated:[/yellow] "
            f"{res['rpc_error']}"
        )
        console.print("    [dim]Run 'retrobridge sync' to (re)start the download.[/dim]")

@app.command("export")
def cmd_export(
    target: str = typer.Argument(..., help="Name of the configured target (SD card)"),
    system: Optional[str] = typer.Option(None, "--system", "-s", help="Filter by system"),
    tier: Optional[int] = typer.Option(None, "--tier", "-t", help="Filter by tier (1=classic 2D, 2=early 3D, 3=PS2 and up)"),
    raw: bool = typer.Option(False, "--raw", help="Copy raw files without format conversion"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate the export without copying"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="No output except errors"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database")
):
    """Incrementally export the collection to a target (SD card)."""
    try:
        res = export_to_target(
            target_name=target,
            system=system,
            tier=tier,
            raw=raw,
            dry_run=dry_run,
            db_path=db_path
        )
    except ExportError as e:
        if as_json:
            typer.echo(json.dumps({"error": str(e)}))
        elif not quiet:
            console.print(f"[bold red]Export error:[/bold red] {escape(str(e))}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return
    if quiet:
        return

    mode = "[yellow](DRY RUN)[/yellow] " if res["dry_run"] else ""
    console.print(f"{mode}[bold green]Export to '{res['target']}' complete.[/bold green]")
    console.print(f"  • Profile: {res['profile']} (UUID: {res['uuid'][:8]}...)")
    console.print(f"  • Games exported / converted: [bold cyan]{res['exported']}[/bold cyan]")
    console.print(f"  • Games skipped (already up to date): [dim]{res['skipped']}[/dim]")

    extra = res.get("destination_only") or []
    if extra:
        console.print(
            f"\n[bold cyan]Info:[/bold cyan] {len(extra)} file(s) present only on the target "
            "[dim](not deleted)[/dim]"
        )
        for item in extra[:20]:
            console.print(f"  [dim]• {escape(str(item['system']))} : {escape(str(item['file']))}[/dim]")
        if len(extra) > 20:
            console.print(f"  [dim]... and {len(extra) - 20} more.[/dim]")

    if res["errors"]:
        console.print("[bold red]Errors encountered:[/bold red]")
        for err in res["errors"]:
            console.print(f"  [red]! {escape(str(err))}[/red]")
