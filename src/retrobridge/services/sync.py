import os
from pathlib import Path
from typing import Dict, Any, Optional, List
from collections import defaultdict

from retrobridge.config import Config, load_config
from retrobridge.storage.db import backfill_game_files, get_db_connection, init_db, is_ttl_expired, set_meta
from retrobridge.storage.lock import file_lock
from retrobridge.profiles.loader import load_profile
from retrobridge.services.scan import scan_torrents
from retrobridge.services.apply import apply_selection
from retrobridge.adapters.transmission import TransmissionClient
from retrobridge.domain.multidisc import extract_disc_number, get_disc_family, generate_m3u_content
from retrobridge.domain.systems import normalize_system

def run_sync(
    config: Optional[Config] = None,
    db_path: Optional[str] = None,
    profile_name: str = "esde",
    dry_run: bool = False,
    force_scan: bool = False,
    skip_scan: bool = False,
    apply: bool = True,
    execute_pending: bool = True,
    reconcile_transmission: bool = True,
) -> Dict[str, Any]:
    cfg = config or load_config()
    init_db(db_path)
    profile = load_profile(profile_name)

    actions: List[str] = []
    linked_count = 0
    newly_downloaded = 0
    newly_downloading = 0
    pending_executed = 0
    errors: List[str] = []

    with file_lock(cfg.paths.data_dir):
        # 1. Check scan prerequisites via TTL
        with get_db_connection(db_path) as conn:
            should_scan = (not skip_scan) and (force_scan or is_ttl_expired(conn, "last_scan", cfg.ttl.scan_ttl))
            if should_scan:
                try:
                    scan_res = scan_torrents(config=cfg, db_path=db_path)
                    actions.append(f"Transmission scan: {scan_res['torrents_count']} torrents, {scan_res['files_cached']} files.")
                except Exception as e:
                    actions.append(f"Scan skipped: {e}")

            # Guarantee "each game = a real file from an indexed pack".
            orphan_count = conn.execute("""
                SELECT COUNT(*) as c FROM games g
                LEFT JOIN torrent_cache tc
                  ON g.matched_torrent_id = tc.torrent_id
                 AND g.matched_file_index = tc.file_index
                WHERE tc.file_name IS NULL;
            """).fetchone()["c"]
            if orphan_count:
                errors.append(
                    f"{orphan_count} game(s) no longer reference any pack file "
                    "(run 'retrobridge tools audit')."
                )

        # 2. Execute pending actions (local deletions)
        with get_db_connection(db_path) as conn:
            backfill_game_files(conn)
            cur = conn.cursor()
            cur.execute("""
                SELECT pa.id, pa.game_id, pa.action_type, g.title, g.system,
                       g.matched_file_path, g.matched_file_name,
                       g.matched_torrent_id, g.matched_file_index
                FROM pending_actions pa
                JOIN games g ON pa.game_id = g.id
                WHERE pa.target_id IS NULL
            """)
            pending = cur.fetchall() if execute_pending else []

            for p in pending:
                pa_id = p["id"]
                a_type = p["action_type"]
                sys_key = normalize_system(p["system"])
                sys_prof = profile.get_system(sys_key)
                sys_sub = sys_prof.dir if sys_prof else sys_key

                files = conn.execute(
                    "SELECT disc_number, torrent_id, file_index, file_path, file_name "
                    "FROM game_files WHERE game_id = ? ORDER BY disc_number;",
                    (p["game_id"],),
                ).fetchall()
                if not files:
                    files = [{
                        "disc_number": 1,
                        "torrent_id": p["matched_torrent_id"],
                        "file_index": p["matched_file_index"],
                        "file_path": p["matched_file_path"],
                        "file_name": p["matched_file_name"],
                    }]

                if a_type == "unlink_library":
                    for f in files:
                        file_rel = f["file_path"] or f["file_name"] or ""
                        target_file = cfg.paths.library_dir / sys_sub / Path(file_rel).name
                        if target_file.is_file() or target_file.is_symlink():
                            if not dry_run:
                                target_file.unlink()
                            actions.append(f"Link removed: {target_file}")
                    if not dry_run:
                        conn.execute("DELETE FROM pending_actions WHERE id = ?;", (pa_id,))
                    pending_executed += 1

                elif a_type == "delete_download":
                    for f in files:
                        file_rel = f["file_path"] or f["file_name"] or ""
                        raw_file = cfg.paths.download_dir / file_rel
                        used = conn.execute(
                            "SELECT COUNT(*) AS n FROM game_files gf JOIN games g ON g.id = gf.game_id "
                            "WHERE (gf.file_path = ? OR gf.file_name = ?) AND g.state != 'removing';",
                            (file_rel, file_rel),
                        ).fetchone()["n"]
                        if used == 0 and raw_file.exists():
                            if not dry_run:
                                raw_file.unlink()
                            actions.append(f"Raw file removed: {raw_file}")
                    if not dry_run:
                        conn.execute("DELETE FROM pending_actions WHERE id = ?;", (pa_id,))
                    pending_executed += 1

                elif a_type == "unselect":
                    try:
                        client = TransmissionClient(
                            rpc_url=cfg.transmission.rpc_url,
                            username=cfg.transmission.user,
                            password=cfg.transmission.password,
                            timeout=cfg.transmission.timeout,
                        )
                        by_torrent = defaultdict(list)
                        for f in files:
                            if f["torrent_id"] is not None and f["file_index"] is not None:
                                by_torrent[f["torrent_id"]].append(f["file_index"])
                        for tid, indices in by_torrent.items():
                            if not dry_run:
                                client.set_file_priorities(tid, unwanted_indices=indices)
                        actions.append(f"Unselected in Transmission: {p['title']} ({len(files)} file(s))")
                    except Exception:
                        pass
                    if not dry_run:
                        conn.execute("DELETE FROM pending_actions WHERE id = ?;", (pa_id,))
                    pending_executed += 1

            # Purge games with no remaining actions
            if execute_pending:
                cur.execute("""
                    DELETE FROM games WHERE state = 'removing' AND id NOT IN (SELECT game_id FROM pending_actions);
                """)
            conn.commit()

            # 2.4. Apply selection: no unwanted file may
            # stay "wanted" in Transmission (large packs = thousands of files).
            apply_res = {"files_wanted": 0, "files_unwanted": 0, "torrents_processed": 0, "errors": []}
            if apply:
                apply_res = apply_selection(config=cfg, db_path=db_path, dry_run=dry_run)
            if apply_res["files_unwanted"] or apply_res["files_wanted"]:
                actions.append(
                    f"Apply: {apply_res['files_wanted']} files enabled, "
                    f"{apply_res['files_unwanted']} unselected "
                    f"({apply_res['torrents_processed']} torrents)."
                )
            for err in apply_res["errors"]:
                actions.append(f"Apply warning: {err}")

            # 2.5. Transmission reconciliation: ensure requested files are active and torrents are running
            cur.execute("""
                SELECT g.id, g.title, g.matched_torrent_id, g.matched_file_index
                FROM games g
                WHERE g.state = 'downloading'
                  AND g.matched_torrent_id IS NOT NULL
                  AND g.matched_file_index IS NOT NULL;
            """)
            active_games = cur.fetchall()
            files_to_want_by_torrent = defaultdict(list)
            for g in active_games:
                files_to_want_by_torrent[g["matched_torrent_id"]].append(g["matched_file_index"])

            if reconcile_transmission and files_to_want_by_torrent:
                try:
                    t_client = TransmissionClient(
                        rpc_url=cfg.transmission.rpc_url,
                        username=cfg.transmission.user,
                        password=cfg.transmission.password,
                        timeout=cfg.transmission.timeout,
                    )
                    if t_client.test_connection():
                        t_ids = list(files_to_want_by_torrent.keys())
                        t_data = t_client.request("torrent-get", {
                            "ids": t_ids,
                            "fields": ["id", "name", "status", "fileStats"]
                        }).get("arguments", {}).get("torrents", [])

                        for t_item in t_data:
                            tid = t_item["id"]
                            t_status = t_item.get("status", 0)  # 0 = stopped/paused
                            fstats = t_item.get("fileStats", [])
                            needed_indices = files_to_want_by_torrent[tid]

                            missing_wanted = [
                                idx for idx in needed_indices
                                if idx < len(fstats) and not fstats[idx].get("wanted", False)
                            ]

                            if missing_wanted:
                                if not dry_run:
                                    t_client.set_file_priorities(tid, wanted_indices=missing_wanted)
                                actions.append(f"Transmission: {len(missing_wanted)} files re-enabled (wanted) in {t_item.get('name', f'Torrent #{tid}')}")

                            # If the torrent is paused (0) and has requested files
                            if t_status == 0:
                                if not dry_run:
                                    t_client.start_torrent(tid)
                                actions.append(f"Transmission: Torrent #{tid} started ({t_item.get('name', tid)})")
                except Exception as e:
                    actions.append(f"Transmission RPC warning: {e}")


        # 3. Reconcile game state from files (multi-disc)
        with get_db_connection(db_path) as conn:
            backfill_game_files(conn)
            cur = conn.cursor()
            cur.execute("""
                SELECT g.id, g.state, gf.file_path, gf.file_name, gf.file_length,
                       tc.bytes_completed
                FROM games g
                JOIN game_files gf ON gf.game_id = g.id
                LEFT JOIN torrent_cache tc
                  ON gf.torrent_id = tc.torrent_id AND gf.file_index = tc.file_index
                WHERE g.state IN ('downloading', 'downloaded', 'linked')
                ORDER BY g.id, gf.disc_number;
            """)
            per_game = defaultdict(list)
            game_states = {}
            for row in cur.fetchall():
                per_game[row["id"]].append(row)
                game_states[row["id"]] = row["state"]

            for g_id, files in per_game.items():
                complete = 0
                started = 0
                for f in files:
                    file_rel = f["file_path"] or f["file_name"] or ""
                    path = cfg.paths.download_dir / file_rel
                    exists = bool(file_rel) and path.is_file()
                    length = int(f["file_length"] or 0)
                    completed = int(f["bytes_completed"] or 0)
                    if exists and (length == 0 or path.stat().st_size >= length):
                        complete += 1
                    if exists or (file_rel and (cfg.paths.download_dir / f"{file_rel}.part").is_file()) or completed > 0:
                        started += 1

                if game_states[g_id] == "downloading" and complete == len(files) and files:
                    if not dry_run:
                        conn.execute("UPDATE games SET state = 'downloaded' WHERE id = ?;", (g_id,))
                    newly_downloaded += 1
                elif game_states[g_id] == "downloading" and started:
                    # Already in progress: nothing to change.
                    pass

            conn.commit()

        # 4. Links and multi-disc .m3u generation
        multidisc_tracker = defaultdict(lambda: defaultdict(list)) # system -> base_title -> list of filenames

        with get_db_connection(db_path) as conn:
            backfill_game_files(conn)
            cur = conn.cursor()
            cur.execute("""
                SELECT id, title, system, state
                FROM games
                WHERE state IN ('downloaded', 'linked')
            """)
            games_to_link = cur.fetchall()

            for g in games_to_link:
                g_id = g["id"]
                sys_key = normalize_system(g["system"])
                sys_prof = profile.get_system(sys_key)
                sys_sub = sys_prof.dir if sys_prof else sys_key
                dest_dir = cfg.paths.library_dir / sys_sub

                files = cur.execute(
                    "SELECT disc_number, file_path, file_name FROM game_files "
                    "WHERE game_id = ? ORDER BY disc_number;",
                    (g_id,),
                ).fetchall()
                if not files:
                    continue

                all_linked = True
                any_missing = False
                any_invalid = False
                for f in files:
                    file_rel = f["file_path"] or f["file_name"] or ""
                    fname = Path(file_rel).name
                    src_path = cfg.paths.download_dir / file_rel
                    dest_path = dest_dir / fname

                    if sys_prof and sys_prof.multidisc_m3u and extract_disc_number(fname):
                        base_name = get_disc_family(Path(fname).stem)
                        multidisc_tracker[sys_key][base_name].append(fname)

                    if dest_path.exists():
                        linked_count += 1
                        continue

                    if not src_path.exists():
                        all_linked = False
                        any_missing = True
                        continue

                    # Invalid destination directory (file, broken link...)
                    if dest_dir.is_symlink() and not dest_dir.exists():
                        all_linked = False
                        any_invalid = True
                        errors.append(f"Broken link to directory {dest_dir}: recreate or delete it.")
                        continue
                    if dest_dir.exists() and not dest_dir.is_dir():
                        all_linked = False
                        any_invalid = True
                        errors.append(f"The path {dest_dir} exists but is not a directory.")
                        continue

                    if not dry_run:
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        if dest_path.is_symlink() and not dest_path.exists():
                            dest_path.unlink()
                        try:
                            os.link(src_path, dest_path)
                        except OSError:
                            dest_path.symlink_to(src_path)

                    linked_count += 1
                    actions.append(f"Link: {g['title']} [{f['disc_number']}] -> {dest_path}")

                if all_linked:
                    if g["state"] != "linked" and not dry_run:
                        conn.execute("UPDATE games SET state = 'linked' WHERE id = ?;", (g_id,))
                elif any_invalid:
                    if g["state"] == "linked" and not dry_run:
                        conn.execute("UPDATE games SET state = 'downloaded' WHERE id = ?;", (g_id,))
                else:
                    if g["state"] in ("downloaded", "linked") and not dry_run:
                        conn.execute("UPDATE games SET state = 'downloading' WHERE id = ?;", (g_id,))
                    if any_missing:
                        errors.append(
                            f"Missing file(s) (Download + Library) for {g['title']}: "
                            "re-download scheduled."
                        )

            # Create .m3u files for multi-disc games
            for sys_key, bases in multidisc_tracker.items():
                sys_prof = profile.get_system(sys_key)
                sys_sub = sys_prof.dir if sys_prof else sys_key
                dest_dir = cfg.paths.library_dir / sys_sub

                if not dest_dir.is_dir():
                    continue
                for bname, disc_files in bases.items():
                    if len(disc_files) > 1:
                        m3u_path = dest_dir / f"{bname}.m3u"
                        m3u_content = generate_m3u_content(disc_files)
                        if not dry_run:
                            m3u_path.write_text(m3u_content, encoding="utf-8")
                        actions.append(f".m3u generated: {m3u_path.name} ({len(disc_files)} discs)")

            set_meta(conn, "last_sync", "completed")
            conn.commit()

    return {
        "dry_run": dry_run,
        "actions": actions,
        "pending_executed": pending_executed,
        "newly_downloading": newly_downloading,
        "newly_downloaded": newly_downloaded,
        "links_created_or_verified": linked_count,
        "errors": list(dict.fromkeys(errors))
    }
