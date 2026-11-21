"""Classification d'un téléchargement, partagée par `status` et `list`."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


def classify_download(
    file_rel: str,
    download_dir: Path,
    in_cache: bool,
    wanted: bool,
    file_length: int,
    bytes_completed: int,
) -> Dict[str, Any]:
    """Détermine l'état réel d'un fichier de jeu par rapport à Transmission.

    - ``queued`` : demandé, rien reçu ;
    - ``downloading`` : .part ou octets reçus ;
    - ``complete`` : fichier final présent et complet ;
    - ``broken`` : absent ET non sélectionné ;
    - ``orphan_partial`` : .part présent mais non sélectionné ;
    - ``present_unwanted`` : fichier final présent mais non sélectionné ;
    - ``unknown`` : absent du cache torrent.
    """
    final_path = download_dir / file_rel if file_rel else None
    part_path = download_dir / f"{file_rel}.part" if file_rel else None
    final_exists = bool(final_path and final_path.is_file())
    part_exists = bool(part_path and part_path.is_file())
    on_disk_size = final_path.stat().st_size if final_exists else 0
    progressed = max(on_disk_size, int(bytes_completed or 0))
    length = int(file_length or 0)

    if not in_cache:
        download_status = "unknown"
        issue = "absente du cache torrent"
    elif final_exists and (length == 0 or on_disk_size >= length):
        download_status = "complete"
        issue = None
    elif not wanted:
        if part_exists:
            download_status = "orphan_partial"
            issue = "partiel orphelin (non sélectionné)"
        elif final_exists:
            download_status = "present_unwanted"
            issue = "fichier présent mais non sélectionné"
        else:
            download_status = "broken"
            issue = "cible absente et non sélectionnée"
    elif final_exists or part_exists or progressed > 0:
        download_status = "downloading"
        issue = None
    else:
        download_status = "queued"
        issue = None

    progress_pct = round(min(100.0, progressed * 100.0 / length), 1) if length else 0.0
    return {
        "target_exists": final_exists,
        "partial_exists": part_exists,
        "progress_pct": progress_pct,
        "download_status": download_status,
        "issue": issue,
        "bytes_completed": int(bytes_completed or 0),
        "file_length": length,
    }
