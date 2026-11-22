"""Auto-verification of files that already exist on disk.

When a pack is re-added (or its ``.resume`` is lost), Transmission starts from
0 % even though the files are already complete. Selecting those files then
re-downloads them **in place**, which is wasteful and can disturb hard-linked
library copies.

These helpers detect the "complete on disk but under-reported by Transmission"
case so the caller can request a ``torrent-verify`` instead of a download.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List


# Near-complete files (>= 90 %) do not need a forced verification: Transmission
# will simply fetch the few remaining pieces.
VERIFY_PROGRESS_RATIO = 0.9


def file_is_complete(download_dir: Path, rel: str, length: int) -> bool:
    """True when ``rel`` exists in the Download-Dir at (at least) its full size."""
    if not rel:
        return False
    path = download_dir / rel
    if not path.is_file():
        return False
    if length <= 0:
        # Length unknown: the file exists, let the caller verify it.
        return True
    try:
        return path.stat().st_size >= int(length)
    except OSError:
        return False


def under_reported(completed: int, length: int) -> bool:
    """True when Transmission reports little/no progress for a complete file."""
    if length <= 0:
        return True
    completed = max(0, int(completed or 0))
    return completed < int(length) * VERIFY_PROGRESS_RATIO


def needs_verification(
    download_dir: Path,
    file_name: str,
    file_length: int,
    bytes_completed: int,
) -> bool:
    return file_is_complete(download_dir, file_name, file_length) and under_reported(
        bytes_completed, file_length
    )


def torrents_needing_verify(
    download_dir: Path,
    rows: Iterable[Dict[str, Any]],
) -> Dict[int, List[int]]:
    """Groups the file indices to verify, per torrent.

    Each row must provide ``torrent_id``, ``file_index``, ``file_name``,
    ``file_length`` and ``bytes_completed``.
    """
    flagged: Dict[int, List[int]] = {}
    for row in rows:
        torrent_id = row.get("torrent_id")
        file_index = row.get("file_index")
        if torrent_id is None or file_index is None:
            continue
        rel = row.get("file_name") or row.get("file_path") or ""
        if needs_verification(
            download_dir,
            str(rel),
            int(row.get("file_length") or 0),
            int(row.get("bytes_completed") or 0),
        ):
            flagged.setdefault(int(torrent_id), []).append(int(file_index))
    return flagged


def is_missing_but_complete(
    download_dir: Path,
    file_name: str,
    file_length: int,
    bytes_completed: int,
) -> bool:
    """True when Transmission claims a file is complete but it is absent.

    Seen after an external deletion or a lost resume: the game stays
    ``downloading`` forever because Transmission never re-checks the file.
    A ``torrent-verify`` is required to reset its piece state.
    """
    if not file_name or int(file_length or 0) <= 0:
        return False
    if int(bytes_completed or 0) < int(file_length):
        return False
    final = download_dir / file_name
    part = download_dir / f"{file_name}.part"
    return not final.is_file() and not part.is_file()


def torrents_with_missing_complete(
    download_dir: Path,
    rows: Iterable[Dict[str, Any]],
) -> Dict[int, List[int]]:
    """Groups the file indices that are missing although marked complete."""
    flagged: Dict[int, List[int]] = {}
    for row in rows:
        torrent_id = row.get("torrent_id")
        file_index = row.get("file_index")
        if torrent_id is None or file_index is None:
            continue
        rel = row.get("file_name") or row.get("file_path") or ""
        if is_missing_but_complete(
            download_dir,
            str(rel),
            int(row.get("file_length") or 0),
            int(row.get("bytes_completed") or 0),
        ):
            flagged.setdefault(int(torrent_id), []).append(int(file_index))
    return flagged
