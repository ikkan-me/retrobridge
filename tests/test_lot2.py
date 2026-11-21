import pytest
from pathlib import Path
from typer.testing import CliRunner

from retrobridge.config import Config, PathsConfig, load_config, save_config
from retrobridge.storage.lock import file_lock, LockError
from retrobridge.storage.db import init_db, get_db_connection, set_meta, get_meta, is_ttl_expired
from retrobridge.profiles.loader import load_profile, list_profiles
from retrobridge.services.sync import run_sync
from retrobridge.cli.user import app

runner = CliRunner()

def test_config_serialization(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg = Config()
    cfg.paths.download_dir = tmp_path / "downloads"
    cfg.paths.library_dir = tmp_path / "library"
    cfg.transmission.host = "192.168.1.100"

    save_config(cfg, cfg_file)
    assert cfg_file.exists()

    loaded = load_config(cfg_file)
    assert loaded.paths.download_dir == tmp_path / "downloads"
    assert loaded.transmission.host == "192.168.1.100"
    assert loaded.transmission.port == 9091

def test_file_lock(tmp_path: Path):
    lock_dir = tmp_path / "lock_test"
    with file_lock(lock_dir):
        # Trying to acquire the same lock must fail
        with pytest.raises(LockError):
            with file_lock(lock_dir):
                pass
    # After leaving the with block, the lock must be released and re-acquired without error
    with file_lock(lock_dir):
        pass

def test_profiles_loader():
    profiles = list_profiles()
    assert "esde" in profiles
    assert "knulli" in profiles

    esde = load_profile("esde")
    assert esde.name == "esde"
    assert esde.get_system("snes").dir == "snes"
    assert ".sfc" in esde.get_system("snes").extensions
    assert esde.get_system("psx").export_format == "chd"
    assert esde.get_system("psx").multidisc_m3u is True

def test_meta_and_ttl(tmp_path: Path):
    db_file = tmp_path / "test_meta.db"
    init_db(str(db_file))

    with get_db_connection(str(db_file)) as conn:
        assert is_ttl_expired(conn, "last_scan", 3600) is True
        set_meta(conn, "last_scan", "done")
        assert get_meta(conn, "last_scan") == "done"
        assert is_ttl_expired(conn, "last_scan", 3600) is False

def test_sync_linking_and_idempotence(tmp_path: Path):
    db_file = tmp_path / "test_sync.db"
    init_db(str(db_file))

    dl_dir = tmp_path / "downloads"
    lib_dir = tmp_path / "library"
    dl_dir.mkdir(parents=True)

    # Create a fake downloaded file
    rom_file = dl_dir / "Super Mario World (USA).sfc"
    rom_file.write_text("dummy rom content")

    # Insert the game into the DB with state downloaded
    with get_db_connection(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_path, matched_file_name) "
            "VALUES (?, ?, 'downloaded', ?, ?);",
            ("Super Mario World", "snes", "Super Mario World (USA).sfc", "Super Mario World (USA).sfc")
        )
        conn.commit()

    cfg = Config(paths=PathsConfig(download_dir=dl_dir, library_dir=lib_dir, data_dir=tmp_path))

    # Test 1: Dry run
    res_dry = run_sync(config=cfg, db_path=str(db_file), dry_run=True, skip_scan=True, apply=False)
    assert res_dry["links_created_or_verified"] == 1
    dest_file = lib_dir / "snes" / "Super Mario World (USA).sfc"
    assert not dest_file.exists() # Non créé en dry-run

    # Test 2: Real run
    res_real = run_sync(config=cfg, db_path=str(db_file), dry_run=False, skip_scan=True, apply=False)
    assert res_real["links_created_or_verified"] == 1
    assert dest_file.exists()
    assert dest_file.read_text() == "dummy rom content"

    with get_db_connection(str(db_file)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT state FROM games WHERE title = 'Super Mario World';")
        assert cur.fetchone()["state"] == "linked"

    # Test 3: Idempotence (rerun must not break the link or raise)
    res_again = run_sync(config=cfg, db_path=str(db_file), dry_run=False, skip_scan=True, apply=False)
    assert res_again["links_created_or_verified"] == 1
    assert dest_file.exists()

def test_cli_status_and_sync_json(tmp_path: Path):
    db_file = tmp_path / "test_cli.db"
    init_db(str(db_file))

    result = runner.invoke(app, ["status", "--json", "--no-refresh", "--db", str(db_file)])
    assert result.exit_code == 0
    assert '"games"' in result.stdout
    assert '"storage"' in result.stdout
