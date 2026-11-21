import shutil
import zipfile
from pathlib import Path
from typing import Dict, Any, Optional
from collections import defaultdict

from retrobridge.config import Config, load_config
from retrobridge.storage.db import backfill_game_files, get_db_connection
from retrobridge.storage.lock import file_lock
from retrobridge.profiles.loader import load_profile
from retrobridge.domain.tiers import get_tier_systems
from retrobridge.domain.target import ensure_target_uuid
from retrobridge.domain.multidisc import extract_disc_number, get_base_title, generate_m3u_content
from retrobridge.domain.systems import normalize_system
from retrobridge.adapters.compress import get_staging_dir, extract_from_archive, convert_to_chd, convert_to_rvz

class ExportError(Exception):
    pass


def _unpack_if_archive(src: Path, staging_dir: Path, sys_prof, tmp_files: list) -> Path:
    """Extrait une archive zip dans le staging avant conversion (chd/rvz)."""
    if zipfile.is_zipfile(src):
        extracted = extract_from_archive(
            src, staging_dir, sys_prof.extensions if sys_prof else None
        )
        tmp_files.append(extracted)
        return extracted
    return src

def export_to_target(
    target_name: str,
    system: Optional[str] = None,
    tier: Optional[int] = None,
    raw: bool = False,
    dry_run: bool = False,
    config: Optional[Config] = None,
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    cfg = config or load_config()

    if target_name not in cfg.targets:
        raise ExportError(f"Cible inconnue: '{target_name}'. Cibles disponibles: {list(cfg.targets.keys())}")

    target_cfg = cfg.targets[target_name]
    mount_path = Path(target_cfg.mount_path)
    if not mount_path.is_dir():
        raise ExportError(f"Le point de montage de la cible '{target_name}' n'est pas accessible: {mount_path}")

    # 1. Identify the target card (.retrobridge-target)
    target_uuid = ensure_target_uuid(mount_path, target_cfg.uuid, name=target_name, profile=target_cfg.profile)
    profile = load_profile(target_cfg.profile)
    staging_dir = get_staging_dir()

    allowed_systems = None
    if tier:
        allowed_systems = get_tier_systems(tier)

    # 2. Fetch games ready for export
    with file_lock(cfg.paths.data_dir):
        with get_db_connection(db_path) as conn:
            backfill_game_files(conn)
            cur = conn.cursor()
            cur.execute("""
                SELECT g.id, g.title, g.system,
                       gf.disc_number, gf.file_path, gf.file_name
                FROM games g
                JOIN game_files gf ON gf.game_id = g.id
                WHERE g.state IN ('downloaded', 'linked')
                ORDER BY g.id, gf.disc_number;
            """)
            all_files = cur.fetchall()

            # System and tier filters
            files_to_consider = []
            for row in all_files:
                sys_k = normalize_system(row["system"]).lower()
                if system and sys_k != normalize_system(system).lower():
                    continue
                if allowed_systems is not None and sys_k not in allowed_systems:
                    continue
                files_to_consider.append(row)

            # Known names (all states) for destination-only reconciliation.
            known_stems: Dict[str, set] = defaultdict(set)
            for row in conn.execute(
                "SELECT g.system, gf.file_name FROM games g JOIN game_files gf ON gf.game_id = g.id;"
            ).fetchall():
                sys_k = normalize_system(row["system"]).lower()
                if system and sys_k != normalize_system(system).lower():
                    continue
                if allowed_systems is not None and sys_k not in allowed_systems:
                    continue
                known_stems[sys_k].add(Path(row["file_name"] or "").stem)

            # 3. Incremental check (diff against export_files and disk)
            pending_exports = []
            skipped_count = 0

            for row in files_to_consider:
                g_id = row["id"]
                disc_number = row["disc_number"]
                file_rel = row["file_path"] or row["file_name"] or ""

                sys_k = normalize_system(row["system"]).lower()
                sys_prof = profile.get_system(sys_k)
                sys_subfolder = sys_prof.dir if sys_prof else sys_k

                # Source: Download-Dir takes priority (needed for seeding),
                # falling back to the Library-Dir hard link if it was removed.
                src_path = cfg.paths.download_dir / file_rel
                if not src_path.exists():
                    src_path = cfg.paths.library_dir / sys_subfolder / Path(file_rel).name
                if not src_path.is_file():
                    continue

                dest_dir = mount_path / profile.roms_subpath / sys_subfolder

                # Export rule
                rule = "copy" if raw else (sys_prof.export_format if sys_prof else "copy")
                target_ext = (sys_prof.target_ext if sys_prof and sys_prof.target_ext else Path(file_rel).suffix) if not raw else Path(file_rel).suffix
                expected_dest_file = dest_dir / (Path(file_rel).stem + target_ext)

                # Check the export_files table
                cur.execute(
                    "SELECT file_size, exported_file FROM export_files "
                    "WHERE target_id = ? AND game_id = ? AND disc_number = ?",
                    (target_uuid, g_id, disc_number),
                )
                exp_row = cur.fetchone()

                if exp_row and expected_dest_file.is_file():
                    # File already present with matching size
                    if expected_dest_file.stat().st_size == exp_row["file_size"]:
                        skipped_count += 1
                        continue

                pending_exports.append({
                    "game": row,
                    "disc_number": disc_number,
                    "src_path": src_path,
                    "dest_dir": dest_dir,
                    "dest_file": expected_dest_file,
                    "rule": rule,
                    "sys_prof": sys_prof
                })

            # 4. Check free space on target
            total_estimated_bytes = sum(item["src_path"].stat().st_size for item in pending_exports)
            stat_vfs = shutil.disk_usage(mount_path)
            if stat_vfs.free < total_estimated_bytes:
                free_mb = round(stat_vfs.free / (1024 * 1024), 1)
                req_mb = round(total_estimated_bytes / (1024 * 1024), 1)
                raise ExportError(f"Espace insuffisant sur la cible '{target_name}': {free_mb} Mo libres, {req_mb} Mo requis.")

            # 5. Run the export
            exported_count = 0
            errors = []
            multidisc_tracker = defaultdict(lambda: defaultdict(list))

            for item in pending_exports:
                g = item["game"]
                disc_number = item["disc_number"]
                src = item["src_path"]
                dest = item["dest_file"]
                dest_dir = item["dest_dir"]
                rule = item["rule"]
                sys_prof = item["sys_prof"]

                if dry_run:
                    exported_count += 1
                    continue

                if dest_dir.is_symlink() and not dest_dir.exists():
                    errors.append(
                        f"Lien cassé vers le répertoire {dest_dir} : à recréer ou supprimer."
                    )
                    continue
                if dest_dir.exists() and not dest_dir.is_dir():
                    errors.append(f"Le chemin {dest_dir} existe mais n'est pas un répertoire.")
                    continue

                dest_dir.mkdir(parents=True, exist_ok=True)
                tmp_staging_files = []

                try:
                    if rule == "copy" or raw:
                        shutil.copy2(src, dest)
                    elif rule == "extract":
                        if not zipfile.is_zipfile(src):
                            # Already in the right format (e.g. .sfc outside an archive).
                            shutil.copy2(src, dest)
                        else:
                            extracted = extract_from_archive(src, staging_dir, sys_prof.extensions if sys_prof else None)
                            tmp_staging_files.append(extracted)
                            shutil.copy2(extracted, dest)
                    elif rule == "chd" and src.suffix.lower() == ".chd":
                        shutil.copy2(src, dest)
                    elif rule == "chd":
                        if shutil.which("chdman"):
                            work_src = _unpack_if_archive(src, staging_dir, sys_prof, tmp_staging_files)
                            is_dvd = (normalize_system(g["system"]).lower() in ("ps2", "psp"))
                            tmp_chd = staging_dir / (src.stem + ".chd")
                            convert_to_chd(work_src, tmp_chd, is_dvd=is_dvd)
                            tmp_staging_files.append(tmp_chd)
                            shutil.copy2(tmp_chd, dest)
                        else:
                            shutil.copy2(src, dest)
                    elif rule == "rvz" and src.suffix.lower() == ".rvz":
                        shutil.copy2(src, dest)
                    elif rule == "rvz":
                        if shutil.which("dolphin-tool"):
                            work_src = _unpack_if_archive(src, staging_dir, sys_prof, tmp_staging_files)
                            tmp_rvz = staging_dir / (src.stem + ".rvz")
                            convert_to_rvz(work_src, tmp_rvz)
                            tmp_staging_files.append(tmp_rvz)
                            shutil.copy2(tmp_rvz, dest)
                        else:
                            shutil.copy2(src, dest)

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
                    """, (target_uuid, g["id"], disc_number, str(src), str(dest), final_size))
                    conn.commit()

                    exported_count += 1

                    # Multi-disc tracking
                    fname = dest.name
                    if sys_prof and sys_prof.multidisc_m3u and extract_disc_number(fname):
                        bname = get_base_title(dest.stem)
                        multidisc_tracker[dest_dir][bname].append(fname)

                except Exception as e:
                    errors.append(f"Erreur lors de l'export de {g['title']}: {e}")
                finally:
                    for tmp_file in tmp_staging_files:
                        if tmp_file and tmp_file.exists():
                            try:
                                tmp_file.unlink()
                            except OSError:
                                pass

            # 6. Generate .m3u files on target
            if not dry_run:
                for target_dir, bases in multidisc_tracker.items():
                    for bname, disc_files in bases.items():
                        if len(disc_files) > 1:
                            m3u_path = target_dir / f"{bname}.m3u"
                            m3u_content = generate_m3u_content(disc_files)
                            m3u_path.write_text(m3u_content, encoding="utf-8")

            # Files present on target with no known source counterpart.
            destination_only = []
            for sys_k, stems in known_stems.items():
                sys_prof = profile.get_system(sys_k)
                sys_sub = sys_prof.dir if sys_prof else sys_k
                dest_dir = mount_path / profile.roms_subpath / sys_sub
                if not dest_dir.is_dir():
                    continue
                for path in dest_dir.rglob("*"):
                    if not path.is_file() or path.suffix.lower() == ".m3u":
                        continue
                    if path.stem not in stems:
                        destination_only.append({
                            "system": normalize_system(sys_k),
                            "file": str(path),
                            "size_bytes": path.stat().st_size,
                        })

    return {
        "target": target_name,
        "destination_only": destination_only,
        "uuid": target_uuid,
        "profile": target_cfg.profile,
        "dry_run": dry_run,
        "exported": exported_count,
        "skipped": skipped_count,
        "errors": errors
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
    """Convertit les ROMs dans le staging local, sans écrire sur une carte cible."""
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
                        convert_to_chd(work_src, dest, is_dvd=sys_k in ("ps2", "psp"))
                    elif rule == "rvz" and src.suffix.lower() != ".rvz":
                        work_src = _unpack_if_archive(src, dest_dir, sys_prof, tmp_files)
                        convert_to_rvz(work_src, dest)
                    elif rule not in ("extract", "chd", "rvz"):
                        skipped += 1
                        continue
                    prepared += 1
                except Exception as e:
                    errors.append(f"Erreur de préparation pour {g['title']}: {e}")
                finally:
                    # Extracted archives are only intermediate files;
                    # the final converted file stays in staging.
                    for tmp_file in tmp_files:
                        if tmp_file.exists():
                            tmp_file.unlink(missing_ok=True)

    return {
        "profile": profile_name,
        "staging_dir": str(staging_root),
        "dry_run": dry_run,
        "prepared": prepared,
        "skipped": skipped,
        "errors": errors,
    }
