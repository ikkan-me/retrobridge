import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from collections import defaultdict

from retrobridge.config import Config, load_config
from retrobridge.storage.db import backfill_game_files, get_db_connection
from retrobridge.storage.lock import file_lock
from retrobridge.profiles.loader import load_profile
from retrobridge.domain.tiers import get_tier_systems
from retrobridge.domain.target import ensure_target_uuid
from retrobridge.domain.multidisc import extract_disc_number, get_disc_family, generate_m3u_content
from retrobridge.domain.systems import normalize_system
from retrobridge.adapters.compress import (
    get_staging_dir,
    extract_from_archive,
    extract_disc_set,
    convert_to_chd,
    convert_to_rvz,
    DISC_SET_MEMBERS,
)

class ExportError(Exception):
    pass


# Copy/chunk size for byte-level progress (4 MiB).
COPY_CHUNK_SIZE = 4 * 1024 * 1024

# Files that live on the card but are not games: emulator metadata, images,
# playlists. They must never show up as "unmanaged" files.
CARD_NOISE_EXTENSIONS = {
    ".txt", ".xml", ".dat", ".cfg", ".ini", ".json",
    ".png", ".jpg", ".jpeg", ".webp", ".m3u",
}


def _copy_with_progress(
    src: Path, dst: Path, on_bytes: Optional[Callable[[int, int], None]] = None
) -> Path:
    """Copies a file in chunks so the caller can report byte progress."""
    total = src.stat().st_size
    done = 0
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            block = fsrc.read(COPY_CHUNK_SIZE)
            if not block:
                break
            fdst.write(block)
            done += len(block)
            if on_bytes is not None:
                on_bytes(done, total)
    try:
        shutil.copystat(src, dst)
    except OSError:
        # Not every filesystem (exFAT/FAT) accepts every metadata change.
        pass
    return dst


def _discard_staging(paths: list) -> None:
    """Removes intermediate staging files and unpack directories."""
    for path in paths:
        if not path:
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _unpack_if_archive(src: Path, staging_dir: Path, sys_prof, tmp_files: list) -> Path:
    """Extracts a zip archive into staging before conversion (chd/rvz).

    Optical disc sets (`.cue` + `.bin` tracks, `.gdi`...) need the whole set:
    chdman cannot convert a `.cue` whose tracks are still inside the archive.
    Single-file images (`.rvz`, `.gcm`, `.wbfs`...) must be selected by
    extension instead: ``extract_disc_set`` ignores `.rvz` and would raise
    "No disc image found".
    """
    if zipfile.is_zipfile(src):
        work_dir = Path(tempfile.mkdtemp(prefix="unpack-", dir=staging_dir))
        tmp_files.append(work_dir)
        with zipfile.ZipFile(src) as zf:
            suffixes = {
                Path(member).suffix.lower()
                for member in zf.namelist()
                if not member.endswith("/")
            }
        if suffixes & set(DISC_SET_MEMBERS):
            return extract_disc_set(src, work_dir)
        return extract_from_archive(
            src, work_dir, sys_prof.extensions if sys_prof else None
        )
    return src

def export_to_target(
    target_name: str,
    system: Optional[str] = None,
    tier: Optional[int] = None,
    raw: bool = False,
    dry_run: bool = False,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    config: Optional[Config] = None,
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    """Incrementally exports the collection to a target.

    `progress`, when given, receives plain dicts (`{"event": ...}`) so the
    caller can render a progress bar without the service knowing about the UI.
    """
    cfg = config or load_config()

    if target_name not in cfg.targets:
        raise ExportError(f"Unknown target: '{target_name}'. Available targets: {list(cfg.targets.keys())}")

    target_cfg = cfg.targets[target_name]
    mount_path = Path(target_cfg.mount_path)
    if not mount_path.is_dir():
        raise ExportError(f"The mount point of target '{target_name}' is not accessible: {mount_path}")

    # 1. Identify the target card (.retrobridge-target)
    target_uuid = ensure_target_uuid(mount_path, target_cfg.uuid, name=target_name, profile=target_cfg.profile)
    profile = load_profile(target_cfg.profile)
    staging_dir = get_staging_dir()

    allowed_systems = get_tier_systems(tier) if tier else None

    def emit(event: Dict[str, Any]) -> None:
        if progress is None:
            return
        try:
            progress(event)
        except Exception:
            # A broken reporter must never break an export.
            pass

    def scope_of(row_system: str) -> Optional[str]:
        sys_k = normalize_system(row_system).lower()
        if system and sys_k != normalize_system(system).lower():
            return None
        if allowed_systems is not None and sys_k not in allowed_systems:
            return None
        return sys_k

    def new_game(row) -> Dict[str, Any]:
        return {
            "system": normalize_system(row["system"]),
            "title": row["title"],
            "discs": 0,
            "file_count": 0,
            "size_bytes": 0,
            "files": [],
            "_items": [],
        }

    planned_games: Dict[int, Dict[str, Any]] = {}
    present_games: Dict[int, Dict[str, Any]] = {}
    waiting_games: Dict[int, Dict[str, Any]] = {}
    known_stems: Dict[str, set] = defaultdict(set)
    skipped_files = 0

    with file_lock(cfg.paths.data_dir):
        with get_db_connection(db_path) as conn:
            backfill_game_files(conn)
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT g.id, g.title, g.system, g.state, "
                "gf.disc_number, gf.file_path, gf.file_name, gf.file_length "
                "FROM games g JOIN game_files gf ON gf.game_id = g.id "
                "ORDER BY g.id, gf.disc_number;"
            ).fetchall()

            # 2. Build the plan: planned / present / waiting (+ card names)
            for row in rows:
                sys_k = scope_of(row["system"])
                if sys_k is None:
                    continue
                file_rel = row["file_path"] or row["file_name"] or ""
                if not file_rel:
                    continue
                known_stems[sys_k].add(Path(file_rel).stem)

                state = row["state"]
                if state not in ("downloading", "downloaded", "linked"):
                    continue

                sys_prof = profile.get_system(sys_k)
                sys_sub = sys_prof.dir if sys_prof else sys_k
                file_name = Path(file_rel).name

                if state == "downloading":
                    entry = waiting_games.setdefault(row["id"], new_game(row))
                    entry["discs"] += 1
                    entry["file_count"] += 1
                    entry["size_bytes"] += int(row["file_length"] or 0)
                    entry["files"].append(file_name)
                    continue

                # Source: Download-Dir first (seeding), Library-Dir as fallback.
                src = cfg.paths.download_dir / file_rel
                if not src.is_file():
                    src = cfg.paths.library_dir / sys_sub / file_name
                if not src.is_file():
                    continue

                rule = "copy" if raw else (sys_prof.export_format if sys_prof else "copy")
                if raw:
                    target_ext = Path(file_rel).suffix
                elif sys_prof and sys_prof.target_ext:
                    target_ext = sys_prof.target_ext
                else:
                    target_ext = Path(file_rel).suffix

                dest_dir = mount_path / profile.roms_subpath / sys_sub
                expected = dest_dir / (Path(file_rel).stem + target_ext)

                recorded = cur.execute(
                    "SELECT file_size, exported_file FROM export_files "
                    "WHERE target_id = ? AND game_id = ? AND disc_number = ?;",
                    (target_uuid, row["id"], row["disc_number"]),
                ).fetchone()

                # Reconciliation by expected name + size, not only by record:
                # a card filled by an earlier tool still counts as "present".
                status = "new"
                if expected.is_file():
                    dest_size = expected.stat().st_size
                    if recorded and recorded["exported_file"] == str(expected):
                        status = "present" if recorded["file_size"] == dest_size else "outdated"
                    elif rule == "copy":
                        status = "present" if dest_size == src.stat().st_size else "outdated"
                    else:
                        # Converted file, no record: accept by name (unverified).
                        status = "unverified"

                if status in ("present", "unverified"):
                    skipped_files += 1
                    entry = present_games.setdefault(row["id"], new_game(row))
                    entry.setdefault("verified", True)
                    entry["verified"] = entry["verified"] and status == "present"
                    entry["discs"] += 1
                    entry["file_count"] += 1
                    entry["size_bytes"] += expected.stat().st_size
                    entry["files"].append(expected.name)
                    continue

                src_size = src.stat().st_size
                entry = planned_games.setdefault(row["id"], new_game(row))
                entry.setdefault("action", rule)
                entry.setdefault("status", status)
                if status == "outdated":
                    entry["status"] = "outdated"
                entry["discs"] += 1
                entry["file_count"] += 1
                entry["size_bytes"] += src_size
                entry["files"].append(expected.name)
                entry["_items"].append({
                    "game_id": row["id"],
                    "disc_number": row["disc_number"],
                    "src": src,
                    "dest": expected,
                    "dest_dir": dest_dir,
                    "rule": rule,
                    "sys_prof": sys_prof,
                    "src_size": src_size,
                })

            # 3. Check free space on target (whole batch, then per game)
            pending_items = [item for game in planned_games.values() for item in game["_items"]]
            total_estimated_bytes = sum(item["src_size"] for item in pending_items)
            free_bytes = shutil.disk_usage(mount_path).free
            if free_bytes < total_estimated_bytes:
                free_mb = round(free_bytes / (1024 * 1024), 1)
                req_mb = round(total_estimated_bytes / (1024 * 1024), 1)
                raise ExportError(f"Insufficient space on target '{target_name}': {free_mb} MB free, {req_mb} MB required.")

            emit({
                "event": "plan",
                "games": len(planned_games),
                "files": len(pending_items),
                "bytes": total_estimated_bytes,
            })

            # 4. Run the export
            exported_count = 0
            written_bytes = 0
            errors: List[str] = []
            multidisc_tracker = defaultdict(lambda: defaultdict(list))

            def on_bytes(done: int, total: int) -> None:
                emit({"event": "bytes", "done": done, "total": total})

            for index, game in enumerate(planned_games.values(), start=1):
                items = game["_items"]

                if dry_run:
                    exported_count += len(items)
                    continue

                emit({
                    "event": "game_start",
                    "index": index,
                    "total": len(planned_games),
                    "title": game["title"],
                    "system": game["system"],
                    "discs": game["discs"],
                })

                if shutil.disk_usage(mount_path).free < sum(item["src_size"] for item in items):
                    errors.append(f"Not enough space left on target '{target_name}' for {game['title']}.")
                    emit({"event": "game_done", "index": index})
                    continue

                for item in items:
                    src = item["src"]
                    dest = item["dest"]
                    dest_dir = item["dest_dir"]
                    rule = item["rule"]
                    sys_prof = item["sys_prof"]

                    if dest_dir.is_symlink() and not dest_dir.exists():
                        errors.append(f"Broken link to directory {dest_dir}: recreate or delete it.")
                        continue
                    if dest_dir.exists() and not dest_dir.is_dir():
                        errors.append(f"The path {dest_dir} exists but is not a directory.")
                        continue

                    dest_dir.mkdir(parents=True, exist_ok=True)
                    tmp_staging_files = []
                    try:
                        if rule == "copy" or raw:
                            emit({"event": "phase", "phase": "copy"})
                            _copy_with_progress(src, dest, on_bytes)
                        elif rule == "extract":
                            emit({"event": "phase", "phase": "extract"})
                            if not zipfile.is_zipfile(src):
                                _copy_with_progress(src, dest, on_bytes)
                            else:
                                extracted = extract_from_archive(
                                    src, staging_dir, sys_prof.extensions if sys_prof else None
                                )
                                tmp_staging_files.append(extracted)
                                emit({"event": "phase", "phase": "copy"})
                                _copy_with_progress(extracted, dest, on_bytes)
                        elif rule in ("chd", "rvz"):
                            tool = "chdman" if rule == "chd" else "dolphin-tool"
                            if src.suffix.lower() == f".{rule}" or not shutil.which(tool):
                                # Already converted, or tool missing: plain copy.
                                emit({"event": "phase", "phase": "copy"})
                                _copy_with_progress(src, dest, on_bytes)
                            else:
                                work_src = _unpack_if_archive(
                                    src, staging_dir, sys_prof, tmp_staging_files
                                )
                                if work_src.suffix.lower() == f".{rule}":
                                    # The archive already contains the target
                                    # format (e.g. .rvz in the GameCube pack).
                                    emit({"event": "phase", "phase": "copy"})
                                    _copy_with_progress(work_src, dest, on_bytes)
                                else:
                                    emit({"event": "phase", "phase": "convert"})
                                    tmp_out = staging_dir / (src.stem + f".{rule}")
                                    if rule == "chd":
                                        is_dvd = normalize_system(game["system"]).lower() in ("ps2", "psp")
                                        convert_to_chd(work_src, tmp_out, is_dvd=is_dvd)
                                    else:
                                        convert_to_rvz(work_src, tmp_out)
                                    tmp_staging_files.append(tmp_out)
                                    emit({"event": "phase", "phase": "copy"})
                                    _copy_with_progress(tmp_out, dest, on_bytes)
                        else:
                            emit({"event": "phase", "phase": "copy"})
                            _copy_with_progress(src, dest, on_bytes)

                        final_size = dest.stat().st_size
                        conn.execute("""
                            INSERT INTO export_files
                                (target_id, game_id, disc_number, source_file, exported_file, file_size, exported_at)
                            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                            ON CONFLICT(target_id, game_id, disc_number) DO UPDATE SET
                                source_file = excluded.source_file,
                                exported_file = excluded.exported_file,
                                file_size = excluded.file_size,
                                exported_at = CURRENT_TIMESTAMP;
                        """, (target_uuid, item["game_id"], item["disc_number"], str(src), str(dest), final_size))
                        conn.commit()

                        exported_count += 1
                        written_bytes += final_size

                        # Multi-disc tracking
                        if sys_prof and sys_prof.multidisc_m3u and extract_disc_number(dest.name):
                            bname = get_disc_family(dest.stem)
                            multidisc_tracker[dest_dir][bname].append(dest.name)

                    except Exception as e:
                        errors.append(f"Error exporting {game['title']}: {e}")
                    finally:
                        _discard_staging(tmp_staging_files)

                emit({"event": "game_done", "index": index})

            # 5. Generate .m3u files on target
            if not dry_run:
                for target_dir, bases in multidisc_tracker.items():
                    for bname, disc_files in bases.items():
                        if len(disc_files) > 1:
                            m3u_path = target_dir / f"{bname}.m3u"
                            m3u_content = generate_m3u_content(disc_files)
                            m3u_path.write_text(m3u_content, encoding="utf-8")

            # 6. Files on the card that match no game of the collection.
            unmanaged: List[Dict[str, Any]] = []
            for sys_k, stems in known_stems.items():
                sys_prof = profile.get_system(sys_k)
                sys_sub = sys_prof.dir if sys_prof else sys_k
                scan_dir = mount_path / profile.roms_subpath / sys_sub
                if not scan_dir.is_dir():
                    continue
                for path in sorted(scan_dir.rglob("*")):
                    if not path.is_file():
                        continue
                    if path.name.startswith(".") or path.suffix.lower() in CARD_NOISE_EXTENSIONS:
                        continue
                    if path.stem in stems:
                        continue
                    unmanaged.append({
                        "system": normalize_system(sys_k),
                        "file": str(path),
                        "name": path.name,
                        "size_bytes": path.stat().st_size,
                    })

    def public(game: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in game.items() if not key.startswith("_")}

    def by_system_title(game: Dict[str, Any]) -> tuple:
        return (game["system"], (game["title"] or "").lower())

    planned_list = sorted((public(g) for g in planned_games.values()), key=by_system_title)
    present_list = sorted((public(g) for g in present_games.values()), key=by_system_title)
    waiting_list = sorted((public(g) for g in waiting_games.values()), key=by_system_title)

    counts = {
        "planned_games": len(planned_list),
        "planned_files": sum(g["file_count"] for g in planned_list),
        "planned_bytes": sum(g["size_bytes"] for g in planned_list),
        "outdated_games": sum(1 for g in planned_list if g.get("status") == "outdated"),
        "present_games": len(present_list),
        "present_files": sum(g["file_count"] for g in present_list),
        "present_bytes": sum(g["size_bytes"] for g in present_list),
        "unmanaged_files": len(unmanaged),
        "waiting_games": len(waiting_list),
    }

    emit({"event": "done", **counts})

    return {
        "target": target_name,
        "profile": target_cfg.profile,
        "uuid": target_uuid,
        "mount_path": str(mount_path),
        "free_bytes": free_bytes,
        "dry_run": dry_run,
        "exported": exported_count,
        "written_bytes": written_bytes,
        "skipped": skipped_files,
        "counts": counts,
        "planned": planned_list,
        "present": present_list,
        "unmanaged": unmanaged,
        "destination_only": unmanaged,
        "waiting": waiting_list,
        "errors": errors,
    }


def prepare_export(
    system: Optional[str] = None,
    tier: Optional[int] = None,
    raw: bool = False,
    dry_run: bool = False,
    profile_name: str = "esde",
    config: Optional[Config] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Converts game files in the local staging, without writing to a target card."""
    cfg = config or load_config()
    profile = load_profile(profile_name)
    staging_root = get_staging_dir() / "prepare-export"

    allowed_systems = get_tier_systems(tier) if tier else None
    prepared = 0
    skipped = 0
    errors: list[str] = []

    with file_lock(cfg.paths.data_dir):
        with get_db_connection(db_path) as conn:
            backfill_game_files(conn)
            rows = conn.execute(
                "SELECT g.id, g.title, g.system, gf.disc_number, gf.file_path, gf.file_name "
                "FROM games g JOIN game_files gf ON gf.game_id = g.id "
                "WHERE g.state IN ('downloaded', 'linked') "
                "ORDER BY g.id, gf.disc_number;"
            ).fetchall()

            for g in rows:
                sys_k = normalize_system(g["system"]).lower()
                if system and sys_k != normalize_system(system).lower():
                    continue
                if allowed_systems is not None and sys_k not in allowed_systems:
                    continue

                file_rel = g["file_path"] or g["file_name"] or ""
                sys_prof = profile.get_system(sys_k)
                sys_subfolder = sys_prof.dir if sys_prof else sys_k

                src = cfg.paths.download_dir / file_rel
                if not src.exists():
                    src = cfg.paths.library_dir / sys_subfolder / Path(file_rel).name
                if not src.is_file():
                    skipped += 1
                    continue

                rule = "copy" if raw else (sys_prof.export_format if sys_prof else "copy")
                if rule == "copy":
                    skipped += 1
                    continue

                if dry_run:
                    prepared += 1
                    continue

                dest_dir = staging_root / sys_subfolder
                target_ext = sys_prof.target_ext if sys_prof and sys_prof.target_ext else src.suffix
                dest = dest_dir / (src.stem + target_ext)

                tmp_files = []
                try:
                    if dest.is_file():
                        prepared += 1
                        continue
                    if rule == "extract":
                        if zipfile.is_zipfile(src):
                            extract_from_archive(src, dest_dir, sys_prof.extensions if sys_prof else None)
                    elif rule == "chd" and src.suffix.lower() != ".chd":
                        work_src = _unpack_if_archive(src, dest_dir, sys_prof, tmp_files)
                        if work_src.suffix.lower() == ".chd":
                            shutil.copy2(work_src, dest)
                        else:
                            convert_to_chd(work_src, dest, is_dvd=sys_k in ("ps2", "psp"))
                    elif rule == "rvz" and src.suffix.lower() != ".rvz":
                        work_src = _unpack_if_archive(src, dest_dir, sys_prof, tmp_files)
                        if work_src.suffix.lower() == ".rvz":
                            shutil.copy2(work_src, dest)
                        else:
                            convert_to_rvz(work_src, dest)
                    elif rule not in ("extract", "chd", "rvz"):
                        skipped += 1
                        continue
                    prepared += 1
                except Exception as e:
                    errors.append(f"Preparation error for {g['title']}: {e}")
                finally:
                    # Extracted archives are only intermediate files;
                    # the final converted file stays in staging.
                    _discard_staging(tmp_files)

    return {
        "profile": profile_name,
        "staging_dir": str(staging_root),
        "dry_run": dry_run,
        "prepared": prepared,
        "skipped": skipped,
        "errors": errors,
    }
