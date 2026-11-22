from pathlib import Path
from typing import Dict, Any, List, Optional

from retrobridge.config import Config, load_config
from retrobridge.domain.multidisc import extract_disc_number
from retrobridge.storage.db import get_db_connection, backfill_game_files, set_meta
from retrobridge.services.search import search_candidates
from retrobridge.services.verify import torrents_needing_verify
from retrobridge.adapters.transmission import TransmissionClient
from retrobridge.domain.systems import normalize_system


class AddGameError(Exception):
    pass


def add_game(
    title: str,
    system: str,
    selected_candidate: Optional[Dict[str, Any]] = None,
    selected_candidates: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Config] = None,
    db_path: Optional[str] = None,
    apply_rpc: bool = True,
) -> Dict[str, Any]:
    cfg = config or load_config()
    canonical_system = normalize_system(system)

    candidates = selected_candidates or ([selected_candidate] if selected_candidate else [])
    if not candidates:
        candidates = search_candidates(query=title, system=system, db_path=db_path)
        if not candidates:
            raise AddGameError(f"No matching game file found in the torrent packs for '{title}'.")
        candidates = [candidates[0]]

    # Disc 1 (or the first available disc) becomes the primary file
    # kept in `games` for UI and legacy script compatibility.
    def disc_key(item: Dict[str, Any]) -> int:
        return extract_disc_number(Path(item["file_name"]).name) or 1

    ordered = sorted(candidates, key=disc_key)
    primary = next((item for item in ordered if disc_key(item) == 1), ordered[0])

    with get_db_connection(db_path) as conn:
        backfill_game_files(conn)
        previous = conn.execute(
            "SELECT id, matched_torrent_id, matched_file_index FROM games "
            "WHERE title = ? AND system = ?;",
            (title, canonical_system),
        ).fetchone()
        previous_files = []
        if previous:
            previous_files = [
                (row["torrent_id"], row["file_index"])
                for row in conn.execute(
                    "SELECT torrent_id, file_index FROM game_files WHERE game_id = ?;",
                    (previous["id"],),
                ).fetchall()
            ]
            if not previous_files and previous["matched_torrent_id"] is not None:
                previous_files = [(previous["matched_torrent_id"], previous["matched_file_index"])]

        conn.execute("""
            INSERT INTO games (
                title, system, state, is_pinned,
                matched_torrent_id, matched_file_index, matched_file_path, matched_file_name,
                match_score, match_lang_tag
            ) VALUES (?, ?, 'downloading', 0, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(title, system) DO UPDATE SET
                matched_torrent_id = excluded.matched_torrent_id,
                matched_file_index = excluded.matched_file_index,
                matched_file_path = excluded.matched_file_path,
                matched_file_name = excluded.matched_file_name,
                match_score = excluded.match_score,
                match_lang_tag = excluded.match_lang_tag,
                state = 'downloading';
        """, (
            title, canonical_system,
            primary["torrent_id"], primary["file_index"], primary["file_name"], primary["file_name"],
            primary["score"], primary["lang_label"]
        ))
        game_id = conn.execute(
            "SELECT id FROM games WHERE title = ? AND system = ?;",
            (title, canonical_system),
        ).fetchone()["id"]

        conn.execute("DELETE FROM game_files WHERE game_id = ?;", (game_id,))
        used_discs = set()
        for index, item in enumerate(ordered, start=1):
            disc = disc_key(item)
            while disc in used_discs:
                disc += 1
            used_discs.add(disc)
            length = int(item.get("file_length") or round(float(item.get("size_mb") or 0) * 1024 * 1024))
            conn.execute(
                "INSERT INTO game_files "
                "(game_id, disc_number, torrent_id, file_index, file_path, file_name, file_length) "
                "VALUES (?, ?, ?, ?, ?, ?, ?);",
                (game_id, disc, item.get("torrent_id"), item.get("file_index"),
                 item.get("file_name"), item.get("file_name"), length),
            )
        conn.commit()

    # Select in Transmission and start torrents.
    download_started = False
    rpc_error = None
    verify_errors: List[str] = []
    verifications: List[int] = []
    new_files = {(item.get("torrent_id"), item.get("file_index")) for item in ordered}
    items_by_key = {
        (item.get("torrent_id"), item.get("file_index")): item for item in ordered
    }
    if apply_rpc:
        try:
            client = TransmissionClient(
                rpc_url=cfg.transmission.rpc_url,
                username=cfg.transmission.user,
                password=cfg.transmission.password,
                timeout=cfg.transmission.timeout,
            )
            by_torrent: Dict[int, List[int]] = {}
            for item in ordered:
                tid = item.get("torrent_id")
                idx = item.get("file_index")
                if tid is not None and idx is not None:
                    by_torrent.setdefault(tid, []).append(idx)
            for tid, indices in by_torrent.items():
                client.set_file_priorities(tid, wanted_indices=indices)
                client.start_torrent(tid)
            download_started = True

            # If a selected file already exists complete on disk but
            # Transmission reports it as empty (re-added pack / lost resume),
            # verify it instead of re-downloading over the existing data.
            for tid, indices in by_torrent.items():
                try:
                    data = client.request(
                        "torrent-get", {"ids": [tid], "fields": ["id", "fileStats"]}
                    ).get("arguments", {}).get("torrents", [])
                    stats = data[0].get("fileStats", []) if data else []
                    rows = []
                    for idx in indices:
                        item = items_by_key.get((tid, idx))
                        if item is None:
                            continue
                        length = int(
                            item.get("file_length")
                            or round(float(item.get("size_mb") or 0) * 1024 * 1024)
                        )
                        rows.append({
                            "torrent_id": tid,
                            "file_index": idx,
                            "file_name": item.get("file_name", ""),
                            "file_length": length,
                            "bytes_completed": (
                                stats[idx].get("bytesCompleted", 0)
                                if idx < len(stats) else 0
                            ),
                        })
                    flagged = torrents_needing_verify(cfg.paths.download_dir, rows)
                    if tid in flagged:
                        client.verify_torrent(tid)
                        verifications.append(tid)
                        with get_db_connection(db_path) as conn:
                            set_meta(conn, f"verify:{tid}", "requested")
                except Exception as exc:
                    verify_errors.append(f"Torrent #{tid}: verification check failed: {exc}")

            # Unselect old files that are no longer used.
            for old_tid, old_idx in previous_files:
                if old_tid is None or old_idx is None or (old_tid, old_idx) in new_files:
                    continue
                with get_db_connection(db_path) as conn:
                    used = conn.execute(
                        "SELECT COUNT(*) AS n FROM game_files WHERE torrent_id = ? AND file_index = ?;",
                        (old_tid, old_idx),
                    ).fetchone()["n"]
                if used == 0:
                    client.set_file_priorities(old_tid, unwanted_indices=[old_idx])
        except Exception as exc:
            rpc_error = str(exc)

    return {
        "title": title,
        "system": canonical_system,
        "matched_file": primary["file_name"],
        "score": primary["score"],
        "lang_label": primary["lang_label"],
        "size_mb": primary.get("size_mb", 0),
        "discs": len(ordered),
        "download_started": download_started,
        "rpc_error": rpc_error,
        "verifications": verifications,
        "verify_errors": verify_errors,
    }
