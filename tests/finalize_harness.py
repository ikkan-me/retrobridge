"""Test harness for the Finalize phase (day 1).

Goal: provide a realistic SQLite database, a fake Transmission client,
and a temporary config, without network or external disk.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from retrobridge.config import Config, PathsConfig
from retrobridge.storage.db import get_db_connection, init_db, set_meta


class FakeTransmission:
    """Fake Transmission client: minimal API used by status/scan/sync.

    Calls are recorded in ``calls`` to verify that a read-only command
    does not write to Transmission.
    """

    def __init__(
        self,
        torrents: Optional[List[Dict[str, Any]]] = None,
        session_stats: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.torrents = torrents or []
        self.session_stats = session_stats or {
            "downloadSpeed": 0,
            "uploadSpeed": 0,
            "cumulative-stats": {"ratio": 0.0},
            "activeTorrentCount": 0,
            "torrentCount": len(self.torrents),
        }
        self.calls: List[tuple] = []
        self.unwanted: Dict[int, List[int]] = {}

    # --- read ------------------------------------------------------------
    def test_connection(self) -> bool:
        self.calls.append(("test_connection", {}))
        return True

    def get_session_stats(self) -> Dict[str, Any]:
        self.calls.append(("get_session_stats", {}))
        return self.session_stats

    def get_torrents(self, fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        self.calls.append(("get_torrents", {"fields": fields}))
        return self.torrents

    def get_torrent_file_count(self, torrent_id: int) -> int:
        self.calls.append(("get_torrent_file_count", {"torrent_id": torrent_id}))
        for torrent in self.torrents:
            if torrent["id"] == torrent_id:
                return len(torrent.get("files", []))
        return 0

    # --- write -----------------------------------------------------------
    def set_file_priorities(
        self,
        torrent_id: int,
        wanted_indices: Optional[List[int]] = None,
        unwanted_indices: Optional[List[int]] = None,
        chunk_size: int = 1000,
    ) -> None:
        self.calls.append((
            "set_file_priorities",
            {
                "torrent_id": torrent_id,
                "wanted": list(wanted_indices or []),
                "unwanted": list(unwanted_indices or []),
            },
        ))
        if unwanted_indices:
            self.unwanted.setdefault(torrent_id, []).extend(unwanted_indices)

    def start_torrent(self, torrent_id: int) -> None:
        self.calls.append(("start_torrent", {"torrent_id": torrent_id}))

    def stop_torrent(self, torrent_id: int) -> None:
        self.calls.append(("stop_torrent", {"torrent_id": torrent_id}))

    def select_nothing(self, torrent_id: int) -> int:
        self.calls.append(("select_nothing", {"torrent_id": torrent_id}))
        return self.get_torrent_file_count(torrent_id)


def make_config(tmp_path: Path) -> Config:
    download_dir = tmp_path / "download"
    library_dir = tmp_path / "library"
    data_dir = tmp_path / "data"
    for directory in (download_dir, library_dir, data_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return Config(
        paths=PathsConfig(
            download_dir=download_dir,
            library_dir=library_dir,
            data_dir=data_dir,
        )
    )


def seed_minimal_db(db_path: Path, download_dir: Path, with_broken: bool = False) -> None:
    """Creates a mini collection: 1 linked game + 2 queued.

    With ``with_broken=True``, adds a broken target (missing file and
    ``is_wanted=0``) to test targeted reconciliation.
    """
    init_db(str(db_path))

    linked_file = download_dir / "PS2" / "Ico (USA).zip"
    linked_file.parent.mkdir(parents=True, exist_ok=True)
    linked_file.write_bytes(b"PK\x05\x06" + b"\0" * 18)

    cache_rows = [
        (10, "Minerva_Myrient/Redump/Sony - PlayStation 2", 0,
         "Ico (USA).zip", 1000, 1, 1000),
        (10, "Minerva_Myrient/Redump/Sony - PlayStation 2", 1,
         "Shadow of the Colossus (USA).zip", 2000, 1, 0),
        (1, "Minerva_Myrient/TOSEC/Nintendo/Game Boy/Games", 0,
         "Cave Noire (1991)(Konami)(JP).zip", 500, 1, 0),
    ]
    game_rows = [
        ("Ico", "PS2", "linked", 10, 0,
         "PS2/Ico (USA).zip", "Ico (USA).zip", 1000, "🇺🇸 US 60Hz"),
        ("Shadow of the Colossus", "PS2", "downloading", 10, 1,
         "PS2/Shadow of the Colossus (USA).zip",
         "Shadow of the Colossus (USA).zip", 1000, "🇺🇸 US 60Hz"),
        ("Cave Noir", "GB", "downloading", 1, 0,
         "TOSEC/Nintendo/Game Boy/Games/Cave Noire (1991)(Konami)(JP).zip",
         "Cave Noire (1991)(Konami)(JP).zip", 620, "🇯🇵 Japan 60Hz"),
    ]
    if with_broken:
        cache_rows.append(
            (10, "Minerva_Myrient/Redump/Sony - PlayStation 2", 2,
             "Broken Game (USA).zip", 3000, 0, 0)
        )
        game_rows.append(
            ("Broken Game", "PS2", "downloading", 10, 2,
             "PS2/Broken Game (USA).zip", "Broken Game (USA).zip", 1000, "🇺🇸 US 60Hz")
        )

    with get_db_connection(str(db_path)) as conn:
        conn.executemany(
            "INSERT INTO torrent_cache "
            "(torrent_id, torrent_name, file_index, file_name, file_length, is_wanted, bytes_completed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?);",
            cache_rows,
        )
        conn.executemany(
            "INSERT INTO games (title, system, state, matched_torrent_id, matched_file_index, "
            "matched_file_path, matched_file_name, match_score, match_lang_tag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
            game_rows,
        )
        set_meta(conn, "last_scan", "completed")
        set_meta(conn, "last_sync", "completed")
        conn.commit()


def fake_torrents() -> List[Dict[str, Any]]:
    """Two torrents consistent with ``seed_minimal_db``."""
    return [
        {
            "id": 10,
            "name": "Minerva_Myrient/Redump/Sony - PlayStation 2",
            "downloadDir": "/tmp/download",
            "percentDone": 0.5,
            "status": 4,
            "files": [
                {"name": "PS2/Ico (USA).zip", "length": 1000, "bytesCompleted": 1000},
                {"name": "PS2/Shadow of the Colossus (USA).zip", "length": 2000, "bytesCompleted": 0},
            ],
        },
        {
            "id": 1,
            "name": "Minerva_Myrient/TOSEC/Nintendo/Game Boy/Games",
            "downloadDir": "/tmp/download",
            "percentDone": 0.0,
            "status": 4,
            "files": [
                {
                    "name": "TOSEC/Nintendo/Game Boy/Games/Cave Noire (1991)(Konami)(JP).zip",
                    "length": 500,
                    "bytesCompleted": 0,
                }
            ],
        },
    ]
