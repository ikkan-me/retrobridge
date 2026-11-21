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
from retrobridge.services.find import expand_disc_candidates, find_existing_game, refresh_cache_if_needed
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

app = typer.Typer(help="RetroBridge v2 - Gestionnaire et pont de collection de ROMs")
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
        help="Affiche la version et quitte.",
    ),
) -> None:
    """RetroBridge v2 — gestion et pont de collection de ROMs."""

# Sub-application for pack commands
pack_app = typer.Typer(help="Commandes de gestion de packs de ROMs (torrents)")
app.add_typer(pack_app, name="pack")


@pack_app.command("add")
def cmd_pack_add(
    torrent_file: str = typer.Argument(..., help="Chemin vers le fichier .torrent à ajouter"),
    keep_all: bool = typer.Option(False, "--keep-all", help="Conserver tous les fichiers du pack sélectionnés"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
    config_path: Optional[str] = typer.Option(None, "--config", help="Chemin vers le fichier de configuration"),
):
    """Ajoute un pack de ROMs en pause et désélectionne tous ses fichiers."""

    cfg = load_config(config_path)
    torrent_path = Path(torrent_file).expanduser().resolve()
    if not torrent_path.is_file():
        if as_json:
            typer.echo(json.dumps({"error": f"Le fichier torrent '{torrent_file}' n'existe pas."}))
        else:
            console.print(f"[bold red]Erreur :[/bold red] Le fichier torrent '{torrent_file}' n'existe pas.")
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
            console.print(f"[bold red]Erreur lors de l'ajout du torrent :[/bold red] {e}")
        raise typer.Exit(code=1)

    deselected = 0
    if not keep_all:
        try:
            deselected = client.select_nothing(torrent_id)
        except Exception as e:
            console.print(f"[yellow]Avertissement : impossible de désélectionner les fichiers :[/yellow] {e}")

    res = {
        "torrent_id": torrent_id,
        "file": str(torrent_path),
        "paused": True,
        "deselected_files": deselected,
    }
    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return

    console.print(f"[bold green]Pack ajouté (en pause) avec l'ID {torrent_id}.[/bold green]")
    console.print(f"  • Fichier : {torrent_path}")
    if deselected:
        console.print(f"  • Fichiers désélectionnés : {deselected} (aucun téléchargement inutile)")
    console.print("[dim]Lancez 'retrobridge tools scan' pour l'indexer, puis 'retrobridge add' pour sélectionner les jeux.[/dim]")


STEPS = [
    ("paths", "Configuration des chemins et de la destination"),
    ("transmission", "Configuration de la connexion Transmission (hôte et port RPC)"),
    ("scoring", "Configuration des préférences de scoring (région et langues)"),
    ("automation", "Automatisation (timer de synchronisation)"),
]


def _run_setup_step(step_name: str, cfg) -> None:
    """Exécute une étape interactive de l'assistant de configuration."""

    if step_name == "paths":
        dl_input = typer.prompt("Répertoire des téléchargements (Download-Dir)", default=str(cfg.paths.download_dir))
        lib_input = typer.prompt("Répertoire de la bibliothèque ROMs (Library-Dir)", default=str(cfg.paths.library_dir))
        cfg.paths.download_dir = Path(dl_input)
        cfg.paths.library_dir = Path(lib_input)

        if cfg.targets:
            console.print(
                "[dim]Cibles déjà configurées : "
                + ", ".join(f"{name} ({t.profile})" for name, t in cfg.targets.items())
                + "[/dim]"
            )
        else:
            profile = typer.prompt(
                "Profil de destination (esde, knulli ou aucun)",
                default="esde",
            ).strip().lower()
            if profile not in {"aucun", "none", ""}:
                mount = typer.prompt(
                    "Point de montage de la carte SD",
                    default=str(Path.home() / "ROMs"),
                )
                cfg.targets["sd"] = TargetConfig(
                    name="sd",
                    profile=profile,
                    mount_path=Path(mount).expanduser(),
                )
    elif step_name == "transmission":
        cfg.transmission.host = typer.prompt("Hôte RPC Transmission", default=cfg.transmission.host)
        cfg.transmission.port = int(typer.prompt("Port RPC Transmission", default=str(cfg.transmission.port)))
    elif step_name == "scoring":
        cfg.scoring.region_preference = typer.prompt(
            "Préférence de région [NTSC/PAL]", default=cfg.scoring.region_preference
        ).upper()
        langs_default = ",".join(cfg.scoring.preferred_languages or ["fr", "en"])
        langs_input = typer.prompt(
            "Langues préférées (codes séparés par des virgules, par ordre d'importance)",
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
            "Installer le timer de synchronisation automatique (10 min) ?",
            default=True,
        ):
            console.print("[dim]Timer non installé. Tu pourras relancer 'retrobridge setup --step 4'.[/dim]")
            return
        res = install_timer()
        if res.get("systemctl_ok"):
            status = res.get("status", {})
            next_run = status.get("next_run") or "?"
            console.print(f"[green]Timer installé.[/green] Prochaine synchro : {next_run}")
        else:
            console.print(
                "[yellow]Units écrites, mais systemd --user n'a pas pu les activer.[/yellow]\n"
                f"[dim]Vérifie : {res['service_path']} et {res['timer_path']}[/dim]"
            )

@app.command("setup")
def cmd_setup(
    check: bool = typer.Option(False, "--check", "-c", help="Vérifie uniquement les prérequis et l'environnement"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
    config_path: Optional[str] = typer.Option(None, "--config", help="Chemin vers le fichier de configuration"),
    step: Optional[int] = typer.Option(None, "--step", help="Exécute uniquement l'étape numérotée N (1 à 4)"),
    timer: Optional[bool] = typer.Option(
        None,
        "--timer/--no-timer",
        help="Installer (--timer) ou non (--no-timer) le timer de sync ; par défaut, demande en interactif",
    ),
    uninstall: bool = typer.Option(False, "--uninstall-timer", help="Désinstalle le timer de synchronisation"),
):
    """Vérifie l'environnement et configure RetroBridge."""
    cfg = load_config(config_path)

    if uninstall:
        result = uninstall_timer()
        if as_json:
            typer.echo(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            console.print("[green]Timer désinstallé.[/green]")
        return

    if check:
        report = check_environment(cfg)
        if as_json:
            typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
            return

        table = Table(title="Diagnostic de l'environnement RetroBridge")
        table.add_column("Composant", style="cyan")
        table.add_column("État", justify="center")
        table.add_column("Détail", style="white")

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
        table.add_row("Outil: chdman", status_badge(chd["status"]), chd["message"])
        dol = report["tools"]["dolphin_tool"]
        table.add_row("Outil: dolphin-tool", status_badge(dol["status"]), dol["message"])
        # Paths
        dl = report["paths"]["download_dir"]
        table.add_row("Download-Dir", status_badge(dl["status"]), f"{dl['path']} ({dl['free_gb']} Go libres)")
        lib = report["paths"]["library_dir"]
        table.add_row("Library-Dir", status_badge(lib["status"]), f"{lib['path']} ({lib['free_gb']} Go libres)")
        fs = report["paths"]["same_filesystem"]
        table.add_row("Partition Hardlinks", status_badge(fs["status"]), fs["message"])
        # Sync timer
        timer_info = report.get("timer", {})
        if timer_info.get("installed"):
            timer_state = "actif" if timer_info.get("active") else "inactif"
            timer_detail = f"{timer_state}"
            if timer_info.get("next_run"):
                timer_detail += f" — prochaine : {timer_info['next_run']}"
            table.add_row("Timer sync", status_badge("ok" if timer_info.get("active") else "warn"), timer_detail)
        else:
            table.add_row(
                "Timer sync",
                status_badge("warn"),
                "non installé — lance 'retrobridge setup --step 4'",
            )

        console.print(table)
        if not report["overall_ok"]:
            console.print("[bold yellow]Avertissement : certains composants ou répertoires requièrent une attention.[/bold yellow]")
        else:
            console.print("[bold green]Tous les prérequis fondamentaux sont validés ![/bold green]")
        return

    # Interactive configuration mode
    console.print(Panel("[bold cyan]Assistant de configuration RetroBridge v2[/bold cyan]\nConfiguration de vos répertoires et de la connexion Transmission."))

    # If --step is specified, run only that step
    if step is not None:
        if step < 1 or step > len(STEPS):
            console.print(f"[bold red]Erreur :[/bold red] L'étape doit être comprise entre 1 et {len(STEPS)}.")
            raise typer.Exit(code=1)

        step_name, step_description = STEPS[step - 1]
        console.print(f"\n[bold cyan]Étape {step}/{len(STEPS)} : {step_description}[/bold cyan]")
        _run_setup_step(step_name, cfg)
        console.print(f"[green]Étape '{step_name}' mise à jour.[/green]")

        initialize_directories(cfg)
        save_config(cfg, config_path)
        init_db()
        console.print("[bold green]Configuration enregistrée avec succès ![/bold green]")
        return

    # By default, run all steps
    for i, (step_name, step_description) in enumerate(STEPS, start=1):
        if step_name == "automation":
            if timer is False:
                console.print("\n[dim]Timer non installé (--no-timer).[/dim]")
                continue
            if timer is True:
                console.print(f"\n[bold cyan]Étape {i}/{len(STEPS)} : {step_description}[/bold cyan]")
                res = install_timer()
                if res.get("systemctl_ok"):
                    console.print(f"[green]Timer installé.[/green] Prochaine synchro : {res.get('status', {}).get('next_run') or '?'}")
                else:
                    console.print("[yellow]Units écrites, activation systemd impossible.[/yellow]")
                continue
        console.print(f"\n[bold cyan]Étape {i}/{len(STEPS)} : {step_description}[/bold cyan]")
        _run_setup_step(step_name, cfg)

    initialize_directories(cfg)
    save_config(cfg, config_path)
    init_db()
    console.print("[bold green]Configuration enregistrée avec succès ![/bold green]")
    console.print("Prochaines étapes recommandées :")
    console.print("  • [cyan]retrobridge setup --check[/cyan] pour vérifier l'état")
    console.print("  • [cyan]retrobridge tools scan[/cyan] pour indexer les torrents actifs")
    console.print("  • [cyan]retrobridge status[/cyan] pour voir la vue d'ensemble")
    console.print("  • [cyan]retrobridge --install-completion[/cyan] pour activer la complétion du shell")


@app.command("list")
def cmd_list(
    system: Optional[str] = typer.Option(None, "--system", "-s", help="Filtrer par système"),
    state: Optional[str] = typer.Option(None, "--state", help="Filtrer par état (downloading, downloaded, linked)"),
    sort: str = typer.Option("system", "--sort", help="Tri : system (défaut), title, state, score"),
    wide: bool = typer.Option(False, "--wide", "-w", help="Afficher les titres et ROM complets (peut passer sur plusieurs lignes)"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base")
):
    """Liste les jeux de la collection et leur état actuel."""
    cfg = load_config()
    games = list_games(system=system, state=state, db_path=db_path, download_dir=cfg.paths.download_dir)

    if as_json:
        typer.echo(json.dumps(games, indent=2, ensure_ascii=False))
        return

    if not games:
        console.print("[yellow]Aucun jeu trouvé.[/yellow]")
        return

    sort_key = sort.strip().lower()
    if sort_key not in {"system", "title", "state", "score"}:
        console.print(f"[bold red]Erreur :[/bold red] tri inconnu '{sort}'. Utilise system, title, state ou score.")
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
        console.print(f"[bold]Collection RetroBridge[/bold] — {len(games)} jeux")
        for sys_name, group in groupby(games, key=lambda g: g["system"] or "?"):
            group_list = list(group)
            console.rule(f"[bold cyan]{sys_name}[/bold cyan] ({len(group_list)})", style="cyan")
            _print_list_table(group_list, wide=wide, show_system=False)
    else:
        console.print(f"[bold]Collection RetroBridge[/bold] — {len(games)} jeux (tri : {sort_key})")
        _print_list_table(games, wide=wide, show_system=True)

    console.print(
        "[dim]✅ lié · 🟡 en attente/partiel · 📥 en cours · ⏳ en file · "
        "❌ cassé · 🗑️ suppression[/dim]"
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
        table.add_column("Système", style="cyan", no_wrap=True, min_width=9)
    table.add_column("Titre", style="bold white", no_wrap=not wide, overflow="ellipsis" if not wide else "fold", ratio=6)
    table.add_column("État", justify="center", no_wrap=True, width=4)
    table.add_column("Score", justify="right", no_wrap=True, width=5)
    table.add_column("Langue", style="magenta", no_wrap=True, width=16)
    table.add_column("ROM", style="dim", no_wrap=not wide, overflow="ellipsis" if not wide else "fold", ratio=6)

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
    compact: bool = typer.Option(False, "--compact", "-c", help="Vue synthétique sans le détail des systèmes"),
    refresh: bool = typer.Option(
        True,
        "--refresh/--no-refresh",
        help="Synchronisation automatique (safe) si le cache est périmé ; --no-refresh pour un status strictement en lecture",
    ),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
    no_live: bool = typer.Option(False, "--no-live", help="N'interroge pas Transmission (snapshot local uniquement)"),
    full_path: bool = typer.Option(False, "--full-path", help="Afficher le chemin complet de la ROM au lieu du nom de fichier"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base")
):
    """Affiche la vue globale : états de la collection, santé du système, ventilation par système et téléchargements."""
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
                console.print("[dim]Dernière vérification > 10 min : synchronisation automatique…[/dim]")
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
                    with console.status("[dim]Synchronisation en cours…[/dim]"):
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
                        f"[green]Sync auto (safe) :[/green] {sync_res['newly_downloaded']} téléchargés, "
                        f"{sync_res['links_created_or_verified']} liens vérifiés "
                        f"en {refresh_info['duration_ms']} ms."
                    )
                    for err in sync_res["errors"]:
                        console.print(f"  [yellow]! {err}[/yellow]")
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
                    console.print(f"[yellow]Sync auto impossible :[/yellow] {e}")

    status = get_system_status(db_path=db_path, live=not no_live)
    if refresh_info is not None:
        status["refresh"] = refresh_info

    if as_json:
        typer.echo(json.dumps(status, indent=2, ensure_ascii=False))
        return

    games = status["games"]
    acquired = games.get("acquired", games["downloaded"] + games["linked"])
    pending_link = games.get("downloaded_pending", games["downloaded"])
    pending_suffix = f", {pending_link} en attente de lien" if pending_link else ""
    console.print(Panel(
        f"[bold yellow]En téléchargement:[/bold yellow] {games['downloading']}   "
        f"[bold green]Téléchargés:[/bold green] {acquired} "
        f"[dim](dont liés: {games['linked']}{pending_suffix})[/dim]   "
        f"[bold white](Total: {games['total']})[/bold white]",
        title="[bold]📊 État de la Collection[/bold]"
    ))
    console.print(
        "[dim]Téléchargés = acquis (liés + en attente de lien). "
        "États exclusifs : downloading → downloaded → linked.[/dim]"
    )
    console.print(
        f"[bold]Taille :[/bold] {format_size(games.get('acquired_bytes', 0))} acquis · "
        f"{format_size(games.get('downloading_bytes', 0))} en cours"
    )

    # Snapshot freshness (UTC in DB, local display)
    freshness = status.get("freshness") or {}
    scan_info = freshness.get("scan") or {}
    sync_info = freshness.get("sync") or {}
    if scan_info or sync_info:
        stale_badge = " [bold yellow]\\[périmé][/bold yellow]" if freshness.get("stale") else ""
        scan_at = scan_info.get("at_local") or "?"
        console.print(
            f"[bold]Snapshot :[/bold] scan {scan_info.get('age_label', 'inconnu')} "
            f"({scan_at}) · sync {sync_info.get('age_label', 'inconnu')}{stale_badge}"
        )

    timer_info = status.get("timer") or {}
    if timer_info.get("installed"):
        timer_state = "actif" if timer_info.get("active") else "inactif"
        next_run = timer_info.get("next_run")
        suffix = f" — prochaine : {next_run}" if next_run else ""
        console.print(f"[bold]Sync auto :[/bold] timer {timer_state}{suffix}")
    else:
        console.print("[dim]Sync auto : timer non installé (retrobridge setup --step 4)[/dim]")

    # Per-system table
    if not compact and status.get("systems"):
        table = Table(
            title=f"Ventilation par système ({len(status['systems'])} systèmes configurés)",
            expand=True,
        )
        table.add_column("Système", style="cyan bold", no_wrap=True, min_width=12)
        table.add_column("Total", justify="right", style="bold white", no_wrap=True)
        table.add_column("Téléchargés", justify="right", style="green", no_wrap=True)
        table.add_column("En cours", justify="right", style="bold yellow", no_wrap=True)
        table.add_column("Taille", justify="right", no_wrap=True)
        table.add_column("Complétude", justify="right", no_wrap=True, min_width=18)

        for s in status["systems"]:
            pct = s["progress_pct"]
            acquired = s.get("acquired", s["downloaded"] + s["linked"])
            acquired_cell = str(acquired)
            if s["linked"] > 0:
                acquired_cell += f" ({s['linked']} liés)"
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
            f"[bold red]⚠️ {status['target_issues']} cible(s) cassée(s)[/bold red] "
            "[dim](fichier absent et non sélectionné — voir 'retrobridge sync')[/dim]"
        )

    if status.get("active_downloads"):
        total_active = status.get("active_downloads_total", games["downloading"])
        dl_table = Table(title=f"Téléchargements actifs ({total_active} en cours)", expand=True)
        dl_table.add_column("Système", style="cyan", no_wrap=True, min_width=9)
        dl_table.add_column("Jeu / ROM", style="bold white", ratio=1, no_wrap=True, overflow="ellipsis")
        dl_table.add_column("État", justify="center", no_wrap=True, width=4)
        dl_table.add_column("Taille", justify="right", no_wrap=True, width=16)
        dl_table.add_column("Progression", justify="right", no_wrap=True, width=18)

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
                title = f"{title} ({discs_total} disques)"
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
            "[dim]⏳ en file · 📥 en cours · ✅ complet · ❌ cassé · "
            "🟡 partiel/orphelin — « cassé » = absent + non sélectionné.[/dim]"
        )
        if total_active > len(status["active_downloads"]):
            console.print(f"[dim]... et {total_active - len(status['active_downloads'])} autres en cours d'acquisition.[/dim]")

    # Transmission & Storage
    t_info = status["transmission"]
    if t_info.get("live") is False:
        t_status = "[dim]live désactivé (--no-live)[/dim]"
    elif t_info["reachable"]:
        rate_dl_kb = round(t_info.get("download_rate_bytes", 0) / 1024, 1)
        rate_up_kb = round(t_info.get("upload_rate_bytes", 0) / 1024, 1)
        speed_str = f" | Débit: [bold green]↓ {rate_dl_kb} Ko/s[/bold green] [bold cyan]↑ {rate_up_kb} Ko/s[/bold cyan]"
        ratio_str = f" | Ratio global: [bold]{t_info.get('ratio', '-')}[/bold]"
        torrents_str = f" | Torrents: [bold]{t_info.get('torrents_active', 0)}/{t_info.get('torrents_total', 0)}[/bold] actifs"
        t_status = f"[green]Connecté[/green]{speed_str}{ratio_str}{torrents_str}"
    else:
        t_status = "[red]Injoignable[/red]"
        if status.get("torrent_cache_size"):
            t_status += " [dim](RPC lent ou occupé — état issu du dernier scan)[/dim]"

    console.print(f"\n[bold]Transmission :[/bold] {t_status}")

    # Gap between what Transmission requests and what is actually selected
    sel = status.get("selection", {})
    wanted_bytes = sel.get("wanted_bytes", 0)
    selected_bytes = sel.get("selected_bytes", 0)
    wanted_files = sel.get("wanted_files", 0)
    if wanted_bytes > max(selected_bytes * 1.5, 10 * 1024**3):
        wanted_tb = round(wanted_bytes / 1024**4, 2)
        selected_gb = round(selected_bytes / 1024**3, 1)
        console.print(
            f"[bold yellow]⚠️ Transmission demande {wanted_tb} To ({wanted_files} fichiers) "
            f"pour {selected_gb} Go de jeux sélectionnés.[/bold yellow]"
        )
        console.print("[dim]   Lancez [cyan]retrobridge tools apply[/cyan] pour désélectionner le reste des packs.[/dim]")

    st = status["storage"]
    dl = st["download_dir"]
    lib = st["library_dir"]
    fs_ok = "[green]OK[/green] (même partition)" if st["same_partition_for_hardlinks"] else "[yellow]Partitions différentes[/yellow]"
    console.print(f"[bold]Stockage :[/bold] Download: {dl['free_gb']} Go libres | Library: {lib['free_gb']} Go libres (Hardlinks: {fs_ok})")

    if status.get("targets"):
        console.print("\n[bold]Cibles d'export :[/bold]")
        for name, t in status["targets"].items():
            mounted = "[green]Montée[/green]" if t["mounted"] else "[red]Non montée[/red]"
            console.print(f"  • {name} ({t['profile']}): {mounted} - {t['free_gb']} Go libres, {t['exported_games']} jeux exportés")

    if status.get("pending_actions"):
        console.print(f"\n[bold yellow]Actions en attente :[/bold yellow] {status['pending_actions']}")


@app.command("sync")
def cmd_sync(
    dry_run: bool = typer.Option(False, "--dry-run", help="Simule les opérations sans modifier les fichiers ni la base"),
    force_scan: bool = typer.Option(False, "--force-scan", help="Force le scan Transmission ignorant le TTL"),
    profile: str = typer.Option("esde", "--profile", "-p", help="Profil cible pour l'organisation de Library-Dir"),
    apply: bool = typer.Option(True, "--apply/--no-apply", help="Réappliquer la sélection des fichiers dans Transmission"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Aucune sortie sauf erreur"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base")
):
    """Réconciliation locale complète : scan si requis, mise à jour des états et création des liens."""
    try:
        res = run_sync(profile_name=profile, dry_run=dry_run, force_scan=force_scan, db_path=db_path, apply=apply)
    except LockError:
        # Normal case when the timer is already running: do not fail systemd.
        if as_json:
            typer.echo(json.dumps({"skipped": True, "reason": "locked"}, ensure_ascii=False))
        elif not quiet:
            console.print("[yellow]Synchronisation déjà en cours — ignorée.[/yellow]")
        return

    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return
    if quiet:
        return

    mode = "[yellow](DRY RUN)[/yellow] " if dry_run else ""
    console.print(f"{mode}[bold green]Synchronisation terminée.[/bold green]")
    console.print(f"  • Nouveaux en téléchargement : {res['newly_downloading']}")
    console.print(f"  • Nouveaux téléchargés : {res['newly_downloaded']}")
    console.print(f"  • Liens vérifiés / créés : {res['links_created_or_verified']}")

    for action in res["actions"]:
        console.print(f"    [dim]-> {action}[/dim]")

    if res["errors"]:
        console.print("[bold red]Erreurs :[/bold red]")
        for err in res["errors"]:
            console.print(f"    [red]! {err}[/red]")

@app.command("remove")
def cmd_remove(
    title: str = typer.Argument(..., help="Titre du jeu à supprimer"),
    system: Optional[str] = typer.Option(None, "--system", "-s", help="Système du jeu"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base")
):
    """Marque un jeu pour suppression asynchrone (Transmission, Library, Downloads, Targets)."""
    actions = request_removal(title=title, system=system, db_path=db_path)
    if not actions:
        console.print(f"[yellow]Aucun jeu trouvé correspondant à '{title}'.[/yellow]")
        return
    console.print(f"[bold green]Jeu marqué pour suppression ({len(actions)} jeu(x)).[/bold green]")
    console.print("[dim]Les fichiers locaux seront nettoyés au prochain 'sync', les targets au prochain 'export'.[/dim]")

@app.command("pin")
def cmd_pin(
    title: str = typer.Argument(..., help="Titre du jeu à verrouiller"),
    system: Optional[str] = typer.Option(None, "--system", "-s", help="Système du jeu"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base")
):
    """Verrouille la ROM sélectionnée pour un jeu."""
    if set_game_pin(title=title, system=system, pinned=True, db_path=db_path):
        console.print(f"[bold green]Jeu '{title}' verrouillé (pinned).[/bold green]")
    else:
        console.print(f"[red]Jeu '{title}' introuvable.[/red]")

@app.command("unpin")
def cmd_unpin(
    title: str = typer.Argument(..., help="Titre du jeu à déverrouiller"),
    system: Optional[str] = typer.Option(None, "--system", "-s", help="Système du jeu"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base")
):
    """Déverrouille la ROM sélectionnée pour un jeu."""
    if set_game_pin(title=title, system=system, pinned=False, db_path=db_path):
        console.print(f"[bold green]Jeu '{title}' déverrouillé.[/bold green]")
    else:
        console.print(f"[red]Jeu '{title}' introuvable.[/red]")

@app.command("find")
def cmd_find(
    query: str = typer.Argument(..., help="Titre ou mot-clé"),
    system: str = typer.Option(..., "--system", "-s", help="Système cible (gba, snes, ps2...)"),
    add: Optional[bool] = typer.Option(
        None,
        "--add/--no-add",
        help="Ajouter le jeu après sélection (défaut) ; --no-add pour une recherche seule",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Prendre automatiquement le meilleur candidat (match exact requis)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Afficher la sélection sans l'ajouter"),
    update: bool = typer.Option(False, "--update", help="Remplacer la ROM d'un jeu déjà présent"),
    refresh: bool = typer.Option(True, "--refresh/--no-refresh", help="Rafraîchir le cache torrent s'il est périmé"),
    limit: int = typer.Option(10, "--limit", "-n", min=1, max=50, help="Nombre de candidats affichés"),
    full_path: bool = typer.Option(False, "--full-path", help="Afficher le chemin complet des ROMs"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base"),
):
    """Recherche un jeu et l'ajoute après sélection (ajout par défaut)."""
    cfg = load_config()
    refresh_info = None
    # In --json mode, never prompt: read-only unless --yes is explicit.
    should_add = (add if add is not None else True) and (not as_json or yes)

    try:
        if refresh and not as_json and console.is_terminal:
            with console.status("[dim]Vérification du cache torrent…[/dim]"):
                refresh_info = refresh_cache_if_needed(cfg, db_path, refresh=True)
        else:
            refresh_info = refresh_cache_if_needed(cfg, db_path, refresh=refresh)
    except Exception as exc:
        refresh_info = {"ran": False, "error": str(exc)}
        if not as_json:
            console.print(f"[yellow]Rafraîchissement impossible :[/yellow] {exc}")

    if not as_json and refresh_info.get("ran"):
        console.print(
            f"[dim]Cache rafraîchi : {refresh_info.get('torrents_count', '?')} torrents, "
            f"{refresh_info.get('files_cached', '?')} fichiers.[/dim]"
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
        console.print(f"[bold green]Déjà présent :[/bold green] {existing['title']} ({existing['state']})")
        console.print(f"  ROM : {basename(existing.get('matched_file_name'))}")
        console.print("[dim]Utilise --update pour changer la ROM sélectionnée.[/dim]")
        return

    candidates = search_candidates(query=query, system=system, config=cfg, db_path=db_path)
    if not candidates:
        message = f"Aucune ROM correspondante trouvée pour '{query}' ({system})."
        if as_json:
            typer.echo(json.dumps({"error": message, "refresh": refresh_info}, ensure_ascii=False))
        else:
            console.print(f"[bold red]Erreur :[/bold red] {message}")
        raise typer.Exit(code=1)

    shown = candidates[:limit]
    if not as_json:
        if existing:
            console.print(
                f"[bold yellow]Remplacement :[/bold yellow] {existing['title']} "
                f"({existing['state']}) — ROM actuelle : {basename(existing.get('matched_file_name'))}"
            )
        table = Table(title=f"Candidats pour '{query}' ({len(candidates)})")
        table.add_column("N°", justify="right", style="cyan")
        table.add_column("Score", justify="right")
        table.add_column("Langue", style="magenta")
        table.add_column("Taille", justify="right")
        table.add_column("ROM", style="bold white")
        for index, candidate in enumerate(shown, start=1):
            rom_display = candidate["file_name"] if full_path else basename(candidate["file_name"])
            table.add_row(
                str(index),
                str(candidate["score"]),
                candidate.get("lang_label") or "-",
                f"{candidate.get('size_mb', 0):.1f} Mo",
                Text(rom_display),
            )
        console.print(table)
        if len(candidates) > len(shown):
            console.print(f"[dim]... et {len(candidates) - len(shown)} autres candidats.[/dim]")

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
            message = "Le meilleur candidat n'est pas un match exact. Relance sans --yes pour choisir."
            if as_json:
                typer.echo(json.dumps({"error": message, "candidate": selected}, ensure_ascii=False))
            else:
                console.print(f"[bold red]Erreur :[/bold red] {message}")
            raise typer.Exit(code=1)
        if len(candidates) > 1:
            second = candidates[1]
            if second["score"] == selected["score"] and second["match_bonus"] == selected["match_bonus"]:
                message = "Deux candidats sont à égalité. Relance sans --yes pour choisir."
                if as_json:
                    typer.echo(json.dumps({"error": message, "candidates": candidates[:2]}, ensure_ascii=False))
                else:
                    console.print(f"[bold red]Erreur :[/bold red] {message}")
                raise typer.Exit(code=1)
    else:
        choice = typer.prompt("Numéro du candidat à ajouter (q = annuler)", default="1")
        choice = choice.strip()
        if not choice.isdigit():
            console.print("[yellow]Aucun ajout.[/yellow]")
            return
        index = int(choice)
        if not 1 <= index <= len(shown):
            console.print(f"[bold red]Erreur :[/bold red] Sélection invalide '{choice}'.")
            raise typer.Exit(code=1)
        selected = shown[index - 1]

    if selected["score"] <= -5000:
        message = "Le candidat sélectionné est un mauvais dump / hack / démo. Refusé."
        if as_json:
            typer.echo(json.dumps({"error": message, "candidate": selected}, ensure_ascii=False))
        else:
            console.print(f"[bold red]Erreur :[/bold red] {message}")
        raise typer.Exit(code=1)

    discs = expand_disc_candidates(selected, system, config=cfg, db_path=db_path)
    if not as_json and len(discs) > 1:
        console.print(f"[bold]Multi-disques :[/bold] {len(discs)} disques seront sélectionnés.")
        for item in discs:
            console.print(f"  • {basename(item['file_name'])}")

    if dry_run:
        if as_json:
            typer.echo(json.dumps({"dry_run": True, "candidate": selected, "discs": discs, "existing": existing, "refresh": refresh_info}, indent=2, ensure_ascii=False))
        else:
            rom_display = selected["file_name"] if full_path else basename(selected["file_name"])
            console.print(f"[bold]Ajout simulé :[/bold] {rom_display}")
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
            console.print(f"[bold red]Erreur :[/bold red] {exc}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps({
            "added": result,
            "candidate": selected,
            "existing": existing,
            "refresh": refresh_info,
        }, indent=2, ensure_ascii=False))
        return

    console.print(f"[bold green]Jeu ajouté :[/bold green] [bold white]{result['title']}[/bold white] ({result['system']})")
    console.print(f"  • ROM sélectionnée : {basename(result['matched_file'])}")
    console.print(f"  • Score : {result['score']} ({result['lang_label']}) - {result['size_mb']} Mo")
    if result.get("discs", 1) > 1:
        console.print(f"  • Disques sélectionnés : {result['discs']}")
    if result.get("download_started"):
        console.print("  • [green]Téléchargement lancé dans Transmission.[/green]")
    elif result.get("rpc_error"):
        console.print(
            "  • [yellow]Ajout en base, mais Transmission n'a pas pu être mis à jour :[/yellow] "
            f"{result['rpc_error']}"
        )
        console.print("    [dim]Lance 'retrobridge sync' pour (re)lancer le téléchargement.[/dim]")
    else:
        console.print("    [yellow]Téléchargement non démarré (RPC désactivé).[/yellow]")


@app.command("search", hidden=True)
def cmd_search(
    query: str = typer.Argument(..., help="Titre ou mot-clé cherché"),
    system: str = typer.Option(..., "--system", "-s", help="Système (ex: snes, gba, psx)"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base")
):
    """Recherche des ROMs dans les packs torrents actifs selon le scoring régional et de version."""
    candidates = search_candidates(query=query, system=system, db_path=db_path)

    if as_json:
        typer.echo(json.dumps(candidates, indent=2, ensure_ascii=False))
        return

    if not candidates:
        console.print(f"[yellow]Aucun fichier correspondant trouvé pour '{query}' dans les packs.[/yellow]")
        return

    table = Table(title=f"Candidats trouvés ({len(candidates)}) pour '{query}'")
    table.add_column("Score", justify="right", style="cyan")
    table.add_column("Région/Langue", style="magenta")
    table.add_column("Fichier trouvé", style="bold white")
    table.add_column("Taille (Mo)", justify="right")

    for c in candidates[:15]:
        table.add_row(str(c["score"]), c["lang_label"], c["file_name"], str(c["size_mb"]))

    console.print(table)
    if len(candidates) > 15:
        console.print(f"[dim]... et {len(candidates) - 15} autres résultats masqués.[/dim]")

@app.command("add", hidden=True)
def cmd_add(
    title: str = typer.Argument(..., help="Titre du jeu à ajouter"),
    system: str = typer.Option(..., "--system", "-s", help="Système cible"),
    auto_yes: bool = typer.Option(False, "--yes", "-y", help="Sélectionne automatiquement le meilleur candidat"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base")
):
    """Ajoute un jeu à la collection en sélectionnant la meilleure ROM disponible."""
    try:
        candidates = search_candidates(query=title, system=system, db_path=db_path)
        if not candidates:
            raise AddGameError(f"Aucune ROM correspondante trouvée dans les packs torrents pour '{title}'.")

        selected = candidates[0]
        if not auto_yes and not as_json and len(candidates) > 1:
            preview = Table(title=f"Candidats pour '{title}' ({len(candidates)})")
            preview.add_column("N°", justify="right", style="cyan")
            preview.add_column("Score", justify="right")
            preview.add_column("Région/Langue", style="magenta")
            preview.add_column("Fichier", style="bold white")
            shown = candidates[:10]
            for i, c in enumerate(shown, start=1):
                preview.add_row(str(i), str(c["score"]), c["lang_label"], c["file_name"])
            console.print(preview)
            if len(candidates) > len(shown):
                console.print(f"[dim]... et {len(candidates) - len(shown)} autres résultats masqués.[/dim]")

            choice = typer.prompt("Numéro du candidat à ajouter", default="1")
            try:
                idx = int(choice)
                if not 1 <= idx <= len(shown):
                    raise ValueError
            except ValueError:
                raise AddGameError(f"Sélection invalide : '{choice}'.")
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
            console.print(f"[bold red]Erreur :[/bold red] {e}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return

    console.print(f"[bold green]Jeu ajouté :[/bold green] [bold white]{res['title']}[/bold white] ({res['system']})")
    console.print(f"  • ROM sélectionnée : {basename(res['matched_file'])}")
    console.print(f"  • Score : {res['score']} ({res['lang_label']}) - {res['size_mb']} Mo")
    if res.get("discs", 1) > 1:
        console.print(f"  • Disques sélectionnés : {res['discs']}")
    if res.get("download_started"):
        console.print("  • [green]Téléchargement lancé dans Transmission.[/green]")
    elif res.get("rpc_error"):
        console.print(
            "  • [yellow]Ajout en base, mais Transmission n'a pas pu être mis à jour :[/yellow] "
            f"{res['rpc_error']}"
        )
        console.print("    [dim]Lance 'retrobridge sync' pour (re)lancer le téléchargement.[/dim]")

@app.command("export")
def cmd_export(
    target: str = typer.Argument(..., help="Nom de la cible configurée (carte SD)"),
    system: Optional[str] = typer.Option(None, "--system", "-s", help="Filtrer par système"),
    tier: Optional[int] = typer.Option(None, "--tier", "-t", help="Filtrer par tier (1=2D classique, 2=première 3D, 3=PS2 et +)"),
    raw: bool = typer.Option(False, "--raw", help="Copier les fichiers bruts sans conversion de format"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simule l'export sans copier"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Aucune sortie sauf erreur"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base")
):
    """Exporte de manière incrémentale la collection vers une cible (carte SD)."""
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
            console.print(f"[bold red]Erreur d'export :[/bold red] {e}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return
    if quiet:
        return

    mode = "[yellow](DRY RUN)[/yellow] " if res["dry_run"] else ""
    console.print(f"{mode}[bold green]Export vers '{res['target']}' terminé.[/bold green]")
    console.print(f"  • Profil : {res['profile']} (UUID: {res['uuid'][:8]}...)")
    console.print(f"  • Jeux exportés / convertis : [bold cyan]{res['exported']}[/bold cyan]")
    console.print(f"  • Jeux ignorés (déjà à jour) : [dim]{res['skipped']}[/dim]")

    extra = res.get("destination_only") or []
    if extra:
        console.print(
            f"\n[bold cyan]Info :[/bold cyan] {len(extra)} fichier(s) présent(s) uniquement sur la cible "
            "[dim](non supprimés)[/dim]"
        )
        for item in extra[:20]:
            console.print(f"  [dim]• {item['system']} : {item['file']}[/dim]")
        if len(extra) > 20:
            console.print(f"  [dim]... et {len(extra) - 20} autres.[/dim]")

    if res["errors"]:
        console.print("[bold red]Erreurs survenues :[/bold red]")
        for err in res["errors"]:
            console.print(f"  [red]! {err}[/red]")
