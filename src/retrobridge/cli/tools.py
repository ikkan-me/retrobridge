import typer
from typing import Optional
from rich.console import Console
from pathlib import Path
import json
import shutil

from retrobridge.storage.db import init_db, get_db_connection
from retrobridge.storage.legacy import import_legacy_db
from retrobridge.storage.migrations import migrate_db, backup_db
from retrobridge.services.scan import scan_torrents
from retrobridge.services.apply import apply_selection
from retrobridge.services.audit import audit_collection
from retrobridge.profiles.loader import list_profiles, load_profile
from retrobridge.adapters.transmission import TransmissionClient
from retrobridge.services.export import prepare_export
from retrobridge.config import load_config


app = typer.Typer(help="Commandes utilitaires et avancées")
db_app = typer.Typer(help="Gestion de la base de données")
app.add_typer(db_app, name="db")
console = Console()


def _transmission_client() -> TransmissionClient:
    """Construit un client Transmission à partir de la configuration utilisateur."""
    cfg = load_config()
    return TransmissionClient(
        rpc_url=cfg.transmission.rpc_url,
        username=cfg.transmission.user,
        password=cfg.transmission.password,
        timeout=cfg.transmission.timeout,
    )

@db_app.command("init")
def db_init(
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base")
):
    """Initialise le schéma de la base de données SQLite."""
    init_db(db_path)
    console.print("[green]Base de données initialisée avec succès.[/green]")

@db_app.command("import-legacy")
def db_import_legacy(
    legacy_db: str = typer.Argument(..., help="Chemin vers l'ancienne base roms_manager.db"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base v2")
):
    """Importe les données depuis l'ancienne base roms_manager.db."""
    stats = import_legacy_db(legacy_db_path=legacy_db, target_db_path=db_path)
    console.print(f"[bold green]Import terminé :[/bold green] {stats['games_inserted']} jeux, {stats['cache_rows_copied']} entrées de cache.")

@db_app.command("migrate")
def db_migrate(
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Affiche les changements sans les appliquer"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
):
    """Aligne les données sur les codes systèmes canoniques (suppression des alias historique)."""
    res = migrate_db(db_path=db_path, dry_run=dry_run)

    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return

    mode = "[yellow](DRY RUN)[/yellow] " if dry_run else ""
    console.print(f"{mode}[bold green]Migration terminée.[/bold green]")
    console.print(f"  • Systèmes renommés : {len(res['renamed'])}")
    console.print(f"  • Doublons fusionnés : {len(res['merged'])}")
    for item in res["renamed"]:
        console.print(f"    [dim]-> {item['title']} : {item['from']} -> {item['to']}[/dim]")
    for item in res["merged"]:
        console.print(f"    [dim]-> {item['title']} : doublon {item['dropped_id']} fusionné dans {item['kept_id']}[/dim]")
    if res["unresolved"]:
        console.print("[bold yellow]Systèmes non reconnus (à corriger manuellement) :[/bold yellow]")
        for item in res["unresolved"]:
            console.print(f"    [yellow]! {item['title']} ({item['system']})[/yellow]")


@db_app.command("backup")
def db_backup(
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base"),
    destination: Optional[str] = typer.Option(None, "--dest", "-d", help="Fichier ou répertoire de destination"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
):
    """Crée une copie horodatée de la base SQLite."""
    try:
        dest = backup_db(db_path=db_path, destination=destination)
    except Exception as e:
        if as_json:
            typer.echo(json.dumps({"error": str(e)}))
        else:
            console.print(f"[bold red]Erreur :[/bold red] {e}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps({"backup": str(dest)}, indent=2, ensure_ascii=False))
        return

    console.print(f"[bold green]Sauvegarde créée :[/bold green] {dest}")

@app.command("audit")
def tools_audit(
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base"),
):
    """Vérifie qu'aucun jeu n'est orphelin (fichier hors pack, disque, doublons)."""
    res = audit_collection(db_path=db_path)

    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        if res["ok"]:
            console.print("[bold green]Audit OK :[/bold green] aucun jeu orphelin.")
        else:
            console.print("[bold red]Anomalies détectées :[/bold red]")
            console.print(f"  • Jeux sans fichier de pack : {len(res['without_pack_file'])}")
            console.print(f"  • Jeux sans fichier matché : {len(res['without_matched_name'])}")
            console.print(f"  • Jeux absents du disque : {len(res['missing_on_disk'])}")
            for key, label in [
                ("without_pack_file", "hors pack"),
                ("missing_on_disk", "absent du disque"),
            ]:
                for item in res[key][:10]:
                    console.print(f"    [yellow]! {item['system']} | {item['title']} ({label})[/yellow]")

    if not res["ok"]:
        raise typer.Exit(code=1)


@app.command("apply")
def tools_apply(
    dry_run: bool = typer.Option(False, "--dry-run", help="Affiche les changements sans les appliquer"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base"),
):
    """Aligne les priorités Transmission : seuls les fichiers des jeux sélectionnés restent demandés."""
    try:
        res = apply_selection(db_path=db_path, dry_run=dry_run)
    except Exception as e:
        if as_json:
            typer.echo(json.dumps({"error": str(e)}))
        else:
            console.print(f"[bold red]Erreur :[/bold red] {e}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return

    mode = "[yellow](DRY RUN)[/yellow] " if dry_run else ""
    console.print(f"{mode}[bold green]Sélection appliquée.[/bold green]")
    console.print(f"  • Torrents modifiés : {res['torrents_processed']}")
    console.print(f"  • Fichiers activés : {res['files_wanted']}")
    console.print(f"  • Fichiers désélectionnés : {res['files_unwanted']}")
    for err in res["errors"]:
        console.print(f"  [red]! {err}[/red]")


@app.command("scan")
def tools_scan(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Aucune sortie sauf erreur"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base"),
):
    """Scanne les torrents actifs dans Transmission et rafraîchit le cache."""
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
            with console.status("[dim]Scan Transmission…[/dim]") as live:
                status = live
                res = scan_torrents(db_path=db_path, progress=progress)
    except Exception as e:
        if as_json:
            typer.echo(json.dumps({"error": str(e)}, ensure_ascii=False))
        elif not quiet:
            console.print(f"[bold red]Erreur lors du scan :[/bold red] {e}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False))
        return
    if quiet:
        return
    console.print(
        f"[green]Scan terminé :[/green] {res['torrents_count']} torrents, "
        f"{res['files_cached']} fichiers en cache, "
        f"{res['changed_torrents']} modifié(s), {res['unchanged_torrents']} inchangé(s) "
        f"en {res['duration_ms']} ms."
    )

@app.command("profiles")
def tools_profiles():
    """Liste les profils frontend disponibles."""
    profiles = list_profiles()
    console.print("[bold]Profils disponibles :[/bold]")
    for p_name in profiles:
        prof = load_profile(p_name)
        console.print(f"  • [cyan]{prof.name}[/cyan] ({prof.display_name}) - {len(prof.systems)} systèmes")

@app.command("bios")
def tools_bios():
    """Propose l'installation des BIOS via RetroBios (outil externe)."""
    executable = shutil.which("retrobios") or shutil.which("RetroBios")
    console.print("[bold]BIOS :[/bold] RetroBridge ne duplique pas le catalogue RetroBios.")
    if executable:
        console.print(f"  • RetroBios détecté : [cyan]{executable}[/cyan]")
        console.print("  • Lance `retrobios` pour choisir, télécharger et vérifier les BIOS.")
    else:
        console.print("  • RetroBios n'est pas installé.")
        console.print("  • Projet : [cyan]https://github.com/Abdess/retrobios[/cyan]")


def _torrent_action(torrent_id: int, action: str, dry_run: bool, as_json: bool) -> None:
    """Exécute une action unitaire Transmission (pause, resume, verify)."""
    labels = {
        "pause": ("mis en pause", "stop_torrent"),
        "resume": ("repris", "start_torrent"),
        "verify": ("vérifié", "verify_torrent"),
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
                console.print(f"[bold red]Erreur :[/bold red] {exc}")
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
    torrent_id: int = typer.Argument(..., help="ID du torrent à mettre en pause"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Affiche l'action sans l'exécuter"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
):
    """Mets en pause un torrent dans Transmission."""
    _torrent_action(torrent_id, "pause", dry_run, as_json)

@app.command("resume")
def tools_resume(
    torrent_id: int = typer.Argument(..., help="ID du torrent à reprendre"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Affiche l'action sans l'exécuter"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
):
    """Reprend un torrent dans Transmission."""
    _torrent_action(torrent_id, "resume", dry_run, as_json)

@app.command("verify")
def tools_verify(
    torrent_id: Optional[int] = typer.Argument(None, help="ID du torrent à vérifier (sans argument : torrents en erreur)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Affiche l'action sans l'exécuter"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
):
    """Vérifie un torrent, ou tous les torrents signalés en erreur si l'ID est omit."""
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
            console.print(f"[bold red]Transmission injoignable :[/bold red] {exc}")
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
        console.print("[green]Aucun torrent signalé en erreur.[/green]")
        return

    for entry in results:
        state = "vérification lancée" if entry.get("verified") else ("dry-run" if dry_run else "échec")
        console.print(
            f"[cyan]#{entry['torrent_id']}[/cyan] {entry['name']} — "
            f"{entry.get('error_string') or 'erreur Transmission'} ({state})"
        )

@app.command("clean")
def tools_clean(
    apply_changes: bool = typer.Option(False, "--apply", help="Supprimer réellement les fichiers orphelins"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base"),
):
    """Nettoie les fichiers orphelins du Download-Dir (dry-run par défaut)."""
    dry_run = not apply_changes
    cfg = load_config()
    download_dir = cfg.paths.download_dir
    init_db(db_path)

    # Collect all known files: selected games, exports and
    # the entire pack index (protects files still seeding).
    referenced_files: set[str] = set()
    with get_db_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT matched_file_path FROM games")
        for row in cur.fetchall():
            path = row[0]
            if path:
                referenced_files.add(str(Path(path).name))
        cur.execute("SELECT file_path FROM game_files")
        for row in cur.fetchall():
            path = row[0]
            if path:
                referenced_files.add(str(Path(path).name))
        cur.execute("SELECT source_file FROM exports")
        for row in cur.fetchall():
            path = row[0]
            if path:
                referenced_files.add(str(Path(path).name))
        cur.execute("SELECT source_file FROM export_files")
        for row in cur.fetchall():
            path = row[0]
            if path:
                referenced_files.add(str(Path(path).name))
        cur.execute("SELECT file_name FROM torrent_cache")
        for row in cur.fetchall():
            path = row[0]
            if path:
                referenced_files.add(str(Path(path).name))

    # Scan the Download-Dir for orphan files
    orphans: list[Path] = []
    if download_dir.exists():
        for p in download_dir.rglob("*"):
            if not p.is_file():
                continue
            name = p.name
            if name in referenced_files:
                continue
            # An in-progress download has the .part suffix: keep it.
            if name.endswith(".part") and name[:-5] in referenced_files:
                continue
            orphans.append(p)

    if dry_run:
        if orphans:
            console.print(f"[yellow]Dry-run :[/yellow] {len(orphans)} fichier(s) orphelin(s) trouvé(s).")
            for o in orphans:
                console.print(f"  [dim]-> {o}[/dim]")
            console.print("[dim]Relance avec --apply pour supprimer.[/dim]")
        else:
            console.print("[dim]Aucun fichier orphelin trouvé.[/dim]")
        return

    if orphans:
        deleted_count = 0
        for orphan in orphans:
            try:
                orphan.unlink()
                deleted_count += 1
            except Exception as e:
                console.print(f"[red]Erreur suppression {orphan} : {e}[/red]")
        console.print(f"[green]Nettoyage terminé : {deleted_count} fichier(s) supprimé(s).[/green]")
    else:
        console.print("[green]Aucun fichier orphelin trouvé.[/green]")

@app.command("relocate")
def tools_relocate(
    torrent_id: int = typer.Argument(..., help="ID du torrent à relocaliser"),
    new_dir: str = typer.Argument(..., help="Nouveau répertoire de téléchargement du torrent"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Affiche l'action sans déplacer les fichiers"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
):
    """Relocalise un torrent : Transmission déplace les fichiers puis met à jour son emplacement."""
    target = Path(new_dir).expanduser().resolve()

    if dry_run:
        if as_json:
            typer.echo(json.dumps({"dry_run": True, "torrent_id": torrent_id, "location": str(target)}))
        else:
            console.print(f"[dim](DRY RUN)[/dim] Torrent {torrent_id} serait déplacé vers {target}.")
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
            console.print(f"[bold red]Erreur :[/bold red] {e}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps({"torrent_id": torrent_id, "location": str(target)}, indent=2))
    else:
        console.print(f"[bold green]Torrent {torrent_id} relocalisé vers {target}.[/bold green]")

@app.command("prepare-export")
def tools_prepare_export(
    system: Optional[str] = typer.Option(None, "--system", "-s", help="Filtrer par système"),
    tier: Optional[int] = typer.Option(None, "--tier", "-t", help="Filtrer par tier (1=2D classique, 2=première 3D, 3=PS2 et +)"),
    raw: bool = typer.Option(False, "--raw", help="Ne rien convertir (copie brute, donc aucune préparation)"),
    profile: str = typer.Option("esde", "--profile", "-p", help="Profil frontend utilisé pour les règles de conversion"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Liste les conversions sans les exécuter"),
    as_json: bool = typer.Option(False, "--json", help="Sortie JSON brute"),
    db_path: Optional[str] = typer.Option(None, "--db", help="Chemin alternatif vers la base"),
):
    """Convertit les fichiers au format d'export dans le staging local, sans copier sur une carte."""
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
            console.print(f"[bold red]Erreur :[/bold red] {e}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(result, indent=2, ensure_ascii=False))
        return

    mode = "[yellow](DRY RUN)[/yellow] " if dry_run else ""
    console.print(f"{mode}[bold green]Préparation d'export terminée.[/bold green]")
    console.print(f"  • Profil : {result['profile']}")
    console.print(f"  • Fichiers préparés : {result['prepared']}")
    console.print(f"  • Fichiers ignorés (copie directe) : {result['skipped']}")
    console.print(f"  • Staging : {result['staging_dir']}")
    for err in result["errors"]:
        console.print(f"  [red]! {err}[/red]")
