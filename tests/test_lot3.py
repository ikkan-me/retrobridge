from pathlib import Path
from typer.testing import CliRunner

from retrobridge.config import Config, PathsConfig
from retrobridge.storage.db import init_db, get_db_connection
from retrobridge.domain.multidisc import (
    extract_disc_number,
    get_base_title,
    get_disc_family,
    get_disc_search_title,
    generate_m3u_content,
)
from retrobridge.services.pin import set_game_pin
from retrobridge.services.removal import request_removal
from retrobridge.services.sync import run_sync

runner = CliRunner()

def test_multidisc_domain():
    f1 = "Final Fantasy VII (USA) (Disc 1).chd"
    f2 = "Final Fantasy VII (USA) (Disc 2).chd"

    assert extract_disc_number(f1) == 1
    assert extract_disc_number(f2) == 2
    assert get_base_title("Final Fantasy VII (USA) (Disc 1)") == "Final Fantasy VII (USA)"

    f3 = "Metal Gear Solid 3 - Subsistence (USA) (En,Es) (Disc 2) (Persistence)"
    assert get_disc_family(f3) == "Metal Gear Solid 3 - Subsistence (USA) (En,Es)"
    assert get_disc_search_title(f3) == "Metal Gear Solid 3 - Subsistence"

    m3u = generate_m3u_content([f2, f1])
    assert m3u == f"{f1}\n{f2}\n"

def test_pin_and_unpin(tmp_path: Path):
    db_file = tmp_path / "test_pin.db"
    init_db(str(db_file))

    with get_db_connection(str(db_file)) as conn:
        conn.execute("INSERT INTO games (title, system, state, matched_file_name) VALUES ('Chrono Trigger', 'snes', 'downloading', 'Chrono Trigger (USA).zip');")
        conn.commit()

    assert set_game_pin("Chrono Trigger", "snes", pinned=True, db_path=str(db_file)) is True
    with get_db_connection(str(db_file)) as conn:
        row = conn.execute("SELECT is_pinned FROM games WHERE title = 'Chrono Trigger';").fetchone()
        assert row["is_pinned"] == 1

    assert set_game_pin("Chrono Trigger", "snes", pinned=False, db_path=str(db_file)) is True
    with get_db_connection(str(db_file)) as conn:
        row = conn.execute("SELECT is_pinned FROM games WHERE title = 'Chrono Trigger';").fetchone()
        assert row["is_pinned"] == 0

def test_removal_and_sync_cleanup(tmp_path: Path):
    db_file = tmp_path / "test_rem.db"
    init_db(str(db_file))

    dl_dir = tmp_path / "downloads"
    lib_dir = tmp_path / "library"
    dl_dir.mkdir(parents=True)
    lib_dir.mkdir(parents=True)

    raw_rom = dl_dir / "Castlevania.nes"
    raw_rom.write_text("dummy")

    sdir = lib_dir / "nes"
    sdir.mkdir(parents=True)
    link_rom = sdir / "Castlevania.nes"
    link_rom.write_text("dummy")

    with get_db_connection(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_path, matched_file_name) "
            "VALUES ('Castlevania', 'nes', 'linked', 'Castlevania.nes', 'Castlevania.nes');"
        )
        conn.commit()

    # Deletion request
    actions = request_removal("Castlevania", "nes", db_path=str(db_file))
    assert len(actions) == 1

    with get_db_connection(str(db_file)) as conn:
        g = conn.execute("SELECT state FROM games WHERE title = 'Castlevania';").fetchone()
        assert g["state"] == "removing"

        pa_count = conn.execute("SELECT COUNT(*) as c FROM pending_actions;").fetchone()["c"]
        assert pa_count == 3 # unselect, unlink_library, delete_download

    cfg = Config(paths=PathsConfig(download_dir=dl_dir, library_dir=lib_dir, data_dir=tmp_path))

    # Run sync to process the deletion
    sync_res = run_sync(config=cfg, db_path=str(db_file), skip_scan=True, apply=False)
    assert sync_res["pending_executed"] == 3
    assert not link_rom.exists() # Lien supprimé
    assert not raw_rom.exists()  # Téléchargement supprimé

    # The game must be fully purged from the games table
    with get_db_connection(str(db_file)) as conn:
        assert conn.execute("SELECT COUNT(*) as c FROM games WHERE title = 'Castlevania';").fetchone()["c"] == 0

def test_multidisc_m3u_generation(tmp_path: Path):
    db_file = tmp_path / "test_m3u.db"
    init_db(str(db_file))

    dl_dir = tmp_path / "downloads"
    lib_dir = tmp_path / "library"
    dl_dir.mkdir(parents=True)

    f1 = dl_dir / "Metal Gear Solid (USA) (Disc 1).chd"
    f2 = dl_dir / "Metal Gear Solid (USA) (Disc 2).chd"
    f1.write_text("d1")
    f2.write_text("d2")

    with get_db_connection(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_path, matched_file_name) "
            "VALUES ('MGS Disc 1', 'psx', 'downloaded', ?, ?);",
            (f1.name, f1.name)
        )
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_path, matched_file_name) "
            "VALUES ('MGS Disc 2', 'psx', 'downloaded', ?, ?);",
            (f2.name, f2.name)
        )
        conn.commit()

    cfg = Config(paths=PathsConfig(download_dir=dl_dir, library_dir=lib_dir, data_dir=tmp_path))
    run_sync(config=cfg, db_path=str(db_file), profile_name="esde", skip_scan=True, apply=False)

    m3u_file = lib_dir / "psx" / "Metal Gear Solid (USA).m3u"
    assert m3u_file.exists()
    content = m3u_file.read_text()
    assert "Metal Gear Solid (USA) (Disc 1).chd" in content
    assert "Metal Gear Solid (USA) (Disc 2).chd" in content
