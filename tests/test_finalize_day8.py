"""Tests du lot D : scan incrémental et sorties CLI."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finalize_harness import make_config  # noqa: E402

from retrobridge.services import scan as scan_service  # noqa: E402
from retrobridge.storage.db import get_db_connection, init_db  # noqa: E402


class FakeTransmission:
    def __init__(self, files, name="Pack", total_size=100):
        self.files = list(files)
        self.name = name
        self.total_size = total_size
        self.files_fetched = 0
        self.stats_fetched = 0

    def get_torrents(self, fields=None):
        if fields and "files" in fields:
            return [{
                "id": 1, "name": self.name,
                "files": [
                    {"name": name, "length": length, "bytesCompleted": completed}
                    for name, length, completed, wanted in self.files
                ],
                "fileStats": [
                    {"wanted": wanted, "bytesCompleted": completed}
                    for _, _, completed, wanted in self.files
                ],
            }]
        return [{
            "id": 1,
            "name": self.name,
            "file-count": len(self.files),
            "totalSize": self.total_size,
        }]

    def request(self, method, args=None):
        args = args or {}
        fields = args.get("fields", [])
        if "files" in fields:
            self.files_fetched += 1
            return {"arguments": {"torrents": self.get_torrents(fields=["files", "fileStats"])}}
        if "fileStats" in fields:
            self.stats_fetched += 1
            return {"arguments": {"torrents": [{
                "id": 1,
                "fileStats": [
                    {"wanted": wanted, "bytesCompleted": completed}
                    for _, _, completed, wanted in self.files
                ],
            }]}}
        return {"arguments": {"torrents": []}}


def test_scan_is_incremental(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    db_path = tmp_path / "scan.db"
    init_db(str(db_path))

    fake = FakeTransmission([
        ("Game (Disc 1).zip", 100, 50, True),
        ("Game (Disc 2).zip", 200, 0, False),
    ])
    monkeypatch.setattr(scan_service, "TransmissionClient", lambda **kwargs: fake)

    first = scan_service.scan_torrents(config=cfg, db_path=str(db_path))
    assert first["torrents_count"] == 1
    assert first["files_cached"] == 2
    assert first["changed_torrents"] == 1
    assert fake.files_fetched == 1
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM torrent_cache;").fetchone()["n"] == 2
        assert conn.execute("SELECT COUNT(*) AS n FROM torrent_meta;").fetchone()["n"] == 1

    second = scan_service.scan_torrents(config=cfg, db_path=str(db_path))
    assert second["changed_torrents"] == 0
    assert second["unchanged_torrents"] == 1
    assert second["files_cached"] == 2
    assert fake.files_fetched == 1  # pas de rechargement des noms
    assert fake.stats_fetched >= 1

    # A metadata change forces the file list to reload.
    changed = FakeTransmission([
        ("Game (Disc 1).zip", 100, 100, True),
        ("Game (Disc 2).zip", 200, 50, True),
        ("Game (Disc 3).zip", 300, 0, True),
    ], total_size=200)
    monkeypatch.setattr(scan_service, "TransmissionClient", lambda **kwargs: changed)
    third = scan_service.scan_torrents(config=cfg, db_path=str(db_path))
    assert third["changed_torrents"] == 1
    assert third["files_cached"] == 3
    assert changed.files_fetched == 1
