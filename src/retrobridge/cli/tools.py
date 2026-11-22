import sys
import typer
from typing import Optional
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from pathlib import Path
import json
import shutil

from retrobridge.storage.db import init_db
from retrobridge.storage.legacy import import_legacy_db
from retrobridge.storage.migrations import migrate_db, backup_db
from retrobridge.services.scan import scan_torrents
from retrobridge.services.apply import apply_selection
from retrobridge.services.cleanup import build_cleanup_report, apply_cleanup
from retrobridge.cli.formatting import format_size
from retrobridge.profiles.loader import list_profiles, load_profile
from retrobridge.adapters.transmission import TransmissionClient
from retrobridge.services.export import prepare_export
from retrobridge.config import load_config


app = typer.Typer(help="Utility and advanced commands")
db_app = typer.Typer(help="Database management")
app.add_typer(db_app, name="db")
console = Console()


def _transmission_client() -> TransmissionClient:
    """Build a Transmission client from the user configuration."""
    cfg = load_config()
    return TransmissionClient(
        rpc_url=cfg.transmission.rpc_url,
        username=cfg.transmission.user,
        password=cfg.transmission.password,
        timeout=cfg.transmission.timeout,
    )

@db_app.command("init")
def db_init(
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database")
):
    """Initialize the SQLite database schema."""
    init_db(db_path)
    console.print("[green]Database initialized successfully.[/green]")

@db_app.command("import-legacy")
def db_import_legacy(
    legacy_db: str = typer.Argument(..., help="Path to the legacy roms_manager.db database"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the v2 database")
):
    """Import data from the legacy roms_manager.db database."""
    stats = import_legacy_db(legacy_db_path=legacy_db, target_db_path=db_path)
    console.print(f"[bold green]Import complete:[/bold green] {stats['games_inserted']} games, {stats['cache_rows_copied']} cache entries.")

@db_app.command("migrate")
def db_migrate(
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show changes without applying them"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
):
    """Align data with canonical system codes (removes historical aliases)."""
    res = migrate_db(db_path=db_path, dry_run=dry_run)

    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return

    mode = "[yellow](DRY RUN)[/yellow] " if dry_run else ""
    console.print(f"{mode}[bold green]Migration complete.[/bold green]")
    console.print(f"  • Systems renamed: {len(res['renamed'])}")
    console.print(f"  • Duplicates merged: {len(res['merged'])}")
    for item in res["renamed"]:
        console.print(f"    [dim]-> {escape(str(item['title']))} : {escape(str(item['from']))} -> {escape(str(item['to']))}[/dim]")
    for item in res["merged"]:
        console.print(f"    [dim]-> {escape(str(item['title']))} : duplicate {item['dropped_id']} merged into {item['kept_id']}[/dim]")
    if res["unresolved"]:
        console.print("[bold yellow]Unrecognized systems (fix manually):[/bold yellow]")
        for item in res["unresolved"]:
            console.print(f"    [yellow]! {escape(str(item['title']))} ({escape(str(item['system']))})[/yellow]")


@db_app.command("backup")
def db_backup(
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database"),
    destination: Optional[str] = typer.Option(None, "--dest", "-d", help="Destination file or directory"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
):
    """Create a timestamped copy of the SQLite database."""
    try:
        dest = backup_db(db_path=db_path, destination=destination)
    except Exception as e:
        if as_json:
            typer.echo(json.dumps({"error": str(e)}))
        else:
            console.print(f"[bold red]Error:[/bold red] {escape(str(e))}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps({"backup": str(dest)}, indent=2, ensure_ascii=False))
        return

    console.print(f"[bold green]Backup created:[/bold green] {escape(str(dest))}")

@app.command("apply")
def tools_apply(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show changes without applying them"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database"),
):
    """Align Transmission priorities: only files from selected games stay wanted."""
    try:
        res = apply_selection(db_path=db_path, dry_run=dry_run)
    except Exception as e:
        if as_json:
            typer.echo(json.dumps({"error": str(e)}))
        else:
            console.print(f"[bold red]Error:[/bold red] {escape(str(e))}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return

    mode = "[yellow](DRY RUN)[/yellow] " if dry_run else ""
    console.print(f"{mode}[bold green]Selection applied.[/bold green]")
    console.print(f"  • Torrents modified: {res['torrents_processed']}")
    console.print(f"  • Files enabled: {res['files_wanted']}")
    console.print(f"  • Files unselected: {res['files_unwanted']}")
    if res.get("verifications"):
        console.print(f"  • Verifications requested: {len(res['verifications'])} torrent(s)")
    for err in res["errors"]:
        console.print(f"  [red]! {escape(str(err))}[/red]")


@app.command("scan")
def tools_scan(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="No output except errors"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database"),
):
    """Scan active torrents in Transmission and refresh the cache."""
    def progress(stage: str, current: int, total: int) -> None:
        if quiet or as_json or not console.is_terminal:
            return
        suffix = f" ({current}/{total})" if total else ""
        if console.is_terminal:
            status.update(f"{stage}{suffix}")

    try:
        if quiet or as_json or not console.is_terminal:
            res = scan_torrents(db_path=db_path, progress=progress)
        else:
            with console.status("[dim]Scanning Transmission…[/dim]") as live:
                status = live
                res = scan_torrents(db_path=db_path, progress=progress)
    except Exception as e:
        if as_json:
            typer.echo(json.dumps({"error": str(e)}, ensure_ascii=False))
        elif not quiet:
            console.print(f"[bold red]Error during scan:[/bold red] {escape(str(e))}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return
    if quiet:
        return
    console.print(
        f"[green]Scan complete:[/green] {res['torrents_count']} torrents, "
        f"{res['files_cached']} files cached, "
        f"{res['changed_torrents']} changed, {res['unchanged_torrents']} unchanged "
        f"in {res['duration_ms']} ms."
    )

@app.command("profiles")
def tools_profiles():
    """List available frontend profiles."""
    profiles = list_profiles()
    console.print("[bold]Available profiles:[/bold]")
    for p_name in profiles:
        prof = load_profile(p_name)
        console.print(f"  • [cyan]{prof.name}[/cyan] ({prof.display_name}) - {len(prof.systems)} systems")

@app.command("bios")
def tools_bios():
    """Offer BIOS installation via RetroBios (external tool)."""
    executable = shutil.which("retrobios") or shutil.which("RetroBios")
    console.print("[bold]BIOS:[/bold] RetroBridge does not duplicate the RetroBios catalog.")
    if executable:
        console.print(f"  • RetroBios detected: [cyan]{executable}[/cyan]")
        console.print("  • Run `retrobios` to choose, download and verify BIOS files.")
    else:
        console.print("  • RetroBios is not installed.")
        console.print("  • Project: [cyan]https://github.com/Abdess/retrobios[/cyan]")


def _torrent_action(torrent_id: int, action: str, dry_run: bool, as_json: bool) -> None:
    """Run a single Transmission action (pause, resume, verify)."""
    labels = {
        "pause": ("paused", "stop_torrent"),
        "resume": ("resumed", "start_torrent"),
        "verify": ("verified", "verify_torrent"),
    }
    done_label, method = labels[action]

    client = _transmission_client()
    if not dry_run:
        try:
            getattr(client, method)(torrent_id)
        except Exception as exc:
            if as_json:
                typer.echo(json.dumps({"error": str(exc), "torrent_id": torrent_id}, ensure_ascii=False))
            else:
                console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
            raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps({
            "torrent_id": torrent_id,
            "action": action,
            "dry_run": dry_run,
        }, indent=2))
    else:
        console.print(f"[bold green]Torrent {torrent_id} {done_label}.[/bold green]")


@app.command("pause")
def tools_pause(
    torrent_id: int = typer.Argument(..., help="Torrent ID to pause"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the action without running it"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
):
    """Pause a torrent in Transmission."""
    _torrent_action(torrent_id, "pause", dry_run, as_json)

@app.command("resume")
def tools_resume(
    torrent_id: int = typer.Argument(..., help="Torrent ID to resume"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the action without running it"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
):
    """Resume a torrent in Transmission."""
    _torrent_action(torrent_id, "resume", dry_run, as_json)

@app.command("verify")
def tools_verify(
    torrent_id: Optional[int] = typer.Argument(None, help="Torrent ID to verify (no argument: torrents in error)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the action without running it"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
):
    """Verify a torrent, or all torrents reported in error if the ID is omitted."""
    if torrent_id is not None:
        _torrent_action(torrent_id, "verify", dry_run, as_json)
        return

    client = _transmission_client()
    try:
        data = client.request(
            "torrent-get",
            {"fields": ["id", "name", "error", "errorString", "status", "percentDone"]},
        ).get("arguments", {}).get("torrents", [])
    except Exception as exc:
        if as_json:
            typer.echo(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            console.print(f"[bold red]Transmission unreachable:[/bold red] {escape(str(exc))}")
        raise typer.Exit(code=1)

    suspects = []
    for torrent in data:
        error_code = int(torrent.get("error") or 0)
        error_text = (torrent.get("errorString") or "").lower()
        if error_code in (2, 3) or "verify local data" in error_text or "corrupt" in error_text:
            suspects.append(torrent)

    results = []
    for torrent in suspects:
        entry = {
            "torrent_id": torrent["id"],
            "name": torrent.get("name"),
            "error": torrent.get("error"),
            "error_string": torrent.get("errorString"),
            "dry_run": dry_run,
        }
        if not dry_run:
            try:
                client.verify_torrent(torrent["id"])
                entry["verified"] = True
            except Exception as exc:
                entry["verified"] = False
                entry["error_string"] = f"{entry.get('error_string') or ''} {exc}".strip()
        results.append(entry)

    if as_json:
        typer.echo(json.dumps(results, indent=2, ensure_ascii=False))
        return
    if not results:
        console.print("[green]No torrents reported in error.[/green]")
        return

    for entry in results:
        state = "verification started" if entry.get("verified") else ("dry-run" if dry_run else "failure")
        console.print(
            f"[cyan]#{entry['torrent_id']}[/cyan] {entry['name']} — "
            f"{entry.get('error_string') or 'Transmission error'} ({state})"
        )

def _interactive_confirm(message: str) -> bool:
    """Asks y/N only on a real terminal (never hangs in scripts/systemd)."""
    if not sys.stdin.isatty():
        return False
    return typer.confirm(message, default=False)


def _print_cleanup_report(report: dict, partial_only: bool) -> None:
    refresh = report.get("refresh") or {}
    if refresh.get("ran"):
        console.print(
            f"[dim]Transmission refreshed: {refresh.get('torrents', '?')} torrents, "
            f"{refresh.get('changed', 0)} changed.[/dim]"
        )
    elif refresh.get("error"):
        console.print(
            "[bold yellow]⚠ Transmission unreachable:[/bold yellow] "
            f"[dim]{escape(str(refresh['error']))} — state from the last scan.[/dim]"
        )

    totals = report["totals"]
    table = Table(title="État des lieux", expand=True)
    table.add_column("Catégorie", style="cyan", no_wrap=True)
    table.add_column("Éléments", justify="right", no_wrap=True)
    table.add_column("Taille réelle", justify="right", no_wrap=True)

    table.add_row("Jeux orphelins (base)", str(totals["games_to_purge"]), "—")
    table.add_row(
        "Fichiers complets non voulus",
        str(totals["complete_files"]),
        format_size(totals["complete_bytes"]),
    )
    table.add_row(
        "Fichiers .part non voulus",
        str(totals["partial_files"]),
        format_size(totals["partial_bytes"]),
    )
    table.add_row(
        "Fichiers hors pack",
        str(totals["orphan_files"]),
        format_size(totals["orphan_bytes"]),
    )
    table.add_row(
        "[bold]Total récupérable[/bold]",
        "",
        f"[bold]{format_size(totals['reclaimable_bytes'])}[/bold]",
    )
    console.print(table)

    if report.get("missing_on_disk"):
        console.print(
            f"[bold yellow]⚠ {len(report['missing_on_disk'])} jeu(x) lié(s) absent(s) du disque[/bold yellow] "
            "[dim]— relancer `retrobridge sync` (clean ne les supprime pas)[/dim]"
        )
        for game in report["missing_on_disk"][:5]:
            console.print(
                f"    [dim]· {escape(str(game['system']))} | {escape(str(game['title']))}[/dim]"
            )

    if report.get("duplicate_paths"):
        console.print(
            f"[bold yellow]⚠ {len(report['duplicate_paths'])} chemin(s) en doublon[/bold yellow] "
            "[dim]— à examiner manuellement[/dim]"
        )
        for duplicate in report["duplicate_paths"][:5]:
            console.print(
                f"    [dim]· {escape(Path(duplicate['matched_file_path']).name)} "
                f"({duplicate.get('c', '?')} jeux : {escape(str(duplicate.get('titles') or ''))})[/dim]"
            )

    if not partial_only and report["totals"]["complete_files"]:
        console.print(
            "[dim]Les fichiers complets supprimés ne seront plus partagés (seeding) et devront "
            "être re-téléchargés si tu les veux plus tard. Utilise --partial-only pour les conserver.[/dim]"
        )
    if partial_only:
        console.print("[dim]Mode partiels : les fichiers complets sont conservés (seeding).[/dim]")


@app.command("clean")
def tools_clean(
    apply_changes: bool = typer.Option(
        False, "--apply", "-y", help="Apply without asking for confirmation"
    ),
    partial_only: bool = typer.Option(
        False, "--partial-only", help="Only remove .part files (keep complete files for seeding)"
    ),
    no_games: bool = typer.Option(
        False, "--no-games", help="Do not purge orphan games from the database"
    ),
    no_disk_scan: bool = typer.Option(
        False, "--no-disk-scan", help="Skip scanning files outside indexed packs"
    ),
    no_refresh: bool = typer.Option(
        False, "--no-refresh", help="Do not refresh Transmission file stats first"
    ),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database"),
):
    """Audit the collection and reclaim disk space: report first, then y/N."""
    cfg = load_config()
    report = build_cleanup_report(
        config=cfg,
        db_path=db_path,
        scan_disk=not no_disk_scan,
        refresh=not no_refresh,
    )

    actionable = bool(
        report["games_to_purge"]
        or report["partial_unwanted"]
        or report["orphan_files"]
        or (report["complete_unwanted"] and not partial_only)
    )

    if not as_json:
        _print_cleanup_report(report, partial_only=partial_only)

    if not actionable:
        if as_json:
            typer.echo(json.dumps({**report, "applied": None}, indent=2, ensure_ascii=False))
        else:
            console.print("[bold green]Rien à nettoyer.[/bold green]")
        return

    should_apply = apply_changes
    if not should_apply and not as_json:
        reclaimable = report["totals"]["reclaimable_bytes"]
        if partial_only:
            reclaimable = report["totals"]["partial_bytes"] + report["totals"]["orphan_bytes"]
        should_apply = _interactive_confirm(
            f"Appliquer le nettoyage ({format_size(reclaimable)} récupérables) ?"
        )

    if not should_apply:
        if as_json:
            typer.echo(json.dumps({**report, "applied": None}, indent=2, ensure_ascii=False))
        else:
            console.print("[dim]Aucune modification. Relance avec --apply pour exécuter.[/dim]")
        return

    result = apply_cleanup(
        report,
        db_path=db_path,
        include_complete=not partial_only,
        purge_games=not no_games,
    )

    if as_json:
        typer.echo(json.dumps({**report, "applied": result}, indent=2, ensure_ascii=False))
        return

    purged = (
        f", {result['purged_games']} jeu(x) orphelin(s) purgé(s)"
        if result["purged_games"]
        else ""
    )
    console.print(
        f"[bold green]Nettoyage terminé :[/bold green] {result['deleted_files']} fichier(s), "
        f"{format_size(result['freed_bytes'])} récupérés{purged}."
    )
    for err in result["errors"]:
        console.print(f"  [red]! {escape(err)}[/red]")
    if result["errors"]:
        raise typer.Exit(code=1)


@app.command("relocate")
def tools_relocate(
    torrent_id: int = typer.Argument(..., help="Torrent ID to relocate"),
    new_dir: str = typer.Argument(..., help="New download directory for the torrent"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the action without moving files"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
):
    """Relocate a torrent: Transmission moves the files then updates its location."""
    target = Path(new_dir).expanduser().resolve()

    if dry_run:
        if as_json:
            typer.echo(json.dumps({"dry_run": True, "torrent_id": torrent_id, "location": str(target)}))
        else:
            console.print(f"[dim](DRY RUN)[/dim] Torrent {torrent_id} would be moved to {escape(str(target))}.")
        return

    try:
        target.mkdir(parents=True, exist_ok=True)
        # `move: true` lets Transmission move data consistently
        # with its own state instead of manipulating files behind its back.
        _transmission_client().request("torrent-set-location", {
            "ids": [torrent_id],
            "location": str(target),
            "move": True,
        })
    except Exception as e:
        if as_json:
            typer.echo(json.dumps({"error": str(e), "torrent_id": torrent_id}))
        else:
            console.print(f"[bold red]Error:[/bold red] {escape(str(e))}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps({"torrent_id": torrent_id, "location": str(target)}, indent=2))
    else:
        console.print(f"[bold green]Torrent {torrent_id} relocated to {escape(str(target))}.[/bold green]")

@app.command("prepare-export")
def tools_prepare_export(
    system: Optional[str] = typer.Option(None, "--system", "-s", help="Filter by system"),
    tier: Optional[int] = typer.Option(None, "--tier", "-t", help="Filter by tier (1=classic 2D, 2=early 3D, 3=PS2 and up)"),
    raw: bool = typer.Option(False, "--raw", help="Do not convert anything (raw copy, so no preparation)"),
    profile: str = typer.Option("esde", "--profile", "-p", help="Frontend profile used for conversion rules"),
    dry_run: bool = typer.Option(False, "--dry-run", help="List conversions without running them"),
    as_json: bool = typer.Option(False, "--json", help="Raw JSON output"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Alternative path to the database"),
):
    """Convert files to the export format in local staging, without copying to a card."""
    try:
        result = prepare_export(
            system=system,
            tier=tier,
            raw=raw,
            dry_run=dry_run,
            profile_name=profile,
            db_path=db_path,
        )
    except Exception as e:
        if as_json:
            typer.echo(json.dumps({"error": str(e)}))
        else:
            console.print(f"[bold red]Error:[/bold red] {escape(str(e))}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(result, indent=2, ensure_ascii=False))
        return

    mode = "[yellow](DRY RUN)[/yellow] " if dry_run else ""
    console.print(f"{mode}[bold green]Export preparation complete.[/bold green]")
    console.print(f"  • Profile: {result['profile']}")
    console.print(f"  • Files prepared: {result['prepared']}")
    console.print(f"  • Files skipped (direct copy): {result['skipped']}")
    console.print(f"  • Staging: {escape(str(result['staging_dir']))}")
    for err in result["errors"]:
        console.print(f"  [red]! {escape(str(err))}[/red]")
