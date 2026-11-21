import time
from typing import Any, Callable, Dict, Optional

from retrobridge.config import Config, load_config
from retrobridge.storage.db import get_db_connection, init_db, set_meta
from retrobridge.adapters.transmission import TransmissionClient
from retrobridge.domain.scoring import normalize_text

ProgressCallback = Callable[[str, int, int], None]


def _report(progress: Optional[ProgressCallback], stage: str, current: int = 0, total: int = 0) -> None:
    if progress:
        progress(stage, current, total)


def scan_torrents(
    config: Optional[Config] = None,
    db_path: Optional[str] = None,
    progress: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    """Scanne Transmission de façon incrémentale.

    - une passe légère compare (nom, nombre de fichiers, taille totale) ;
    - les torrents inchangés ne rechargent que `fileStats` (wanted/progression) ;
    - seuls les torrents nouveaux/modifiés rechargent la liste `files`.
    """
    started = time.perf_counter()
    cfg = config or load_config()
    init_db(db_path)

    client = TransmissionClient(
        rpc_url=cfg.transmission.rpc_url,
        username=cfg.transmission.user,
        password=cfg.transmission.password,
        timeout=cfg.transmission.timeout,
    )

    _report(progress, "Connexion à Transmission")
    torrents = client.get_torrents(fields=["id", "name", "file-count", "totalSize"])
    _report(progress, "Torrents lus", len(torrents), len(torrents))

    with get_db_connection(db_path) as conn:
        meta = {
            row["torrent_id"]: row
            for row in conn.execute(
                "SELECT torrent_id, name, file_count, total_size FROM torrent_meta;"
            ).fetchall()
        }
        cache_counts = {
            row["torrent_id"]: row["n"]
            for row in conn.execute(
                "SELECT torrent_id, COUNT(*) AS n FROM torrent_cache GROUP BY torrent_id;"
            ).fetchall()
        }

        changed = []
        unchanged = []
        for torrent in torrents:
            tid = torrent["id"]
            name = torrent.get("name", "")
            count = int(torrent.get("file-count") or torrent.get("fileCount") or 0)
            total_size = int(torrent.get("totalSize") or 0)
            old = meta.get(tid)
            signature_changed = (
                old is None
                or old["name"] != name
                or int(old["file_count"]) != count
                or int(old["total_size"]) != total_size
                or cache_counts.get(tid, 0) != count
            )
            (changed if signature_changed else unchanged).append(torrent)

        # Unchanged torrents only refresh wanted/bytesCompleted.
        if unchanged:
            _report(progress, "États des torrents", 0, len(unchanged))
            ids = [t["id"] for t in unchanged]
            stats_data = client.request(
                "torrent-get", {"ids": ids, "fields": ["id", "fileStats"]}
            ).get("arguments", {}).get("torrents", [])
            stats_by_id = {t["id"]: t.get("fileStats", []) for t in stats_data}

            fallback = []
            for torrent in unchanged:
                tid = torrent["id"]
                expected = int(torrent.get("file-count") or torrent.get("fileCount") or 0)
                stats = stats_by_id.get(tid, [])
                if len(stats) != expected:
                    fallback.append(torrent)
                    continue
                conn.executemany(
                    "UPDATE torrent_cache SET is_wanted = ?, bytes_completed = ? "
                    "WHERE torrent_id = ? AND file_index = ?;",
                    [
                        (1 if stat.get("wanted", False) else 0,
                         int(stat.get("bytesCompleted", 0)), tid, index)
                        for index, stat in enumerate(stats)
                    ],
                )
            changed.extend(fallback)

        # Modified torrents reload the file list.
        if changed:
            _report(progress, "Fichiers des torrents", 0, len(changed))
            ids = [t["id"] for t in changed]
            data = client.request(
                "torrent-get", {"ids": ids, "fields": ["id", "name", "files", "fileStats"]}
            ).get("arguments", {}).get("torrents", [])
            for index, torrent in enumerate(data, start=1):
                tid = torrent["id"]
                name = torrent.get("name", "")
                files = torrent.get("files", [])
                stats = torrent.get("fileStats", [])
                conn.execute("DELETE FROM torrent_cache WHERE torrent_id = ?;", (tid,))
                conn.executemany(
                    "INSERT INTO torrent_cache "
                    "(torrent_id, torrent_name, file_index, file_name, file_length, is_wanted, bytes_completed, "
                    "file_name_norm, file_name_compact) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
                    [
                        (tid, name, idx, f.get("name", ""), f.get("length", 0),
                         1 if (idx < len(stats) and stats[idx].get("wanted", False)) else 0,
                         f.get("bytesCompleted", 0),
                         normalize_text(f.get("name", "")),
                         normalize_text(f.get("name", "")).replace(" ", ""))
                        for idx, f in enumerate(files)
                    ],
                )
                _report(progress, "Fichiers des torrents", index, len(changed))

        # Clean up vanished torrents.
        present_ids = {t["id"] for t in torrents}
        existing_ids = {row["torrent_id"] for row in conn.execute("SELECT DISTINCT torrent_id FROM torrent_cache;")}
        for tid in existing_ids - present_ids:
            conn.execute("DELETE FROM torrent_cache WHERE torrent_id = ?;", (tid,))
            conn.execute("DELETE FROM torrent_meta WHERE torrent_id = ?;", (tid,))

        conn.executemany(
            "INSERT INTO torrent_meta (torrent_id, name, file_count, total_size, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(torrent_id) DO UPDATE SET "
            "name = excluded.name, file_count = excluded.file_count, "
            "total_size = excluded.total_size, updated_at = CURRENT_TIMESTAMP;",
            [
                (t["id"], t.get("name", ""),
                 int(t.get("file-count") or t.get("fileCount") or 0),
                 int(t.get("totalSize") or 0))
                for t in torrents
            ],
        )
        set_meta(conn, "last_scan", "completed")
        files_cached = conn.execute("SELECT COUNT(*) AS n FROM torrent_cache;").fetchone()["n"]
        conn.commit()

    _report(progress, "Écriture terminée", 1, 1)
    return {
        "torrents_count": len(torrents),
        "files_cached": files_cached,
        "changed_torrents": len(changed),
        "unchanged_torrents": max(0, len(torrents) - len(changed)),
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }
