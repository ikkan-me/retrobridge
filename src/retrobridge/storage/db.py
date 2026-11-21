import sqlite3
from pathlib import Path
from typing import Optional

from retrobridge.domain.multidisc import extract_disc_number

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    system TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'downloading', -- downloading, downloaded, linked, removing
    is_pinned INTEGER DEFAULT 0,
    matched_torrent_id INTEGER,
    matched_file_index INTEGER,
    matched_file_path TEXT,
    matched_file_name TEXT,
    match_score INTEGER DEFAULT 0,
    match_lang_tag TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(title, system)
);

CREATE TABLE IF NOT EXISTS torrent_cache (
    torrent_id INTEGER NOT NULL,
    torrent_name TEXT NOT NULL,
    file_index INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    file_length INTEGER DEFAULT 0,
    is_wanted INTEGER DEFAULT 0,
    bytes_completed INTEGER DEFAULT 0,
    file_name_norm TEXT,
    file_name_compact TEXT,
    PRIMARY KEY(torrent_id, file_index)
);
CREATE INDEX IF NOT EXISTS idx_torrent_cache_file ON torrent_cache(file_name);

-- Torrent signatures for incremental scanning.
CREATE TABLE IF NOT EXISTS torrent_meta (
    torrent_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_size INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS exports (
    target_id TEXT NOT NULL,
    game_id INTEGER NOT NULL,
    source_file TEXT NOT NULL,
    exported_file TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    exported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(target_id, game_id),
    FOREIGN KEY(game_id) REFERENCES games(id) ON DELETE CASCADE
);

-- Game files (multi-disc): one row per disc/file.
CREATE TABLE IF NOT EXISTS game_files (
    game_id INTEGER NOT NULL,
    disc_number INTEGER NOT NULL,
    torrent_id INTEGER,
    file_index INTEGER,
    file_path TEXT,
    file_name TEXT,
    file_length INTEGER DEFAULT 0,
    PRIMARY KEY(game_id, disc_number),
    FOREIGN KEY(game_id) REFERENCES games(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_game_files_game ON game_files(game_id);

-- Actual exports, one per file/disc (the `exports` table remains for compatibility).
CREATE TABLE IF NOT EXISTS export_files (
    target_id TEXT NOT NULL,
    game_id INTEGER NOT NULL,
    disc_number INTEGER NOT NULL,
    source_file TEXT NOT NULL,
    exported_file TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    exported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(target_id, game_id, disc_number),
    FOREIGN KEY(game_id) REFERENCES games(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_export_files_game ON export_files(game_id);

CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL,
    target_id TEXT, -- NULL for local actions
    action_type TEXT NOT NULL, -- unselect, unlink_library, delete_download, delete_target
    target_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(game_id) REFERENCES games(id) ON DELETE CASCADE
);

-- A game can only exist if it references a file actually
-- present in a pack (torrent_cache). Prevents any "phantom" / "wanted" game.
CREATE TRIGGER IF NOT EXISTS prevent_phantom_game_insert
BEFORE INSERT ON games
WHEN NEW.matched_file_name IS NULL OR NEW.matched_file_name = ''
BEGIN
    SELECT RAISE(ABORT, 'A game must reference an existing file (matched_file_name required)');
END;

CREATE TRIGGER IF NOT EXISTS prevent_phantom_game_update
BEFORE UPDATE OF matched_file_name ON games
WHEN NEW.matched_file_name IS NULL OR NEW.matched_file_name = ''
BEGIN
    SELECT RAISE(ABORT, 'A game must reference an existing file (matched_file_name required)');
END;

-- "wanted" and "matched" states (separate match logic) are removed:
-- a game can only be added from a real file in packs.
CREATE TRIGGER IF NOT EXISTS prevent_legacy_match_state_insert
BEFORE INSERT ON games
WHEN NEW.state IN ('wanted', 'matched')
BEGIN
    SELECT RAISE(ABORT, 'The wanted/matched states are removed: add an existing game from the packs');
END;

CREATE TRIGGER IF NOT EXISTS prevent_legacy_match_state_update
BEFORE UPDATE OF state ON games
WHEN NEW.state IN ('wanted', 'matched')
BEGIN
    SELECT RAISE(ABORT, 'The wanted/matched states are removed: add an existing game from the packs');
END;
"""

def get_db_path(custom_path: Optional[str] = None) -> Path:
    if custom_path:
        p = Path(custom_path)
    else:
        p = Path.home() / ".local" / "share" / "retrobridge" / "retrobridge.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = get_db_path(db_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def populate_cache_norms(conn: sqlite3.Connection) -> int:
    """Populates the normalized columns used by the SQL search."""
    from retrobridge.domain.scoring import normalize_text

    columns = {row[1] for row in conn.execute("PRAGMA table_info(torrent_cache);")}
    if "file_name_norm" not in columns:
        conn.execute("ALTER TABLE torrent_cache ADD COLUMN file_name_norm TEXT;")
    if "file_name_compact" not in columns:
        conn.execute("ALTER TABLE torrent_cache ADD COLUMN file_name_compact TEXT;")

    rows = conn.execute(
        "SELECT rowid, file_name FROM torrent_cache WHERE file_name_norm IS NULL;"
    ).fetchall()
    for row in rows:
        norm = normalize_text(row["file_name"])
        conn.execute(
            "UPDATE torrent_cache SET file_name_norm = ?, file_name_compact = ? WHERE rowid = ?;",
            (norm, norm.replace(" ", ""), row["rowid"]),
        )
    if rows:
        conn.commit()
    return len(rows)


def backfill_game_files(conn: sqlite3.Connection) -> int:
    """Creates a `game_files` row (disc 1 by default) for legacy games."""
    rows = conn.execute("""
        SELECT g.id, g.matched_torrent_id, g.matched_file_index,
               g.matched_file_path, g.matched_file_name,
               COALESCE(tc.file_length, 0) AS file_length
        FROM games g
        LEFT JOIN torrent_cache tc
          ON g.matched_torrent_id = tc.torrent_id
         AND g.matched_file_index = tc.file_index
        WHERE g.matched_file_name IS NOT NULL AND g.matched_file_name != ''
          AND NOT EXISTS (SELECT 1 FROM game_files gf WHERE gf.game_id = g.id);
    """).fetchall()
    inserted = 0
    for row in rows:
        name = row["matched_file_name"] or ""
        disc = extract_disc_number(Path(name).name) or 1
        conn.execute(
            "INSERT OR IGNORE INTO game_files "
            "(game_id, disc_number, torrent_id, file_index, file_path, file_name, file_length) "
            "VALUES (?, ?, ?, ?, ?, ?, ?);",
            (row["id"], disc, row["matched_torrent_id"], row["matched_file_index"],
             row["matched_file_path"], row["matched_file_name"], row["file_length"]),
        )
        inserted += 1
    if inserted:
        conn.commit()
    return inserted


def init_db(db_path: Optional[str] = None) -> None:
    with get_db_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        populate_cache_norms(conn)
        backfill_game_files(conn)

def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;",
        (key, value)
    )
    conn.commit()

def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
    row = cur.fetchone()
    return row["value"] if row else None

def is_ttl_expired(conn: sqlite3.Connection, key: str, ttl_seconds: int) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT (strftime('%s', 'now') - strftime('%s', updated_at)) AS elapsed_seconds "
        "FROM meta WHERE key = ?",
        (key,)
    )
    row = cur.fetchone()
    if not row or row["elapsed_seconds"] is None:
        return True
    return int(row["elapsed_seconds"]) >= ttl_seconds
