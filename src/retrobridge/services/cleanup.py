"""Unified cleanup: "état des lieux" of orphan games and reclaimable files.

Two different notions are reported together:

* **orphan games** — a database row that no longer points to a file of an
  indexed pack (the anti-phantom invariant is broken);
* **reclaimable files** — bytes on disk that no game references:
  - complete files unselected in Transmission (old replacements),
  - ``.part`` files of unselected downloads,
  - files outside any indexed pack.

Referenced files (games, multi-disc files, pending upgrades, exports) and files
still wanted in Transmission are never candidates.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from retrobridge.config import Config, load_config
from retrobridge.services.audit import audit_collection
from retrobridge.services.scan import scan_torrents
from retrobridge.storage.db import get_db_connection, init_db


def _real_bytes(path: Path) -> int:
    """Actual allocated size (sparse/preallocated ``.part`` files lie in ``st_size``)."""
    try:
        st = path.stat()
    except OSError:
        return 0
    return getattr(st, "st_blocks", 0) * 512 or st.st_size


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?;", (name,)
    ).fetchone() is not None


def _totals(items: List[Dict[str, Any]]) -> int:
    return sum(int(item.get("bytes") or 0) for item in items)


def build_cleanup_report(
    config: Optional[Config] = None,
    db_path: Optional[str] = None,
    scan_disk: bool = True,
    refresh: bool = True,
) -> Dict[str, Any]:
    """Builds the full report without modifying anything.

    ``refresh`` re-reads Transmission file stats first so the report never
    relies on a stale ``is_wanted`` flag (which could delete active downloads).
    """
    cfg = config or load_config()

    refresh_info: Dict[str, Any] = {"ran": False, "error": None}
    if refresh:
        try:
            scan_res = scan_torrents(config=cfg, db_path=db_path)
            refresh_info = {
                "ran": True,
                "torrents": scan_res["torrents_count"],
                "changed": scan_res["changed_torrents"],
                "error": None,
            }
        except Exception as exc:
            refresh_info["error"] = str(exc)

    init_db(db_path)
    audit = audit_collection(config=cfg, db_path=db_path)

    download_dir = cfg.paths.download_dir
    with get_db_connection(db_path) as conn:
        has_upgrades = _has_table(conn, "upgrades")

        referenced_paths: set[str] = set()
        referenced_names: set[str] = set()

        def add_ref(value: Any) -> None:
            if not value:
                return
            referenced_paths.add(str(value))
            referenced_names.add(Path(str(value)).name)

        for row in conn.execute("SELECT matched_file_path, matched_file_name FROM games;"):
            add_ref(row["matched_file_path"])
            add_ref(row["matched_file_name"])
        for row in conn.execute("SELECT file_path, file_name FROM game_files;"):
            add_ref(row["file_path"])
            add_ref(row["file_name"])
        if has_upgrades:
            for row in conn.execute("SELECT old_file_path, old_file_name FROM upgrades;"):
                add_ref(row["old_file_path"])
                add_ref(row["old_file_name"])
        for row in conn.execute("SELECT source_file FROM exports;"):
            add_ref(row["source_file"])
        for row in conn.execute("SELECT source_file FROM export_files;"):
            add_ref(row["source_file"])

        cache_rows = conn.execute("""
            SELECT torrent_id, file_name, file_length, bytes_completed, is_wanted
            FROM torrent_cache;
        """).fetchall()

    # A path still wanted by another torrent must never be deleted.
    wanted_by_path: Dict[str, set] = defaultdict(set)
    for row in cache_rows:
        if row["is_wanted"]:
            wanted_by_path[row["file_name"]].add(row["torrent_id"])

    def protected(rel: str) -> bool:
        return rel in referenced_paths or Path(rel).name in referenced_names

    complete: List[Dict[str, Any]] = []
    partial: List[Dict[str, Any]] = []

    for row in cache_rows:
        if row["is_wanted"]:
            continue
        rel = row["file_name"]
        if protected(rel):
            continue
        if wanted_by_path.get(rel, set()) - {row["torrent_id"]}:
            continue

        final = download_dir / rel
        part = download_dir / f"{rel}.part"
        completed = int(row["bytes_completed"] or 0)
        length = int(row["file_length"] or 0)

        # A final file is a completed download (Transmission writes to
        # `*.part` until complete). It is reclaimable even when the stale
        # `bytes_completed` column reports 0.
        if final.is_file() and (length == 0 or final.stat().st_size >= length):
            complete.append({
                "kind": "complete",
                "torrent_id": row["torrent_id"],
                "file_name": rel,
                "path": str(final),
                "bytes": _real_bytes(final),
                "size_bytes": length or final.stat().st_size,
                "completed_bytes": completed,
            })
        elif part.is_file():
            partial.append({
                "kind": "partial",
                "torrent_id": row["torrent_id"],
                "file_name": rel,
                "path": str(part),
                "bytes": _real_bytes(part),
                "size_bytes": length,
                "completed_bytes": completed,
            })

    # Files present on disk with no pack entry and no game/export reference.
    orphans: List[Dict[str, Any]] = []
    if scan_disk and download_dir.is_dir():
        pack_basenames = {Path(row["file_name"]).name for row in cache_rows}
        for path in download_dir.rglob("*"):
            if not path.is_file():
                continue
            name = path.name
            if name in referenced_names:
                continue
            if name in pack_basenames:
                # Handled by the complete/partial passes, or intentionally kept.
                continue
            if name.endswith(".part") and (
                name[:-5] in pack_basenames or name[:-5] in referenced_names
            ):
                continue
            orphans.append({
                "kind": "orphan",
                "file_name": str(path.relative_to(download_dir)),
                "path": str(path),
                "bytes": _real_bytes(path),
            })

    # Orphan games are deduplicated: a row may match several audit queries.
    purge: Dict[int, Dict[str, Any]] = {}
    for game in audit["without_pack_file"]:
        purge[game["id"]] = {**game, "reason": "no file in indexed packs"}
    for game in audit["without_matched_name"]:
        purge[game["id"]] = {**game, "reason": "no matched file"}

    complete_bytes = _totals(complete)
    partial_bytes = _totals(partial)
    orphan_bytes = _totals(orphans)
    games_to_purge = list(purge.values())
    stale_resume = audit.get("stale_resume", [])
    orphan_library = audit.get("orphan_library", [])
    library_removable = [
        item
        for item in orphan_library
        if item.get("download_exists") or item.get("is_symlink")
    ]
    library_only = [
        item
        for item in orphan_library
        if not (item.get("download_exists") or item.get("is_symlink"))
    ]
    orphan_library_bytes = sum(int(item.get("bytes") or 0) for item in library_removable)
    library_only_bytes = sum(int(item.get("bytes") or 0) for item in library_only)

    return {
        "ok": not (
            games_to_purge
            or complete
            or partial
            or orphans
            or stale_resume
            or orphan_library
        ),
        "audit_ok": audit["ok"],
        "refresh": refresh_info,
        "games_to_purge": games_to_purge,
        "missing_on_disk": audit["missing_on_disk"],
        "stale_resume": stale_resume,
        "duplicate_paths": audit["duplicate_paths"],
        "orphan_library": orphan_library,
        "library_removable": library_removable,
        "library_only": library_only,
        "complete_unwanted": complete,
        "partial_unwanted": partial,
        "orphan_files": orphans,
        "totals": {
            "games_to_purge": len(games_to_purge),
            "complete_files": len(complete),
            "complete_bytes": complete_bytes,
            "partial_files": len(partial),
            "partial_bytes": partial_bytes,
            "orphan_files": len(orphans),
            "orphan_bytes": orphan_bytes,
            "stale_resume_files": len(stale_resume),
            "orphan_library_files": len(library_removable),
            "orphan_library_bytes": orphan_library_bytes,
            "library_only_files": len(library_only),
            "library_only_bytes": library_only_bytes,
            "reclaimable_bytes": complete_bytes + partial_bytes + orphan_bytes,
        },
    }


def apply_cleanup(
    report: Dict[str, Any],
    db_path: Optional[str] = None,
    include_complete: bool = True,
    include_partial: bool = True,
    include_orphans: bool = True,
    include_library: bool = True,
    purge_games: bool = True,
) -> Dict[str, Any]:
    """Deletes the selected files then purges orphan games. Returns what was done."""
    items: List[Dict[str, Any]] = []
    if include_complete:
        items.extend(report.get("complete_unwanted", []))
    if include_partial:
        items.extend(report.get("partial_unwanted", []))
    if include_orphans:
        items.extend(report.get("orphan_files", []))

    deleted_files = 0
    freed_bytes = 0
    errors: List[str] = []

    for item in items:
        path = Path(item["path"])
        try:
            if path.is_file() or path.is_symlink():
                freed_bytes += _real_bytes(path)
                path.unlink()
                deleted_files += 1
            # A completed file can still leave a stale .part behind.
            if item.get("kind") == "complete":
                leftover = Path(f"{path}.part")
                if leftover.is_file():
                    freed_bytes += _real_bytes(leftover)
                    leftover.unlink()
        except OSError as exc:
            errors.append(f"{path}: {exc}")

    if include_library:
        for item in report.get("library_removable", report.get("orphan_library", [])):
            path = Path(item["file"])
            # Never delete the last copy: only remove a Library link when the
            # Download file still exists, or when it is a symlink.
            if not item.get("download_exists") and not item.get("is_symlink"):
                errors.append(f"Library-only file kept (no Download copy): {path}")
                continue
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
                    deleted_files += 1
            except OSError as exc:
                errors.append(f"{path}: {exc}")

    purged_games = 0
    if purge_games and report.get("games_to_purge"):
        ids = [game["id"] for game in report["games_to_purge"]]
        with get_db_connection(db_path) as conn:
            conn.executemany("DELETE FROM games WHERE id = ?;", [(gid,) for gid in ids])
            conn.commit()
        purged_games = len(ids)

    return {
        "deleted_files": deleted_files,
        "freed_bytes": freed_bytes,
        "purged_games": purged_games,
        "errors": errors,
    }
