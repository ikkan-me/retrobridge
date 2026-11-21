"""Day 2 tests: guarded auto-sync (A0) + exposed freshness (A1)."""
from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finalize_harness import (  # noqa: E402
    FakeTransmission,
    fake_torrents,
    make_config,
    seed_minimal_db,
)

from retrobridge.cli import user as user_cli  # noqa: E402
from retrobridge.services import status as status_service  # noqa: E402
from retrobridge.services.sync import run_sync  # noqa: E402
from retrobridge.storage.db import get_db_connection, init_db  # noqa: E402

runner = CliRunner()


def _minimal_status() -> dict:
    return {
        "games": {"total": 0, "downloading": 0, "downloaded": 0, "linked": 0, "removing": 0},
        "systems": [],
        "active_downloads": [],
        "freshness": {"stale": False, "scan": {}, "sync": {}},
        "transmission": {"reachable": False},
        "storage": {
            "download_dir": {"free_gb": 0},
            "library_dir": {"free_gb": 0},
            "same_partition_for_hardlinks": True,
        },
        "targets": {},
        "pending_actions": {},
        "selection": {"wanted_files": 0, "wanted_bytes": 0, "selected_bytes": 0},
    }


def test_safe_sync_does_not_execute_destructive_actions(tmp_path):
    cfg = make_config(tmp_path)
    db_path = tmp_path / "safe.db"
    init_db(str(db_path))

    rom = cfg.paths.download_dir / "PS2" / "Old Game (USA).zip"
    rom.parent.mkdir(parents=True, exist_ok=True)
    rom.write_bytes(b"dummy")

    with get_db_connection(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO torrent_cache (torrent_id, torrent_name, file_index, file_name, file_length, is_wanted, bytes_completed) "
            "VALUES (99, 'Pack PS2', 0, 'Old Game (USA).zip', 5, 1, 5);"
        )
        cur = conn.execute(
            "INSERT INTO games (title, system, state, matched_torrent_id, matched_file_index, matched_file_path, matched_file_name) "
            "VALUES ('Old Game', 'PS2', 'removing', 99, 0, 'PS2/Old Game (USA).zip', 'Old Game (USA).zip');"
        )
        game_id = cur.lastrowid
        conn.execute(
            "INSERT INTO pending_actions (game_id, action_type) VALUES (?, 'delete_download');",
            (game_id,),
        )
        conn.commit()

    result = run_sync(
        config=cfg,
        db_path=str(db_path),
        skip_scan=True,
        apply=False,
        execute_pending=False,
        reconcile_transmission=False,
    )

    assert rom.exists(), "le mode safe ne doit jamais supprimer de ROM"
    assert result["pending_executed"] == 0
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM pending_actions;").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM games WHERE state='removing';").fetchone()["n"] == 1


def test_cli_explicit_refresh_uses_safe_flags(tmp_path, monkeypatch):
    db_path = tmp_path / "cli.db"
    init_db(str(db_path))
    calls = []

    def fake_sync(**kwargs):
        calls.append(kwargs)
        return {
            "newly_downloaded": 0,
            "links_created_or_verified": 0,
            "errors": [],
        }

    monkeypatch.setattr(user_cli, "run_sync", fake_sync)
    monkeypatch.setattr(user_cli, "is_ttl_expired", lambda *args, **kwargs: True)
    monkeypatch.setattr(user_cli, "get_system_status", lambda **kwargs: _minimal_status())

    result = runner.invoke(user_cli.app, ["status", "--refresh", "--json", "--db", str(db_path)])

    assert result.exit_code == 0, result.stdout
    assert len(calls) == 1
    assert calls[0]["skip_scan"] is True
    assert calls[0]["apply"] is False
    assert calls[0]["execute_pending"] is False
    assert calls[0]["reconcile_transmission"] is False


def test_cli_json_auto_sync_is_safe(tmp_path, monkeypatch):
    db_path = tmp_path / "cli2.db"
    init_db(str(db_path))
    calls = []

    def fake_sync(**kwargs):
        calls.append(kwargs)
        return {"newly_downloaded": 0, "links_created_or_verified": 0, "errors": []}

    monkeypatch.setattr(user_cli, "run_sync", fake_sync)
    monkeypatch.setattr(user_cli, "is_ttl_expired", lambda *args, **kwargs: True)
    monkeypatch.setattr(user_cli, "get_system_status", lambda **kwargs: _minimal_status())

    result = runner.invoke(user_cli.app, ["status", "--json", "--db", str(db_path)])

    assert result.exit_code == 0, result.stdout
    assert len(calls) == 1, "l'auto-sync doit rester actif par défaut, même en --json"
    assert calls[0]["skip_scan"] is True
    assert calls[0]["apply"] is False
    assert calls[0]["execute_pending"] is False
    assert calls[0]["reconcile_transmission"] is False


def test_cli_no_refresh_is_strictly_readonly(tmp_path, monkeypatch):
    db_path = tmp_path / "cli3.db"
    init_db(str(db_path))
    calls = []

    monkeypatch.setattr(user_cli, "run_sync", lambda **kwargs: calls.append(kwargs) or {})
    monkeypatch.setattr(user_cli, "is_ttl_expired", lambda *args, **kwargs: True)
    monkeypatch.setattr(user_cli, "get_system_status", lambda **kwargs: _minimal_status())

    result = runner.invoke(user_cli.app, ["status", "--json", "--no-refresh", "--db", str(db_path)])

    assert result.exit_code == 0, result.stdout
    assert calls == [], "--no-refresh ne doit jamais lancer de sync"


def test_status_exposes_freshness(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh.db"
    cfg = make_config(tmp_path)
    seed_minimal_db(db_path, cfg.paths.download_dir)

    fake = FakeTransmission(fake_torrents())
    monkeypatch.setattr(status_service, "TransmissionClient", lambda **kwargs: fake)

    result = status_service.get_system_status(config=cfg, db_path=str(db_path))

    assert "freshness" in result
    assert result["freshness"]["scan"]["age_seconds"] is not None
    assert result["freshness"]["scan"]["stale"] is False
    assert result["freshness"]["sync"]["age_seconds"] is not None
