import shutil
from pathlib import Path
from typing import Dict, Any, Optional, List
from retrobridge.config import Config, load_config
from retrobridge.storage.db import get_db_connection
from retrobridge.adapters.transmission import TransmissionClient
from retrobridge.domain.systems import normalize_system
from retrobridge.services.freshness import get_freshness
from retrobridge.services.downloads import classify_download
from retrobridge.services.setup import get_timer_status

def get_system_status(
    config: Optional[Config] = None,
    db_path: Optional[str] = None,
    live: bool = True,
) -> Dict[str, Any]:
    cfg = config or load_config()

    state_counts: Dict[str, int] = {}
    total_games = 0
    systems_summary: List[Dict[str, Any]] = []
    active_downloads: List[Dict[str, Any]] = []

    with get_db_connection(db_path) as conn:
        cur = conn.cursor()

        # 1. Global states
        cur.execute("SELECT state, COUNT(*) as cnt FROM games GROUP BY state;")
        for row in cur.fetchall():
            state_counts[row["state"]] = row["cnt"]
            total_games += row["cnt"]

        # Volume by state (game_files, multi-disc included)
        cur.execute("""
            SELECT g.state, COALESCE(SUM(gf.file_length), 0) AS bytes
            FROM games g
            LEFT JOIN (
                SELECT game_id, SUM(file_length) AS file_length
                FROM game_files GROUP BY game_id
            ) gf ON gf.game_id = g.id
            GROUP BY g.state;
        """)
        state_bytes = {row["state"]: int(row["bytes"] or 0) for row in cur.fetchall()}

        # Exported games per system: a game counts once if it is on at least
        # one target (distinct game_id, union of legacy and current tables).
        cur.execute("""
            SELECT system, COUNT(DISTINCT game_id) AS cnt FROM (
                SELECT g.system AS system, ef.game_id AS game_id
                FROM export_files ef JOIN games g ON g.id = ef.game_id
                UNION
                SELECT g.system AS system, e.game_id AS game_id
                FROM exports e JOIN games g ON g.id = e.game_id
            ) GROUP BY system;
        """)
        exported_by_system = {
            normalize_system(row["system"]): row["cnt"] for row in cur.fetchall()
        }

        # 2. Detailed breakdown by system
        cur.execute("""
            WITH effective_files AS (
                SELECT game_id, SUM(file_length) AS file_length
                FROM game_files GROUP BY game_id
                UNION ALL
                SELECT g.id, COALESCE(SUM(tc.file_length), 0)
                FROM games g
                LEFT JOIN torrent_cache tc
                  ON g.matched_torrent_id = tc.torrent_id
                 AND g.matched_file_index = tc.file_index
                WHERE NOT EXISTS (SELECT 1 FROM game_files gf WHERE gf.game_id = g.id)
                GROUP BY g.id
            )
            SELECT g.system,
                   COUNT(*) as total,
                   SUM(CASE WHEN g.state = 'linked' THEN 1 ELSE 0 END) as linked,
                   SUM(CASE WHEN g.state = 'downloading' THEN 1 ELSE 0 END) as downloading,
                   SUM(CASE WHEN g.state = 'downloaded' THEN 1 ELSE 0 END) as downloaded,
                   COALESCE(SUM(ef.file_length), 0) as size_bytes
            FROM games g
            LEFT JOIN effective_files ef ON ef.game_id = g.id
            GROUP BY g.system
            ORDER BY total DESC, g.system ASC;
        """)
        for row in cur.fetchall():
            tot = row["total"]
            lnk = row["linked"]
            acquired = row["downloaded"] + lnk
            pct = round((lnk / tot * 100), 1) if tot > 0 else 0.0
            download_pct = round((acquired / tot * 100), 1) if tot > 0 else 0.0
            systems_summary.append({
                "system": normalize_system(row["system"]),
                "total": tot,
                "linked": lnk,
                "downloading": row["downloading"],
                "downloaded": row["downloaded"],
                "downloaded_pending": row["downloaded"],
                "acquired": acquired,
                "exported": exported_by_system.get(normalize_system(row["system"]), 0),
                "size_bytes": row["size_bytes"],
                "progress_pct": pct,
                "download_pct": download_pct,
            })

        # 3. In-progress games: targeted reconciliation (multi-disc)
        cur.execute("""
            WITH effective_files AS (
                SELECT game_id, disc_number, torrent_id, file_index, file_path, file_name, file_length
                FROM game_files
                UNION ALL
                SELECT g.id, 1, g.matched_torrent_id, g.matched_file_index,
                       g.matched_file_path, g.matched_file_name, COALESCE(tc.file_length, 0)
                FROM games g
                LEFT JOIN torrent_cache tc
                  ON g.matched_torrent_id = tc.torrent_id
                 AND g.matched_file_index = tc.file_index
                WHERE NOT EXISTS (SELECT 1 FROM game_files gf WHERE gf.game_id = g.id)
            )
            SELECT g.id, g.title, g.system, g.match_score, g.match_lang_tag,
                   gf.disc_number, gf.file_path, gf.file_name,
                   gf.torrent_id, gf.file_index, gf.file_length,
                   tc.file_name AS cache_file_name,
                   tc.bytes_completed, tc.is_wanted
            FROM games g
            JOIN effective_files gf ON gf.game_id = g.id
            LEFT JOIN torrent_cache tc
              ON gf.torrent_id = tc.torrent_id
             AND gf.file_index = tc.file_index
            WHERE g.state = 'downloading'
            ORDER BY g.system ASC, g.title ASC, gf.disc_number ASC;
        """)
        active_all: List[Dict[str, Any]] = []
        download_status_counts: Dict[str, int] = {}
        grouped: Dict[int, Dict[str, Any]] = {}
        for row in cur.fetchall():
            gid = row["id"]
            entry = grouped.setdefault(gid, {
                "id": gid,
                "title": row["title"],
                "system": row["system"],
                "match_score": row["match_score"],
                "match_lang_tag": row["match_lang_tag"],
                "files": [],
            })
            file_rel = row["file_path"] or row["file_name"] or ""
            wanted = bool(row["is_wanted"] or 0)
            download = classify_download(
                file_rel=file_rel,
                download_dir=cfg.paths.download_dir,
                in_cache=row["cache_file_name"] is not None,
                wanted=wanted,
                file_length=int(row["file_length"] or 0),
                bytes_completed=int(row["bytes_completed"] or 0),
            )
            download.update({
                "disc_number": row["disc_number"],
                "file_name": Path(file_rel).name if file_rel else None,
                "file_path": file_rel or None,
                "wanted": wanted,
                "in_cache": row["cache_file_name"] is not None,
            })
            entry["files"].append(download)

        for entry in grouped.values():
            files = entry.pop("files")
            statuses = [f["download_status"] for f in files]
            if "broken" in statuses:
                status = "broken"
            elif "unknown" in statuses:
                status = "unknown"
            elif "downloading" in statuses:
                status = "downloading"
            elif "orphan_partial" in statuses:
                status = "orphan_partial"
            elif "present_unwanted" in statuses:
                status = "present_unwanted"
            elif statuses and all(s == "complete" for s in statuses):
                status = "complete"
            else:
                status = "queued"

            total_length = sum(f["file_length"] for f in files)
            total_completed = sum(f["bytes_completed"] for f in files)
            first = files[0]
            entry.update({
                "file_name": first.get("file_name"),
                "matched_file_name": first.get("file_name"),
                "matched_file_path": first.get("file_path"),
                "target_exists": all(f["target_exists"] for f in files),
                "partial_exists": any(f["partial_exists"] for f in files),
                "wanted": any(f["wanted"] for f in files),
                "in_cache": any(f["in_cache"] for f in files),
                "bytes_completed": total_completed,
                "file_length": total_length,
                "progress_pct": round(min(100.0, total_completed * 100.0 / total_length), 1) if total_length else 0.0,
                "download_status": status,
                "issue": next((f["issue"] for f in files if f["issue"]), None),
                "discs_total": len(files),
                "discs_complete": sum(1 for f in files if f["download_status"] == "complete"),
            })
            active_all.append(entry)
            download_status_counts[status] = download_status_counts.get(status, 0) + 1

        active_downloads = active_all[:15]
        target_issues = sum(
            1 for item in active_all if item["download_status"] in ("broken", "unknown")
        )

        # Exports by target
        exports_by_target: Dict[str, int] = {}
        cur.execute("""
            SELECT target_id, COUNT(DISTINCT game_id) AS cnt FROM (
                SELECT target_id, game_id FROM export_files
                UNION
                SELECT target_id, game_id FROM exports
            ) GROUP BY target_id;
        """)
        for row in cur.fetchall():
            exports_by_target[row["target_id"]] = row["cnt"]

        # Pending actions
        cur.execute("SELECT action_type, COUNT(*) as cnt FROM pending_actions GROUP BY action_type;")
        pending_actions = {row["action_type"]: row["cnt"] for row in cur.fetchall()}

        # Torrent cache count + volume actually requested by Transmission
        cur.execute("""
            SELECT COUNT(*) as files,
                   COALESCE(SUM(CASE WHEN is_wanted = 1 THEN file_length ELSE 0 END), 0) as wanted_bytes
            FROM torrent_cache;
        """)
        cache_row = cur.fetchone()
        cache_count = cache_row["files"]
        wanted_files = 0
        wanted_bytes = 0
        if cache_count:
            cur.execute("SELECT COUNT(*) as cnt FROM torrent_cache WHERE is_wanted = 1;")
            wanted_files = cur.fetchone()["cnt"]
        wanted_bytes = cache_row["wanted_bytes"]

        # Volume of game files actually selected as games
        cur.execute("""
            SELECT COALESCE(SUM(tc.file_length), 0) as selected_bytes
            FROM games g
            JOIN torrent_cache tc
              ON g.matched_torrent_id = tc.torrent_id
             AND g.matched_file_index = tc.file_index
            WHERE g.state != 'removing';
        """)
        selected_bytes = cur.fetchone()["selected_bytes"]

        # Cache freshness (UTC stored in DB, converted to local time for display).
        freshness = get_freshness(
            conn,
            scan_ttl=cfg.ttl.scan_ttl,
            sync_ttl=cfg.ttl.status_ttl,
        )

    timer = get_timer_status()

    # 4. Transmission RPC & ratio (disable for a strictly local status)
    transmission_reachable = False
    transmission_stats: Dict[str, Any] = {}
    if live:
        client = TransmissionClient(
            rpc_url=cfg.transmission.rpc_url,
            username=cfg.transmission.user,
            password=cfg.transmission.password,
            # Status must stay fast even when the RPC is saturated.
            timeout=min(cfg.transmission.timeout, 3.0),
        )
        try:
            session_stats = client.get_session_stats()
            transmission_reachable = True
            cum_stats = session_stats.get("cumulative-stats", {})
            transmission_stats = {
                "download_rate_bytes": session_stats.get("downloadSpeed", 0),
                "upload_rate_bytes": session_stats.get("uploadSpeed", 0),
                "ratio": round(cum_stats.get("ratio", 0.0), 2),
                "torrents_active": session_stats.get("activeTorrentCount", 0),
                "torrents_total": session_stats.get("torrentCount", 0),
            }
        except Exception:
            transmission_reachable = False

    # 5. Storage and filesystem
    def get_fs_info(p: Path) -> Dict[str, Any]:
        exists = p.exists()
        free_gb = 0.0
        total_gb = 0.0
        if exists:
            try:
                usage = shutil.disk_usage(p)
                free_gb = round(usage.free / (1024**3), 1)
                total_gb = round(usage.total / (1024**3), 1)
            except OSError:
                pass
        return {"path": str(p), "exists": exists, "free_gb": free_gb, "total_gb": total_gb}

    dl_info = get_fs_info(cfg.paths.download_dir)
    lib_info = get_fs_info(cfg.paths.library_dir)
    same_fs = cfg.validate_same_filesystem()

    # 6. Target status
    targets_status = {}
    for t_name, target in cfg.targets.items():
        is_mounted = target.mount_path.is_dir()
        t_fs = get_fs_info(target.mount_path) if is_mounted else {"free_gb": 0.0, "total_gb": 0.0}
        targets_status[t_name] = {
            "profile": target.profile,
            "mount_path": str(target.mount_path),
            "mounted": is_mounted,
            "free_gb": t_fs["free_gb"],
            "exported_games": exports_by_target.get(t_name, 0)
        }

    return {
        "games": {
            "total": total_games,
            "downloading": state_counts.get("downloading", 0),
            "downloaded": state_counts.get("downloaded", 0),
            "downloaded_pending": state_counts.get("downloaded", 0),
            "acquired": state_counts.get("downloaded", 0) + state_counts.get("linked", 0),
            "linked": state_counts.get("linked", 0),
            "removing": state_counts.get("removing", 0),
            "downloading_bytes": state_bytes.get("downloading", 0),
            "acquired_bytes": state_bytes.get("downloaded", 0) + state_bytes.get("linked", 0),
            "total_bytes": sum(state_bytes.values()),
        },
        "systems": systems_summary,
        "active_downloads": active_downloads,
        "active_downloads_total": len(active_all),
        "download_status_counts": download_status_counts,
        "target_issues": target_issues,
        "freshness": freshness,
        "timer": timer,
        "torrent_cache_size": cache_count,
        "selection": {
            "wanted_files": wanted_files,
            "wanted_bytes": wanted_bytes,
            "selected_bytes": selected_bytes,
        },
        "transmission": {
            "live": live,
            "reachable": transmission_reachable,
            **transmission_stats
        },
        "storage": {
            "download_dir": dl_info,
            "library_dir": lib_info,
            "same_partition_for_hardlinks": same_fs
        },
        "targets": targets_status,
        "pending_actions": pending_actions,
    }
