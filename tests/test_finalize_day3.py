"""Day 3 tests: targeted reconciliation (A2) and --no-live mode (A3)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finalize_harness import make_config, seed_minimal_db  # noqa: E402

from retrobridge.cli import user as user_cli  # noqa: E402
from retrobridge.services import status as status_service  # noqa: E402
from retrobridge.storage.db import get_db_connection  # noqa: E402

from typer.testing import CliRunner  # noqa: E402

runner = CliRunner()


def _minimal_status() -> dict:
    return {
        "games": {"total": 0, "downloading": 0, "downloaded": 0, "linked": 0, "removing": 0},
        "systems": [],
        "active_downloads": [],
        "active_downloads_total": 0,
        "download_status_counts": {},
        "target_issues": 0,
        "freshness": {"stale": False, "scan": {}, "sync": {}},
        "transmission": {"live": False, "reachable": False},
        "storage": {
            "download_dir": {"free_gb": 0},
            "library_dir": {"free_gb": 0},
            "same_partition_for_hardlinks": True,
        },
        "targets": {},
        "pending_actions": {},
        "selection": {"wanted_files": 0, "wanted_bytes": 0, "selected_bytes": 0},
    }


def test_active_downloads_classification(tmp_path):
    cfg = make_config(tmp_path)
    db_path = tmp_path / "classify.db"
    seed_minimal_db(db_path, cfg.paths.download_dir, with_broken=True)

    result = status_service.get_system_status(config=cfg, db_path=str(db_path), live=False)
    by_title = {item["title"]: item for item in result["active_downloads"]}

    # File requested but not present yet: normal queue.
    assert by_title["Shadow of the Colossus"]["download_status"] == "queued"
    assert by_title["Shadow of the Colossus"]["target_exists"] is False
    assert by_title["Shadow of the Colossus"]["wanted"] is True
    assert by_title["Shadow of the Colossus"]["issue"] is None
    assert by_title["Shadow of the Colossus"]["file_name"] == "Shadow of the Colossus (USA).zip"

    # File missing AND unselected: broken target.
    assert by_title["Broken Game"]["download_status"] == "broken"
    assert by_title["Broken Game"]["wanted"] is False
    assert by_title["Broken Game"]["issue"] is not None

    assert result["active_downloads_total"] == 3
    assert result["download_status_counts"]["queued"] == 2
    assert result["download_status_counts"]["broken"] == 1
    assert result["target_issues"] == 1


def test_partial_and_orphan_classification(tmp_path):
    cfg = make_config(tmp_path)
    db_path = tmp_path / "partial.db"
    seed_minimal_db(db_path, cfg.paths.download_dir, with_broken=True)

    # Download in progress: .part present + progress cached.
    shadow_part = cfg.paths.download_dir / "PS2" / "Shadow of the Colossus (USA).zip.part"
    shadow_part.parent.mkdir(parents=True, exist_ok=True)
    shadow_part.write_bytes(b"x" * 500)
    # Orphan partial: .part present but file not selected.
    broken_part = cfg.paths.download_dir / "PS2" / "Broken Game (USA).zip.part"
    broken_part.write_bytes(b"x" * 10)

    with get_db_connection(str(db_path)) as conn:
        conn.execute(
            "UPDATE torrent_cache SET bytes_completed = 500 WHERE torrent_id = 10 AND file_index = 1;"
        )
        conn.commit()

    result = status_service.get_system_status(config=cfg, db_path=str(db_path), live=False)
    by_title = {item["title"]: item for item in result["active_downloads"]}

    shadow = by_title["Shadow of the Colossus"]
    assert shadow["download_status"] == "downloading"
    assert shadow["partial_exists"] is True
    assert shadow["target_exists"] is False
    assert shadow["progress_pct"] == 25.0

    broken = by_title["Broken Game"]
    assert broken["download_status"] == "orphan_partial"
    assert broken["issue"] is not None
    assert broken["wanted"] is False


def test_no_live_does_not_touch_transmission(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    db_path = tmp_path / "nolive.db"
    seed_minimal_db(db_path, cfg.paths.download_dir)

    def explode(**kwargs):  # pragma: no cover - ne doit jamais être appelé
        raise AssertionError("TransmissionClient ne doit pas être instancié avec live=False")

    monkeypatch.setattr(status_service, "TransmissionClient", explode)

    result = status_service.get_system_status(config=cfg, db_path=str(db_path), live=False)

    assert result["transmission"]["live"] is False
    assert result["transmission"]["reachable"] is False
    assert "download_rate_bytes" not in result["transmission"]


def test_no_live_budget(tmp_path):
    cfg = make_config(tmp_path)
    db_path = tmp_path / "budget.db"
    seed_minimal_db(db_path, cfg.paths.download_dir)

    started = time.perf_counter()
    status_service.get_system_status(config=cfg, db_path=str(db_path), live=False)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert elapsed_ms < 500, f"--no-live trop lent en CI : {elapsed_ms:.0f} ms"


def test_cli_no_live_passes_flag(tmp_path, monkeypatch):
    db_path = tmp_path / "cli.db"
    received = {}

    def fake_status(**kwargs):
        received.update(kwargs)
        return _minimal_status()

    monkeypatch.setattr(user_cli, "get_system_status", fake_status)

    result = runner.invoke(
        user_cli.app,
        ["status", "--json", "--no-refresh", "--no-live", "--db", str(db_path)],
    )

    assert result.exit_code == 0, result.stdout
    assert received.get("live") is False


def test_json_contract_contains_reconciliation_fields(tmp_path):
    cfg = make_config(tmp_path)
    db_path = tmp_path / "json.db"
    seed_minimal_db(db_path, cfg.paths.download_dir, with_broken=True)

    result = status_service.get_system_status(config=cfg, db_path=str(db_path), live=False)

    for key in (
        "active_downloads",
        "active_downloads_total",
        "download_status_counts",
        "target_issues",
        "freshness",
    ):
        assert key in result
    assert result["transmission"]["live"] is False
    item = result["active_downloads"][0]
    for key in (
        "file_name",
        "target_exists",
        "partial_exists",
        "wanted",
        "in_cache",
        "bytes_completed",
        "file_length",
        "progress_pct",
        "download_status",
        "issue",
    ):
        assert key in item
