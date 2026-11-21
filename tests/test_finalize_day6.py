"""Batch E tests: unified `find` command."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finalize_harness import make_config, seed_minimal_db  # noqa: E402

from retrobridge.cli import user as user_cli  # noqa: E402
from retrobridge.services import add as add_service  # noqa: E402
from retrobridge.services import find as find_service  # noqa: E402
from retrobridge.storage.db import get_db_connection, init_db, set_meta  # noqa: E402

runner = CliRunner()


class FakeTransmission:
    def __init__(self, **kwargs):
        self.calls = []

    def set_file_priorities(self, torrent_id, wanted_indices=None, unwanted_indices=None, chunk_size=1000):
        self.calls.append(("set", torrent_id, wanted_indices, unwanted_indices))

    def start_torrent(self, torrent_id):
        self.calls.append(("start", torrent_id))


class FailingTransmission:
    def __init__(self, **kwargs):
        raise RuntimeError("RPC indisponible")


def _seed_find_db(db_path: Path) -> None:
    init_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        conn.executemany(
            "INSERT INTO torrent_cache (torrent_id, torrent_name, file_index, file_name, file_length, is_wanted, bytes_completed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?);",
            [
                (99, "Minerva_Myrient/Redump/Sony - PlayStation 2", 0, "New Game (USA).zip", 1000, 0, 0),
                (99, "Minerva_Myrient/Redump/Sony - PlayStation 2", 1, "New Game (Europe).zip", 1000, 0, 0),
                (99, "Minerva_Myrient/Redump/Sony - PlayStation 2", 2, "New Adventure (USA).zip", 1000, 0, 0),
            ],
        )
        conn.commit()


def test_find_existing_is_accent_insensitive(tmp_path):
    db_path = tmp_path / "existing.db"
    init_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_name) VALUES "
            "('Pokémon Version Or', 'GBC', 'linked', 'Pokemon - Version Or.zip');"
        )
        conn.commit()

    found = find_service.find_existing_game("Pokemon Version Or", "GBC", db_path=str(db_path))
    assert found is not None
    assert found["title"] == "Pokémon Version Or"


def test_refresh_cache_disabled_and_fresh(tmp_path, monkeypatch):
    db_path = tmp_path / "refresh.db"
    init_db(str(db_path))
    cfg = make_config(tmp_path)

    assert find_service.refresh_cache_if_needed(config=cfg, db_path=str(db_path), refresh=False)["reason"] == "disabled"

    with get_db_connection(str(db_path)) as conn:
        set_meta(conn, "last_scan", "completed")
        conn.commit()

    called = []
    monkeypatch.setattr(find_service, "scan_torrents", lambda **kwargs: called.append(kwargs))
    result = find_service.refresh_cache_if_needed(config=cfg, db_path=str(db_path), refresh=True)
    assert result["ran"] is False
    assert called == []


def test_refresh_cache_stale_triggers_scan(tmp_path, monkeypatch):
    db_path = tmp_path / "stale.db"
    init_db(str(db_path))
    cfg = make_config(tmp_path)

    monkeypatch.setattr(
        find_service,
        "scan_torrents",
        lambda **kwargs: {"torrents_count": 2, "files_cached": 3},
    )
    result = find_service.refresh_cache_if_needed(config=cfg, db_path=str(db_path), refresh=True)
    assert result["ran"] is True
    assert result["torrents_count"] == 2


def test_find_readonly_does_not_add(tmp_path, monkeypatch):
    db_path = tmp_path / "readonly.db"
    _seed_find_db(db_path)
    monkeypatch.setattr(user_cli, "refresh_cache_if_needed", lambda *args, **kwargs: {"ran": False, "reason": "fresh"})

    result = runner.invoke(
        user_cli.app,
        ["find", "New Game", "-s", "ps2", "--no-add", "--no-refresh", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.stdout
    assert "New Game (USA).zip" in result.stdout

    with get_db_connection(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM games;").fetchone()["n"] == 0


def test_find_add_yes_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "add.db"
    _seed_find_db(db_path)
    monkeypatch.setattr(user_cli, "refresh_cache_if_needed", lambda *args, **kwargs: {"ran": False, "reason": "fresh"})
    monkeypatch.setattr(add_service, "TransmissionClient", FakeTransmission)

    first = runner.invoke(
        user_cli.app,
        ["find", "New Game", "-s", "ps2", "--add", "--yes", "--no-refresh", "--db", str(db_path)],
    )
    assert first.exit_code == 0, first.stdout
    assert "Game added" in first.stdout

    with get_db_connection(str(db_path)) as conn:
        rows = conn.execute("SELECT title, state, matched_file_name FROM games;").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "New Game"
    assert rows[0]["matched_file_name"].endswith("New Game (USA).zip")

    second = runner.invoke(
        user_cli.app,
        ["find", "New Game", "-s", "ps2", "--add", "--yes", "--no-refresh", "--db", str(db_path)],
    )
    assert second.exit_code == 0, second.stdout
    assert "Already present" in second.stdout
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM games;").fetchone()["n"] == 1


def test_find_yes_requires_exact_match(tmp_path):
    db_path = tmp_path / "ambiguous.db"
    _seed_find_db(db_path)
    result = runner.invoke(
        user_cli.app,
        ["find", "New", "-s", "ps2", "--add", "--yes", "--no-refresh", "--json", "--db", str(db_path)],
    )
    assert result.exit_code == 1
    assert "exact match" in result.stdout


def test_find_dry_run_writes_nothing(tmp_path, monkeypatch):
    db_path = tmp_path / "dry.db"
    _seed_find_db(db_path)
    monkeypatch.setattr(add_service, "TransmissionClient", FakeTransmission)

    result = runner.invoke(
        user_cli.app,
        ["find", "New Game", "-s", "ps2", "--add", "--yes", "--dry-run", "--no-refresh", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.stdout
    assert "Simulated add" in result.stdout
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM games;").fetchone()["n"] == 0


def test_find_default_prompts_and_q_cancels(tmp_path, monkeypatch):
    db_path = tmp_path / "cancel.db"
    _seed_find_db(db_path)
    monkeypatch.setattr(add_service, "TransmissionClient", FakeTransmission)

    result = runner.invoke(
        user_cli.app,
        ["find", "New Game", "-s", "ps2", "--no-refresh", "--db", str(db_path)],
        input="q\n",
    )
    assert result.exit_code == 0, result.stdout
    assert "Nothing added" in result.stdout
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM games;").fetchone()["n"] == 0


def test_find_default_number_adds(tmp_path, monkeypatch):
    db_path = tmp_path / "default_add.db"
    _seed_find_db(db_path)
    monkeypatch.setattr(add_service, "TransmissionClient", FakeTransmission)

    result = runner.invoke(
        user_cli.app,
        ["find", "New Game", "-s", "ps2", "--no-refresh", "--db", str(db_path)],
        input="1\n",
    )
    assert result.exit_code == 0, result.stdout
    assert "Game added" in result.stdout
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM games;").fetchone()["n"] == 1


def test_find_json_default_is_readonly(tmp_path, monkeypatch):
    db_path = tmp_path / "json_default.db"
    _seed_find_db(db_path)
    monkeypatch.setattr(add_service, "TransmissionClient", FakeTransmission)

    result = runner.invoke(
        user_cli.app,
        ["find", "New Game", "-s", "ps2", "--no-refresh", "--json", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["candidates"]
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM games;").fetchone()["n"] == 0


def test_find_add_reports_rpc_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "rpc_fail.db"
    _seed_find_db(db_path)
    monkeypatch.setattr(add_service, "TransmissionClient", FailingTransmission)

    result = runner.invoke(
        user_cli.app,
        ["find", "New Game", "-s", "ps2", "--add", "--yes", "--no-refresh", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.stdout
    assert "Transmission could not be updated" in result.stdout
    assert "retrobridge sync" in result.stdout
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM games;").fetchone()["n"] == 1


def test_find_existing_without_update_does_nothing(tmp_path):
    db_path = tmp_path / "existing_game.db"
    cfg = make_config(tmp_path)
    seed_minimal_db(db_path, cfg.paths.download_dir)

    result = runner.invoke(
        user_cli.app,
        ["find", "Ico", "-s", "ps2", "--add", "--yes", "--no-refresh", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.stdout
    assert "Already present" in result.stdout
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM games;").fetchone()["n"] == 3
