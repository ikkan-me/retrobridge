"""Unified `find` search: "already present" detection and cache freshness."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from retrobridge.config import Config, load_config
from retrobridge.domain.multidisc import (
    extract_disc_number,
    get_base_title,
    get_disc_family,
    get_disc_search_title,
)
from retrobridge.domain.regions import classify_region
from retrobridge.domain.scoring import normalize_text
from retrobridge.domain.systems import system_codes
from retrobridge.storage.db import get_db_connection, init_db, is_ttl_expired
from retrobridge.services.scan import scan_torrents
from retrobridge.services.search import search_candidates


def find_existing_game(
    title: str,
    system: str,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Finds a game by normalized title + system (accent-insensitive).

    A disc suffix in the query (`... Disk 2`) is ignored so that a
    multi-disc game is still recognized as already present.
    """
    codes = system_codes(system)
    wanted_key = normalize_text(get_base_title(title))
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, title, system, state, matched_file_name, matched_file_path, "
            "matched_torrent_id, matched_file_index "
            f"FROM games WHERE UPPER(system) IN ({','.join('?' * len(codes))});",
            codes,
        ).fetchall()
    for row in rows:
        if normalize_text(row["title"]) == wanted_key:
            return dict(row)
    return None


def refresh_cache_if_needed(
    config: Optional[Config] = None,
    db_path: Optional[str] = None,
    refresh: bool = True,
) -> Dict[str, Any]:
    """Runs `scan_torrents` only if the cache TTL has expired."""
    if not refresh:
        return {"ran": False, "reason": "disabled"}
    cfg = config or load_config()
    init_db(db_path)
    with get_db_connection(db_path) as conn:
        stale = is_ttl_expired(conn, "last_scan", cfg.ttl.scan_ttl)
    if not stale:
        return {"ran": False, "reason": "fresh"}

    result = scan_torrents(config=cfg, db_path=db_path)
    return {"ran": True, "reason": "stale", **result}


def same_disc_family(first: Dict[str, Any], second: Dict[str, Any]) -> bool:
    """True when two candidates are discs of the same release."""
    first_name = first.get("file_name") or ""
    second_name = second.get("file_name") or ""
    if extract_disc_number(Path(first_name).name) is None:
        return False
    if extract_disc_number(Path(second_name).name) is None:
        return False
    return normalize_text(get_disc_family(Path(first_name).stem)) == normalize_text(
        get_disc_family(Path(second_name).stem)
    )


def expand_disc_candidates(
    candidate: Dict[str, Any],
    system: str,
    config: Optional[Config] = None,
    db_path: Optional[str] = None,
) -> list[Dict[str, Any]]:
    """From a file, finds the other discs of the same game."""
    file_name = candidate.get("file_name") or ""
    disc = extract_disc_number(Path(file_name).name)
    if disc is None:
        return [candidate]

    stem = Path(file_name).stem
    family_key = normalize_text(get_disc_family(stem))
    base_key = normalize_text(get_base_title(stem))
    region = classify_region(file_name)
    cfg = config or load_config()

    # Search on the main title only: disc subtitles such as
    # `(Subsistence)` / `(Persistence)` must not restrict the query.
    query = get_disc_search_title(stem)
    candidates = search_candidates(query=query, system=system, config=cfg, db_path=db_path)
    by_disc: Dict[int, Dict[str, Any]] = {}
    for item in candidates:
        item_stem = Path(item["file_name"]).stem
        other_disc = extract_disc_number(Path(item["file_name"]).name)
        if other_disc is None:
            continue
        item_family_key = normalize_text(get_disc_family(item_stem))
        item_base_key = normalize_text(get_base_title(item_stem))
        if item_family_key != family_key and item_base_key != base_key:
            continue
        if region != "UNKNOWN" and classify_region(item["file_name"]) != region:
            continue
        current = by_disc.get(other_disc)
        if current is None or (item.get("score", 0), item.get("file_length", 0)) > (
            current.get("score", 0), current.get("file_length", 0)
        ):
            by_disc[other_disc] = item

    # The user-selected candidate stays preferred for its disc.
    by_disc[disc] = candidate
    return [by_disc[number] for number in sorted(by_disc)]

