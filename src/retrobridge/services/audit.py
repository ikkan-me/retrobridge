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
            SELECT matched_file_path, COUNT(*) as c
            FROM games
            WHERE matched_file_path IS NOT NULL AND matched_file_path != ''
            GROUP BY matched_file_path HAVING c > 1;
        """).fetchall()]

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

    critical = len(without_pack_file) + len(without_name) + len(missing_on_disk)
    return {
        "ok": critical == 0,
        "games_total": len(candidates),
        "without_pack_file": without_pack_file,
        "without_matched_name": without_name,
        "missing_on_disk": missing_on_disk,
        "duplicate_paths": duplicate_paths,
    }
