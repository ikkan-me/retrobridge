from typing import Dict, Any, Optional, List
from collections import defaultdict

from retrobridge.config import Config, load_config
from retrobridge.storage.db import backfill_game_files, get_db_connection
from retrobridge.adapters.transmission import TransmissionClient


# Very large packs (12,000+ files) stall the Transmission RPC:
# we apply in small batches with a generous timeout.
APPLY_CHUNK_SIZE = 200


def _build_client(cfg: Config) -> TransmissionClient:
    return TransmissionClient(
        rpc_url=cfg.transmission.rpc_url,
        username=cfg.transmission.user,
        password=cfg.transmission.password,
        timeout=max(cfg.transmission.timeout, 120.0),
    )


def apply_selection(
    config: Optional[Config] = None,
    db_path: Optional[str] = None,
    dry_run: bool = False,
    client: Optional[TransmissionClient] = None,
) -> Dict[str, Any]:
    """Aligns Transmission priorities with the actual game selection.

    For each torrent that has at least one game, only the files of those games
    stay "wanted"; all others are unselected (no useless bytes).
    """
    cfg = config or load_config()

    with get_db_connection(db_path) as conn:
        backfill_game_files(conn)
        rows = conn.execute(
            "SELECT torrent_id, file_index, is_wanted FROM torrent_cache;"
        ).fetchall()

        wanted_by_torrent: Dict[int, set] = defaultdict(set)
        for row in conn.execute("""
            SELECT torrent_id AS tid, file_index AS idx
            FROM game_files gf JOIN games g ON g.id = gf.game_id
            WHERE g.state != 'removing'
              AND torrent_id IS NOT NULL AND file_index IS NOT NULL
            UNION
            SELECT matched_torrent_id AS tid, matched_file_index AS idx
            FROM games WHERE state != 'removing'
              AND matched_torrent_id IS NOT NULL AND matched_file_index IS NOT NULL;
        """).fetchall():
            wanted_by_torrent[row["tid"]].add(row["idx"])

        if not wanted_by_torrent:
            return {
                "dry_run": dry_run,
                "torrents_processed": 0,
                "files_wanted": 0,
                "files_unwanted": 0,
                "details": [],
                "errors": [],
            }

        current: Dict[int, set] = defaultdict(set)
        total_by_torrent: Dict[int, int] = defaultdict(int)
        for row in rows:
            total_by_torrent[row["torrent_id"]] += 1
            if row["is_wanted"]:
                current[row["torrent_id"]].add(row["file_index"])

        rpc = client or _build_client(cfg)
        if not dry_run:
            try:
                if not rpc.test_connection():
                    raise RuntimeError("Transmission unreachable")
            except Exception as e:
                return {
                    "dry_run": dry_run,
                    "torrents_processed": 0,
                    "files_wanted": 0,
                    "files_unwanted": 0,
                    "details": [],
                    "errors": [f"Apply skipped: {e}"],
                }

        details: List[Dict[str, Any]] = []
        errors: List[str] = []
        files_wanted = 0
        files_unwanted = 0

        for tid in sorted(wanted_by_torrent):
            desired = wanted_by_torrent[tid]
            already = current.get(tid, set())

            to_want = sorted(desired - already)
            to_unwant = sorted(already - desired)

            # First apply on a never-scanned torrent: everything else
            # must also be unselected.
            all_indices = set(range(total_by_torrent.get(tid, 0)))
            if not already and all_indices:
                to_unwant = sorted(all_indices - desired)

            if not to_want and not to_unwant:
                continue

            details.append({
                "torrent_id": tid,
                "wanted": len(to_want),
                "unwanted": len(to_unwant),
            })

            if dry_run:
                files_wanted += len(to_want)
                files_unwanted += len(to_unwant)
                continue

            applied_wanted = 0
            applied_unwanted = 0
            total = max(len(to_want), len(to_unwant))
            try:
                for start in range(0, total, APPLY_CHUNK_SIZE):
                    chunk_wanted = to_want[start:start + APPLY_CHUNK_SIZE]
                    chunk_unwanted = to_unwant[start:start + APPLY_CHUNK_SIZE]
                    rpc.set_file_priorities(
                        tid,
                        wanted_indices=chunk_wanted,
                        unwanted_indices=chunk_unwanted,
                    )
                    # The cache is updated batch by batch: a late timeout
                    # does not let the DB lie about what was applied.
                    if chunk_wanted:
                        conn.executemany(
                            "UPDATE torrent_cache SET is_wanted = 1 WHERE torrent_id = ? AND file_index = ?;",
                            [(tid, idx) for idx in chunk_wanted],
                        )
                    if chunk_unwanted:
                        conn.executemany(
                            "UPDATE torrent_cache SET is_wanted = 0 WHERE torrent_id = ? AND file_index = ?;",
                            [(tid, idx) for idx in chunk_unwanted],
                        )
                    conn.commit()
                    applied_wanted += len(chunk_wanted)
                    applied_unwanted += len(chunk_unwanted)
            except Exception as e:
                errors.append(
                    f"Torrent #{tid}: {e} "
                    f"(partial: {applied_wanted} enabled, {applied_unwanted} unselected)"
                )
                files_wanted += applied_wanted
                files_unwanted += applied_unwanted
                continue

            files_wanted += applied_wanted
            files_unwanted += applied_unwanted

        if not dry_run:
            conn.commit()

    return {
        "dry_run": dry_run,
        "torrents_processed": len(details),
        "files_wanted": files_wanted,
        "files_unwanted": files_unwanted,
        "details": details,
        "errors": errors,
    }
