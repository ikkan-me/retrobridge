from pathlib import Path
from typing import Optional, List, Dict, Any

from retrobridge.storage.db import get_db_connection
from retrobridge.domain.systems import normalize_system
from retrobridge.domain.text import strip_emoji
from retrobridge.services.downloads import classify_download


def list_games(
    system: Optional[str] = None,
    state: Optional[str] = None,
    db_path: Optional[str] = None,
    download_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    query = """
        SELECT g.id, g.title, g.system, g.state, g.is_pinned,
               g.matched_file_name, g.matched_file_path,
               g.matched_torrent_id, g.matched_file_index,
               g.match_score, g.match_lang_tag,
               tc.file_name AS cache_file_name,
               tc.file_length, tc.bytes_completed, tc.is_wanted
        FROM games g
        LEFT JOIN torrent_cache tc
          ON g.matched_torrent_id = tc.torrent_id
         AND g.matched_file_index = tc.file_index
        WHERE 1=1
    """
    params = []
    if system:
        query += " AND UPPER(g.system) = ?"
        params.append(normalize_system(system))
    if state:
        query += " AND g.state = ?"
        params.append(state.lower())
    query += " ORDER BY g.system, g.title"

    with get_db_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()

    games: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        file_rel = item.get("matched_file_path") or item.get("matched_file_name") or ""
        item["file_name"] = Path(file_rel).name if file_rel else None
        item["download_status"] = item["state"]
        item["issue"] = None

        if item["state"] == "downloading" and download_dir is not None:
            download = classify_download(
                file_rel=file_rel,
                download_dir=download_dir,
                in_cache=item.get("cache_file_name") is not None,
                wanted=bool(item.get("is_wanted") or 0),
                file_length=int(item.get("file_length") or 0),
                bytes_completed=int(item.get("bytes_completed") or 0),
            )
            item.update(download)

        item.pop("cache_file_name", None)
        item["match_lang_tag"] = strip_emoji(item.get("match_lang_tag"))
        games.append(item)
    return games
