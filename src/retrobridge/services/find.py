"""Recherche unifiée `find` : détection « déjà présent » et fraîcheur du cache."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from retrobridge.config import Config, load_config
from retrobridge.domain.multidisc import extract_disc_number, get_base_title
from retrobridge.domain.regions import classify_region
from retrobridge.domain.scoring import normalize_text
from retrobridge.domain.systems import normalize_system
from retrobridge.storage.db import get_db_connection, init_db, is_ttl_expired
from retrobridge.services.scan import scan_torrents
from retrobridge.services.search import search_candidates


def find_existing_game(
    title: str,
    system: str,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Retrouve un jeu par titre normalisé + système (insensible aux accents)."""
    canonical = normalize_system(system)
    wanted_key = normalize_text(title)
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, title, system, state, matched_file_name, matched_file_path, "
            "matched_torrent_id, matched_file_index "
            "FROM games WHERE UPPER(system) = ?;",
            (canonical,),
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
    """Lance `scan_torrents` uniquement si le TTL du cache est dépassé."""
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


def expand_disc_candidates(
    candidate: Dict[str, Any],
    system: str,
    config: Optional[Config] = None,
    db_path: Optional[str] = None,
) -> list[Dict[str, Any]]:
    """À partir d'un fichier, retrouve les autres disques du même jeu."""
    file_name = candidate.get("file_name") or ""
    disc = extract_disc_number(Path(file_name).name)
    if disc is None:
        return [candidate]

    base = get_base_title(Path(file_name).stem)
    base_key = normalize_text(base)
    region = classify_region(file_name)
    cfg = config or load_config()

    candidates = search_candidates(query=base, system=system, config=cfg, db_path=db_path)
    by_disc: Dict[int, Dict[str, Any]] = {}
    for item in candidates:
        other_disc = extract_disc_number(Path(item["file_name"]).name)
        if other_disc is None:
            continue
        if normalize_text(get_base_title(Path(item["file_name"]).stem)) != base_key:
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

