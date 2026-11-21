"""Tests de characterization du jour 1 (phase Finalize).

Ils ne testent pas encore les nouveaux lots : ils figent le harnais et le
contrat minimal de `status` pour pouvoir refactorer sans régression.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finalize_harness import (  # noqa: E402
    FakeTransmission,
    fake_torrents,
    make_config,
    seed_minimal_db,
)

from retrobridge.services import status as status_service  # noqa: E402
from retrobridge.storage.db import get_db_connection  # noqa: E402


@pytest.fixture()
def collection(tmp_path: Path):
    db_path = tmp_path / "retrobridge.db"
    cfg = make_config(tmp_path)
    seed_minimal_db(db_path, cfg.paths.download_dir)
    return cfg, db_path


def test_fixture_db_is_coherent(collection):
    cfg, db_path = collection
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM games;").fetchone()["n"] == 3
        assert conn.execute("SELECT COUNT(*) AS n FROM torrent_cache;").fetchone()["n"] == 3
        assert conn.execute("SELECT COUNT(*) AS n FROM games WHERE state='downloading';").fetchone()["n"] == 2
        assert conn.execute("SELECT COUNT(*) AS n FROM games WHERE state='linked';").fetchone()["n"] == 1


def test_fixture_distinguishes_linked_and_queued(collection):
    cfg, _ = collection
    download = cfg.paths.download_dir
    assert (download / "PS2" / "Ico (USA).zip").is_file()
    assert not (download / "PS2" / "Shadow of the Colossus (USA).zip").exists()
    assert not (download / "TOSEC/Nintendo/Game Boy/Games/Cave Noire (1991)(Konami)(JP).zip").exists()


def test_fake_transmission_contract():
    fake = FakeTransmission(fake_torrents())
    assert fake.test_connection() is True
    assert len(fake.get_torrents()) == 2
    stats = fake.get_session_stats()
    assert "cumulative-stats" in stats

    fake.set_file_priorities(10, wanted_indices=[0])
    fake.set_file_priorities(10, unwanted_indices=[1, 2])
    assert fake.unwanted == {10: [1, 2]}
    assert [c[0] for c in fake.calls] == [
        "test_connection",
        "get_torrents",
        "get_session_stats",
        "set_file_priorities",
        "set_file_priorities",
    ]


def test_status_reads_snapshot_without_writing(collection, monkeypatch):
    cfg, db_path = collection
    fake = FakeTransmission(fake_torrents())
    monkeypatch.setattr(status_service, "TransmissionClient", lambda **kwargs: fake)

    stat_before = os.stat(db_path)
    result = status_service.get_system_status(config=cfg, db_path=str(db_path))
    stat_after = os.stat(db_path)

    assert result["games"]["total"] == 3
    assert result["games"]["downloading"] == 2
    assert result["games"]["downloaded"] == 0
    assert result["games"]["downloaded_pending"] == 0
    assert result["games"]["linked"] == 1
    assert result["games"]["acquired"] == 1
    assert result["games"]["removing"] == 0
    assert result["transmission"]["reachable"] is True
    assert {item["title"] for item in result["active_downloads"]} == {
        "Cave Noir",
        "Shadow of the Colossus",
    }

    # A read must not modify the database...
    assert stat_before.st_mtime_ns == stat_after.st_mtime_ns
    assert stat_before.st_size == stat_after.st_size
    # ... nor Transmission.
    assert [call[0] for call in fake.calls] == ["get_session_stats"]


def test_status_db_only_budget(collection, monkeypatch):
    cfg, db_path = collection
    fake = FakeTransmission(fake_torrents())
    monkeypatch.setattr(status_service, "TransmissionClient", lambda **kwargs: fake)

    started = time.perf_counter()
    status_service.get_system_status(config=cfg, db_path=str(db_path))
    elapsed_ms = (time.perf_counter() - started) * 1000

    # Wide margin for CI: the product target is < 300 ms without RPC.
    assert elapsed_ms < 1000, f"status hors RPC trop lent : {elapsed_ms:.0f} ms"
