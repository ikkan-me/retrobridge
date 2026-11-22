import sqlite3
from pathlib import Path
from typing import Dict, Any
from retrobridge.storage.db import get_db_connection, init_db
from retrobridge.domain.systems import normalize_system

def import_legacy_db(legacy_db_path: str, target_db_path: str = None) -> Dict[str, Any]:
    legacy_p = Path(legacy_db_path)
    if not legacy_p.exists():
        raise FileNotFoundError(f"Legacy database not found: {legacy_db_path}")

    init_db(target_db_path)

    legacy_conn = sqlite3.connect(str(legacy_p))
    legacy_conn.row_factory = sqlite3.Row

    stats = {
        "games_inserted": 0,
        "games_skipped": 0,
        "cache_rows_copied": 0
    }

    with get_db_connection(target_db_path) as target_conn:
        # 1. Import games
        legacy_games = legacy_conn.execute("SELECT * FROM games").fetchall()
        for g in legacy_games:
            # A game with no actually matched file does not exist in packs:
            # do not import it (no phantom "wanted"/"match" logic).
            if not g["matched_file_name"]:
                stats["games_skipped"] += 1
                continue

            # Map the old download_status to the new state
            old_status = (g["download_status"] or "").upper()
            if old_status == "LINKED":
                state = "linked"
            elif old_status == "DOWNLOADED":
                state = "downloaded"
            elif old_status == "DOWNLOADING":
                state = "downloading"
            else:
                state = "downloading"

            try:
                target_conn.execute("""
                    INSERT INTO games (
                        title, system, state, is_pinned,
                        matched_torrent_id, matched_file_index, matched_file_path,
                        matched_file_name, match_score, match_lang_tag, created_at
                    ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    g["title"], normalize_system(g["system"]), state,
                    g["matched_torrent_id"], g["matched_file_index"], g["matched_file_path"],
                    g["matched_file_name"], g["match_score"] or 0, g["match_lang_tag"],
                    g["created_at"]
                ))
                stats["games_inserted"] += 1
            except sqlite3.IntegrityError:
                stats["games_skipped"] += 1

        # 2. Copy torrent_cache
        legacy_cache = legacy_conn.execute("SELECT * FROM torrent_cache").fetchall()
        for c in legacy_cache:
            target_conn.execute("""
                INSERT OR REPLACE INTO torrent_cache (
                    torrent_id, torrent_name, file_index, file_name,
                    file_length, is_wanted, bytes_completed
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                c["torrent_id"], c["torrent_name"], c["file_index"], c["file_name"],
                c["file_length"], c["is_wanted"], c["bytes_completed"]
            ))
            stats["cache_rows_copied"] += 1

    legacy_conn.close()
    return stats
