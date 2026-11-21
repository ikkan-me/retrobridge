import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from retrobridge.domain.systems import (
    SYSTEM_KEYWORDS,
    normalize_system,
    file_matches_system,
)
from retrobridge.storage.db import get_db_connection, get_db_path, init_db


def _infer_system(matched_file_name: Optional[str]) -> Optional[str]:
    """Infers a canonical system from the pack file path."""
    if not matched_file_name:
        return None
    candidates = [
        key for key in SYSTEM_KEYWORDS
        if file_matches_system(matched_file_name, key)
    ]
    return normalize_system(candidates[0]) if len(candidates) == 1 else None


def migrate_db(db_path: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Aligns the database with canonical system codes (removes v1 aliases)."""
    init_db(db_path)

    renamed = []
    merged = []
    unresolved = []

    with get_db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, system, matched_file_name FROM games ORDER BY id;"
        ).fetchall()

        for row in rows:
            old_system = row["system"]
            new_system = normalize_system(old_system)

            if new_system.lower() not in SYSTEM_KEYWORDS:
                inferred = _infer_system(row["matched_file_name"])
                if inferred:
                    new_system = inferred
                else:
                    unresolved.append({
                        "id": row["id"],
                        "title": row["title"],
                        "system": old_system,
                    })
                    continue

            if new_system == old_system:
                continue

            duplicate = conn.execute(
                "SELECT id FROM games WHERE title = ? AND system = ? AND id != ?;",
                (row["title"], new_system, row["id"]),
            ).fetchone()

            if duplicate:
                # Merge: keep the oldest row, remove the duplicate.
                keep_id, drop_id = sorted((row["id"], duplicate["id"]))
                if not dry_run:
                    conn.execute("DELETE FROM games WHERE id = ?;", (drop_id,))
                merged.append({
                    "title": row["title"],
                    "system": new_system,
                    "kept_id": keep_id,
                    "dropped_id": drop_id,
                })
                continue

            if not dry_run:
                conn.execute(
                    "UPDATE games SET system = ? WHERE id = ?;",
                    (new_system, row["id"]),
                )
            renamed.append({
                "id": row["id"],
                "title": row["title"],
                "from": old_system,
                "to": new_system,
            })

        if not dry_run:
            conn.commit()

    return {
        "dry_run": dry_run,
        "renamed": renamed,
        "merged": merged,
        "unresolved": unresolved,
    }


def backup_db(db_path: Optional[str] = None, destination: Optional[str] = None) -> Path:
    """Copies the SQLite database to a timestamped file."""
    source = get_db_path(db_path)
    if not source.is_file():
        raise FileNotFoundError(f"Database not found: {source}")

    if destination:
        dest = Path(destination).expanduser()
        # A path without an extension is treated as a destination directory.
        if dest.is_dir() or dest.suffix == "":
            dest = dest / f"retrobridge-backup-{datetime.now():%Y%m%d-%H%M%S}.db"
    else:
        dest = source.parent / f"retrobridge-backup-{datetime.now():%Y%m%d-%H%M%S}.db"

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return dest
