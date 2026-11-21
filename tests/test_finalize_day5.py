"""Batch C tests: formatting, readable list, status with progress."""
from __future__ import annotations

import io
import sys
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retrobridge.cli import user as user_cli  # noqa: E402
from retrobridge.cli.formatting import (  # noqa: E402
    basename,
    format_size,
    format_size_compact,
    progress_bar,
    progress_text,
    truncate,
)
from retrobridge.storage.db import get_db_connection, init_db  # noqa: E402

runner = CliRunner()


def _recording_console(width: int = 120) -> Console:
    return Console(record=True, width=width, file=io.StringIO(), force_terminal=False)


def test_format_helpers():
    assert basename("a/b/c.zip") == "c.zip"
    assert basename(None) == "-"
    assert truncate("abcdef", 4) == "abc…"
    assert truncate("abc", 4) == "abc"
    assert format_size(1024 * 1024) == "1.0 MB"
    assert format_size_compact(1024 * 1024) == "1MB"
    assert format_size_compact(0) == "0"
    assert progress_bar(50, 10) == "🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜"
    assert "50.0%" in progress_text(50, width=10)


def _seed_list_db(db_path: Path) -> None:
    init_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        conn.executemany(
            "INSERT INTO games (title, system, state, matched_file_name, match_score, match_lang_tag) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            [
                ("Un jeu GBA au titre très long pour tester la troncature",
                 "GBA", "linked",
                 "Minerva_Myrient/No-Intro/Nintendo - Game Boy Advance/Un jeu GBA au titre très long pour tester la troncature (USA).zip",
                 1000, "🇺🇸 US 60Hz"),
                ("Short", "GBA", "downloading", "Short (Europe).zip", 500, "🇪🇺 PAL"),
                ("SNES Game", "SNES", "linked", "SNES Game (USA).sfc", 1000, "🇺🇸 US 60Hz"),
            ],
        )
        conn.commit()


def test_list_groups_by_console_and_uses_basename(tmp_path, monkeypatch):
    db_path = tmp_path / "list.db"
    _seed_list_db(db_path)
    console = _recording_console(width=120)
    monkeypatch.setattr(user_cli, "console", console)

    result = runner.invoke(user_cli.app, ["list", "--system", "GBA", "--db", str(db_path)])
    output = console.export_text()

    assert result.exit_code == 0, result.stdout
    assert "GBA (2)" in output
    assert "SNES" not in output
    assert "Un jeu GBA au titre très long" in output
    assert "Minerva_Myrient" not in output
    assert "Un jeu GBA au titre très long pour tester la troncature (USA).zip" not in output
    assert "…" in output


def test_list_wide_shows_full_values(tmp_path, monkeypatch):
    db_path = tmp_path / "list_wide.db"
    _seed_list_db(db_path)
    console = _recording_console(width=160)
    monkeypatch.setattr(user_cli, "console", console)

    result = runner.invoke(user_cli.app, ["list", "--system", "GBA", "--wide", "--db", str(db_path)])
    output = console.export_text()

    assert result.exit_code == 0, result.stdout
    assert "Minerva_Myrient/No-Intro" in output
    assert "Un jeu GBA au titre très long pour tester la troncature" in output


def test_list_sort_title_uses_single_table_with_system(tmp_path, monkeypatch):
    db_path = tmp_path / "list_sort.db"
    _seed_list_db(db_path)
    console = _recording_console(width=120)
    monkeypatch.setattr(user_cli, "console", console)

    result = runner.invoke(user_cli.app, ["list", "--sort", "title", "--db", str(db_path)])
    output = console.export_text()

    assert result.exit_code == 0, result.stdout
    assert "(sort: title)" in output
    assert "System" in output
    assert "SNES Game" in output


def test_list_json_unchanged(tmp_path):
    db_path = tmp_path / "list_json.db"
    _seed_list_db(db_path)
    result = runner.invoke(user_cli.app, ["list", "--json", "--db", str(db_path)])
    assert result.exit_code == 0
    assert '"title"' in result.stdout
    assert '"matched_file_name"' in result.stdout


def _status_payload() -> dict:
    return {
        "games": {
            "total": 3,
            "downloading": 1,
            "downloaded": 0,
            "downloaded_pending": 0,
            "acquired": 2,
            "linked": 2,
            "removing": 0,
        },
        "systems": [
            {
                "system": "GBA", "total": 3, "linked": 2, "downloaded": 0,
                "downloaded_pending": 0, "acquired": 2, "downloading": 1,
                "progress_pct": 66.7,
            }
        ],
        "active_downloads": [
            {
                "title": "Un jeu en cours",
                "system": "GBA",
                "file_name": "Un jeu en cours (USA).zip",
                "matched_file_path": "GBA/Un jeu en cours (USA).zip",
                "matched_file_name": "Un jeu en cours (USA).zip",
                "progress_pct": 42.0,
                "bytes_completed": 42 * 1024 * 1024,
                "file_length": 100 * 1024 * 1024,
                "download_status": "downloading",
                "issue": None,
            }
        ],
        "active_downloads_total": 1,
        "download_status_counts": {"downloading": 1},
        "target_issues": 0,
        "freshness": {"stale": False, "scan": {}, "sync": {}},
        "timer": {"installed": False},
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


def test_status_shows_acquired_and_progress(tmp_path, monkeypatch):
    db_path = tmp_path / "status.db"
    init_db(str(db_path))
    console = _recording_console(width=120)
    monkeypatch.setattr(user_cli, "console", console)
    monkeypatch.setattr(user_cli, "get_system_status", lambda **kwargs: _status_payload())

    result = runner.invoke(user_cli.app, ["status", "--json", "--no-refresh", "--no-live", "--db", str(db_path)])
    # --json bypasses rendering; test the readable output without --json.
    assert result.exit_code == 0
    result = runner.invoke(user_cli.app, ["status", "--no-refresh", "--no-live", "--db", str(db_path)])
    output = console.export_text()

    assert result.exit_code == 0, result.stdout
    assert "Downloaded: 2" in output
    assert "including linked: 2" in output
    assert "42%" in output
    assert "🟩" in output
    assert "📥" in output
    assert "Un jeu en cours (USA).zip" in output
    assert "GBA/Un jeu en cours" not in output


def test_status_full_path(tmp_path, monkeypatch):
    db_path = tmp_path / "status_path.db"
    init_db(str(db_path))
    console = _recording_console(width=160)
    monkeypatch.setattr(user_cli, "console", console)
    monkeypatch.setattr(user_cli, "get_system_status", lambda **kwargs: _status_payload())

    result = runner.invoke(user_cli.app, ["status", "--no-refresh", "--no-live", "--full-path", "--db", str(db_path)])
    output = console.export_text()

    assert result.exit_code == 0, result.stdout
    assert "GBA/Un jeu en cours (USA).zip" in output
