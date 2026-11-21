import sqlite3
import tempfile
import os
from typer.testing import CliRunner
from retrobridge.cli.user import app

runner = CliRunner()


def _create_legacy_db(path: str) -> None:
    """Creates a minimal v1 database with the schema expected by the legacy import."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            system TEXT NOT NULL,
            download_status TEXT,
            matched_torrent_id INTEGER,
            matched_file_index INTEGER,
            matched_file_path TEXT,
            matched_file_name TEXT,
            match_score INTEGER DEFAULT 0,
            match_lang_tag TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE torrent_cache (
            torrent_id INTEGER NOT NULL,
            torrent_name TEXT NOT NULL,
            file_index INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            file_length INTEGER DEFAULT 0,
            is_wanted INTEGER DEFAULT 0,
            bytes_completed INTEGER DEFAULT 0,
            PRIMARY KEY(torrent_id, file_index)
        );
    """)
    conn.execute(
        "INSERT INTO games (title, system, download_status, matched_file_name, match_score) "
        "VALUES ('ActRaiser 2', 'SNES', 'LINKED', 'ActRaiser 2 (USA).zip', 1000);"
    )
    conn.execute(
        "INSERT INTO games (title, system, download_status) "
        "VALUES ('Jeu Fantome', 'SNES', NULL);"
    )
    conn.execute(
        "INSERT INTO torrent_cache (torrent_id, torrent_name, file_index, file_name) "
        "VALUES (1, 'SNES Pack', 0, 'ActRaiser 2 (USA).zip');"
    )
    conn.commit()
    conn.close()


def test_cli_list_and_import():
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as legacy_tmp:
        legacy_path = legacy_tmp.name
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as target_tmp:
        target_path = target_tmp.name

    try:
        _create_legacy_db(legacy_path)

        # 1. Test the tools db import-legacy command
        res_import = runner.invoke(app, [
            "tools", "db", "import-legacy",
            legacy_path,
            "--db", target_path
        ])
        assert res_import.exit_code == 0
        assert "Import complete" in res_import.output

        # A game with no real file is never imported
        with sqlite3.connect(target_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM games WHERE title = 'Jeu Fantome';"
            ).fetchone()[0] == 0

        # 2. Test retrobridge list
        res_list = runner.invoke(app, ["list", "--system", "SNES", "--db", target_path])
        assert res_list.exit_code == 0
        assert "ActRaiser 2" in res_list.output

        # 3. Test retrobridge list --json
        res_json = runner.invoke(app, ["list", "--system", "SNES", "--json", "--db", target_path])
        assert res_json.exit_code == 0
        assert '"title": "ActRaiser 2"' in res_json.output
    finally:
        for p in (legacy_path, target_path):
            if os.path.exists(p):
                os.remove(p)
