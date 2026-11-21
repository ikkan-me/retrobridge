"""Batch F tests: multi-disc games and game_files."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finalize_harness import make_config  # noqa: E402

from retrobridge.config import Config, PathsConfig, TargetConfig  # noqa: E402
from retrobridge.services import add as add_service  # noqa: E402
from retrobridge.services import status as status_service  # noqa: E402
from retrobridge.services.export import export_to_target  # noqa: E402
from retrobridge.services.find import expand_disc_candidates  # noqa: E402
from retrobridge.services.sync import run_sync  # noqa: E402
from retrobridge.storage.db import (  # noqa: E402
    backfill_game_files,
    get_db_connection,
    init_db,
)


class FakeTransmission:
    def __init__(self, **kwargs):
        self.calls = []

    def set_file_priorities(self, torrent_id, wanted_indices=None, unwanted_indices=None, chunk_size=1000):
        self.calls.append(("set", torrent_id, wanted_indices, unwanted_indices))

    def start_torrent(self, torrent_id):
        self.calls.append(("start", torrent_id))


def _cache_rows() -> list[tuple]:
    return [
        (99, "Minerva_Myrient/Redump/Sony - PlayStation", 0, "Test Game (USA) (Disc 1).zip", 100, 50),
        (99, "Minerva_Myrient/Redump/Sony - PlayStation", 1, "Test Game (USA) (Disc 2).zip", 200, 100),
        (99, "Minerva_Myrient/Redump/Sony - PlayStation", 2, "Test Game (USA) (Disc 3).zip", 300, 0),
    ]


def _seed_cache(conn) -> None:
    conn.executemany(
        "INSERT INTO torrent_cache (torrent_id, torrent_name, file_index, file_name, file_length, bytes_completed) "
        "VALUES (?, ?, ?, ?, ?, ?);",
        [(tid, name, idx, fn, length, completed) for tid, name, idx, fn, length, completed in _cache_rows()],
    )


def test_backfill_creates_disc1(tmp_path):
    db_path = tmp_path / "backfill.db"
    init_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_torrent_id, matched_file_index, "
            "matched_file_path, matched_file_name) VALUES "
            "('Old Game', 'psx', 'linked', 1, 2, 'Old Game (Disc 1).zip', 'Old Game (Disc 1).zip');"
        )
        conn.commit()
        inserted = backfill_game_files(conn)
        assert inserted == 1
        row = conn.execute("SELECT disc_number, file_name FROM game_files;").fetchone()
    assert row["disc_number"] == 1
    assert row["file_name"] == "Old Game (Disc 1).zip"


def test_find_expands_and_adds_all_discs(tmp_path, monkeypatch):
    db_path = tmp_path / "add_multi.db"
    init_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        _seed_cache(conn)
        conn.commit()

    candidate = {
        "torrent_id": 99,
        "file_index": 0,
        "file_name": "Test Game (USA) (Disc 1).zip",
        "file_length": 100,
        "size_mb": 0.0,
        "score": 1000,
        "lang_label": "US 60Hz",
    }
    discs = expand_disc_candidates(candidate, "psx", db_path=str(db_path))
    assert [d["file_index"] for d in discs] == [0, 1, 2]

    monkeypatch.setattr(add_service, "TransmissionClient", FakeTransmission)
    result = add_service.add_game(
        "Test Game", "psx", selected_candidates=discs, db_path=str(db_path)
    )
    assert result["discs"] == 3
    with get_db_connection(str(db_path)) as conn:
        rows = conn.execute("SELECT disc_number, file_name FROM game_files ORDER BY disc_number;").fetchall()
        game = conn.execute("SELECT matched_file_name FROM games;").fetchone()
    assert [r["disc_number"] for r in rows] == [1, 2, 3]
    assert game["matched_file_name"].endswith("Disc 1).zip")


def _seed_multi_game(db_path: Path, download_dir: Path, library_dir: Path) -> None:
    init_db(str(db_path))
    for name in ("Test Game (USA) (Disc 1).zip", "Test Game (USA) (Disc 2).zip"):
        (download_dir / name).write_bytes(b"x" * 10)
    with get_db_connection(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_torrent_id, matched_file_index, "
            "matched_file_path, matched_file_name) VALUES "
            "('Test Game', 'psx', 'downloaded', 99, 0, ?, ?);",
            ("Test Game (USA) (Disc 1).zip", "Test Game (USA) (Disc 1).zip"),
        )
        game_id = conn.execute("SELECT id FROM games;").fetchone()["id"]
        for disc, name in ((1, "Test Game (USA) (Disc 1).zip"), (2, "Test Game (USA) (Disc 2).zip")):
            conn.execute(
                "INSERT INTO game_files (game_id, disc_number, torrent_id, file_index, file_path, file_name, file_length) "
                "VALUES (?, ?, 99, ?, ?, ?, 10);",
                (game_id, disc, disc - 1, name, name),
            )
        conn.commit()


def test_sync_links_all_discs_and_generates_m3u(tmp_path):
    db_path = tmp_path / "sync_multi.db"
    dl_dir = tmp_path / "downloads"
    lib_dir = tmp_path / "library"
    dl_dir.mkdir()
    lib_dir.mkdir()
    _seed_multi_game(db_path, dl_dir, lib_dir)
    cfg = Config(paths=PathsConfig(download_dir=dl_dir, library_dir=lib_dir, data_dir=tmp_path))

    result = run_sync(config=cfg, db_path=str(db_path), skip_scan=True, apply=False)

    assert result["links_created_or_verified"] == 2
    assert (lib_dir / "psx" / "Test Game (USA) (Disc 1).zip").is_file()
    assert (lib_dir / "psx" / "Test Game (USA) (Disc 2).zip").is_file()
    m3u = lib_dir / "psx" / "Test Game (USA).m3u"
    assert m3u.is_file()
    content = m3u.read_text()
    assert "Disc 1" in content and "Disc 2" in content
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute("SELECT state FROM games;").fetchone()["state"] == "linked"


def test_export_all_discs_and_m3u(tmp_path):
    db_path = tmp_path / "export_multi.db"
    dl_dir = tmp_path / "downloads"
    lib_dir = tmp_path / "library"
    sd = tmp_path / "sd"
    dl_dir.mkdir()
    lib_dir.mkdir()
    sd.mkdir()
    _seed_multi_game(db_path, dl_dir, lib_dir)
    (sd / "ROMs" / "psx").mkdir(parents=True)
    (sd / "ROMs" / "psx" / "Unknown (USA).zip").write_bytes(b"extra")

    cfg = Config(
        paths=PathsConfig(download_dir=dl_dir, library_dir=lib_dir, data_dir=tmp_path),
        targets={"sd": TargetConfig(name="sd", profile="esde", mount_path=sd, uuid="uuid")},
    )
    result = export_to_target("sd", config=cfg, db_path=str(db_path), raw=True)

    assert result["exported"] == 2
    assert any("Unknown (USA).zip" in item["file"] for item in result["destination_only"])
    assert (sd / "ROMs" / "psx" / "Test Game (USA) (Disc 1).zip").is_file()
    assert (sd / "ROMs" / "psx" / "Test Game (USA) (Disc 2).zip").is_file()
    m3u = sd / "ROMs" / "psx" / "Test Game (USA).m3u"
    assert m3u.is_file()
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM export_files;").fetchone()["n"] == 2


def test_status_aggregates_discs(tmp_path):
    db_path = tmp_path / "status_multi.db"
    cfg = make_config(tmp_path)
    init_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        _seed_cache(conn)
        conn.execute(
            "INSERT INTO games (title, system, state, matched_torrent_id, matched_file_index, "
            "matched_file_path, matched_file_name) VALUES "
            "('Test Game', 'psx', 'downloading', 99, 0, 'Test Game (USA) (Disc 1).zip', "
            "'Test Game (USA) (Disc 1).zip');"
        )
        game_id = conn.execute("SELECT id FROM games;").fetchone()["id"]
        for disc, idx in ((1, 0), (2, 1)):
            name = f"Test Game (USA) (Disc {disc}).zip"
            conn.execute(
                "INSERT INTO game_files (game_id, disc_number, torrent_id, file_index, file_path, file_name, file_length) "
                "VALUES (?, ?, 99, ?, ?, ?, ?);",
                (game_id, disc, idx, name, name, 100 if disc == 1 else 200),
            )
        conn.commit()

    result = status_service.get_system_status(config=cfg, db_path=str(db_path), live=False)
    item = result["active_downloads"][0]
    assert item["discs_total"] == 2
    assert item["file_length"] == 300
    assert item["bytes_completed"] == 150
    assert item["progress_pct"] == 50.0
    system = next(s for s in result["systems"] if s["system"] == "PSX")
    assert system["size_bytes"] == 300
    assert result["games"]["downloading_bytes"] == 300
    assert result["games"]["acquired_bytes"] == 0
