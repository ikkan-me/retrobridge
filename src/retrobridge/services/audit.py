from pathlib import Path
from typing import Dict, Any, Optional, List

from retrobridge.config import Config, load_config
from retrobridge.storage.db import get_db_connection
from retrobridge.profiles.loader import load_profile
from retrobridge.domain.systems import normalize_system


def audit_collection(
    config: Optional[Config] = None,
    db_path: Optional[str] = None,
    profile_name: str = "esde",
) -> Dict[str, Any]:
    """Checks consistency: "each game = a real file from an indexed pack"."""
    cfg = config or load_config()
    profile = load_profile(profile_name)

    with get_db_connection(db_path) as conn:
        without_pack_file = [dict(r) for r in conn.execute("""
            SELECT g.id, g.title, g.system, g.state
            FROM games g
            LEFT JOIN torrent_cache tc
              ON g.matched_torrent_id = tc.torrent_id
             AND g.matched_file_index = tc.file_index
            WHERE tc.file_name IS NULL
            ORDER BY g.system, g.title;
        """).fetchall()]

        without_name = [dict(r) for r in conn.execute("""
            SELECT id, title, system, state
            FROM games
            WHERE matched_file_name IS NULL OR matched_file_name = ''
            ORDER BY system, title;
        """).fetchall()]

        candidates = [dict(r) for r in conn.execute("""
            SELECT id, title, system, state, matched_file_path, matched_file_name
            FROM games
            WHERE state IN ('downloaded', 'linked')
            ORDER BY system, title;
        """).fetchall()]

        duplicate_paths = [dict(r) for r in conn.execute("""
            SELECT matched_file_path, COUNT(*) as c,
                   GROUP_CONCAT(title, ' | ') AS titles
            FROM games
            WHERE matched_file_path IS NOT NULL AND matched_file_path != ''
            GROUP BY matched_file_path HAVING c > 1;
        """).fetchall()]

        # Games that Transmission reports as complete while the file is gone.
        stale_rows = [dict(r) for r in conn.execute("""
            SELECT g.id, g.title, g.system, g.state,
                   gf.torrent_id, gf.file_index, gf.file_path, gf.file_name,
                   tc.file_length, tc.bytes_completed
            FROM games g
            JOIN game_files gf ON gf.game_id = g.id
            JOIN torrent_cache tc
              ON tc.torrent_id = gf.torrent_id AND tc.file_index = gf.file_index
            WHERE g.state = 'downloading'
              AND tc.file_length > 0
              AND tc.bytes_completed >= tc.file_length
            ORDER BY g.system, g.title;
        """).fetchall()]

        referenced_names = set()
        for row in conn.execute(
            "SELECT matched_file_name FROM games "
            "UNION SELECT file_name FROM game_files;"
        ):
            if row[0]:
                referenced_names.add(Path(row[0]).name)

        rel_by_name: Dict[str, str] = {}
        for row in conn.execute("SELECT file_name FROM torrent_cache;"):
            if row[0]:
                rel_by_name[Path(row[0]).name] = row[0]

    missing_on_disk: List[Dict[str, Any]] = []
    for game in candidates:
        rel = game["matched_file_path"] or game["matched_file_name"] or ""
        name = Path(rel).name
        sys_prof = profile.get_system(normalize_system(game["system"]))
        sub = sys_prof.dir if sys_prof else normalize_system(game["system"])

        in_download = bool(rel) and (cfg.paths.download_dir / rel).is_file()
        in_library = bool(name) and (cfg.paths.library_dir / sub / name).is_file()
        if not in_download and not in_library:
            missing_on_disk.append(game)

    stale_resume: List[Dict[str, Any]] = []
    for game in stale_rows:
        rel = game["file_path"] or game["file_name"] or ""
        if not rel:
            continue
        if (cfg.paths.download_dir / rel).is_file():
            continue
        if (cfg.paths.download_dir / f"{rel}.part").is_file():
            continue
        stale_resume.append(game)

    orphan_library: List[Dict[str, Any]] = []
    if cfg.paths.library_dir.is_dir():
        for path in cfg.paths.library_dir.rglob("*"):
            if path.is_dir():
                continue
            if path.suffix.lower() == ".m3u":
                continue
            # Infrastructure symlinks (ps1, sfc...) and backups are not games.
            if not path.suffix:
                continue
            if path.suffix.lower() in {".bak", ".old", ".tmp", ".disabled"}:
                continue
            if path.name.startswith("."):
                continue
            if path.name in referenced_names:
                continue
            if path.is_symlink() and not path.exists():
                size = 0
            elif path.is_file():
                size = path.stat().st_size
            else:
                continue
            rel = rel_by_name.get(path.name)
            download_exists = bool(rel) and (cfg.paths.download_dir / rel).is_file()
            orphan_library.append({
                "file": str(path),
                "name": path.name,
                "bytes": size,
                "download_exists": download_exists,
                "is_symlink": path.is_symlink(),
            })

    critical = (
        len(without_pack_file)
        + len(without_name)
        + len(missing_on_disk)
        + len(stale_resume)
    )
    return {
        "ok": critical == 0,
        "games_total": len(candidates),
        "without_pack_file": without_pack_file,
        "without_matched_name": without_name,
        "missing_on_disk": missing_on_disk,
        "stale_resume": stale_resume,
        "duplicate_paths": duplicate_paths,
        "orphan_library": orphan_library,
    }
