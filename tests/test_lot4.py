import zipfile
from pathlib import Path
from typer.testing import CliRunner

from retrobridge.config import Config, PathsConfig, TargetConfig
from retrobridge.storage.db import init_db, get_db_connection
from retrobridge.domain.tiers import get_tier_systems
from retrobridge.domain.target import read_target_metadata, ensure_target_uuid
from retrobridge.services.search import search_candidates
from retrobridge.services.add import add_game
from retrobridge.services.export import export_to_target

runner = CliRunner()

def test_tiers_cumulative():
    tier1 = get_tier_systems(1)
    tier2 = get_tier_systems(2)
    tier3 = get_tier_systems(3)

    assert "snes" in tier1
    assert "psx" not in tier1
    assert "psx" in tier2
    assert "snes" in tier2
    assert "ps2" in tier3
    assert "psx" in tier3

def test_target_metadata(tmp_path: Path):
    mount = tmp_path / "sdcard"
    u = ensure_target_uuid(mount, configured_uuid="test-uuid-123", name="my_sd", profile="esde")
    assert u == "test-uuid-123"

    meta = read_target_metadata(mount)
    assert meta["uuid"] == "test-uuid-123"
    assert meta["name"] == "my_sd"

def test_search_and_add_game(tmp_path: Path):
    db_file = tmp_path / "test_search.db"
    init_db(str(db_file))

    with get_db_connection(str(db_file)) as conn:
        conn.execute("""
            INSERT INTO torrent_cache (torrent_id, torrent_name, file_index, file_name, file_length, is_wanted, bytes_completed)
            VALUES
            (1, 'PS2 Pack', 0, 'Gran Turismo 4 (USA) (v1.01).zip', 3000000, 0, 0),
            (1, 'PS2 Pack', 1, 'Gran Turismo 4 (USA) (v2.00).zip', 3000000, 0, 0);
        """)
        conn.commit()

    candidates = search_candidates("Gran Turismo 4", system="ps2", db_path=str(db_file))
    assert len(candidates) == 2
    # v2.00 must rank first thanks to the higher revision bonus
    assert "v2.00" in candidates[0]["file_name"]
    assert candidates[0]["score"] > candidates[1]["score"]

    # Test adding the game
    res = add_game(title="Gran Turismo 4", system="ps2", db_path=str(db_file), apply_rpc=False)
    assert res["matched_file"] == candidates[0]["file_name"]

    with get_db_connection(str(db_file)) as conn:
        row = conn.execute("SELECT state, matched_file_name FROM games WHERE title = 'Gran Turismo 4';").fetchone()
        assert row["state"] == "downloading"
        assert "v2.00" in row["matched_file_name"]

def test_incremental_export(tmp_path: Path):
    db_file = tmp_path / "test_exp.db"
    init_db(str(db_file))

    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir(parents=True)
    sd_mount = tmp_path / "sd_card"
    sd_mount.mkdir(parents=True)

    # Create a ROM zip (NES)
    rom_zip = dl_dir / "Super Mario Bros (USA).zip"
    with zipfile.ZipFile(rom_zip, 'w') as zf:
        zf.writestr("Super Mario Bros (USA).nes", b"NES_HEADER_DUMMY_ROM")

    with get_db_connection(str(db_file)) as conn:
        conn.execute("""
            INSERT INTO games (title, system, state, matched_file_path, matched_file_name)
            VALUES ('Super Mario Bros', 'nes', 'downloaded', ?, ?);
        """, (rom_zip.name, rom_zip.name))
        conn.commit()

    cfg = Config(
        paths=PathsConfig(download_dir=dl_dir, library_dir=tmp_path / "lib", data_dir=tmp_path),
        targets={
            "miyoo": TargetConfig(name="miyoo", profile="esde", mount_path=sd_mount, uuid="target-uuid-456")
        }
    )

    # 1. Dry-run test
    res_dry = export_to_target("miyoo", config=cfg, db_path=str(db_file), dry_run=True)
    assert res_dry["exported"] == 1
    assert not (sd_mount / "ROMs" / "nes" / "Super Mario Bros (USA).nes").exists()

    # 2. Real export test (extract rule for NES with the esde profile)
    res_real = export_to_target("miyoo", config=cfg, db_path=str(db_file), dry_run=False)
    assert res_real["exported"] == 1
    dest_file = sd_mount / "ROMs" / "nes" / "Super Mario Bros (USA).nes"
    assert dest_file.exists()
    assert dest_file.read_bytes() == b"NES_HEADER_DUMMY_ROM"

    # 3. Incremental test (re-export must copy nothing)
    res_inc = export_to_target("miyoo", config=cfg, db_path=str(db_file), dry_run=False)
    assert res_inc["exported"] == 0
    assert res_inc["skipped"] == 1

    # 4. Tier filtering test (exclude NES for tier 2 only? no, tiers are cumulative, but non-matching tier)
    # If a PS2 game (Tier 3) is added and we export Tier 1, it must not be exported
    ps2_iso = dl_dir / "Ico.iso"
    ps2_iso.write_bytes(b"DUMMY_ISO")
    with get_db_connection(str(db_file)) as conn:
        conn.execute("""
            INSERT INTO games (title, system, state, matched_file_path, matched_file_name)
            VALUES ('Ico', 'ps2', 'downloaded', ?, ?);
        """, (ps2_iso.name, ps2_iso.name))
        conn.commit()

    res_t1 = export_to_target("miyoo", tier=1, config=cfg, db_path=str(db_file), dry_run=True)
    # Super Mario Bros is already exported (skipped=1); Ico is skipped because PS2 is not in Tier 1
    assert res_t1["exported"] == 0
    assert res_t1["skipped"] == 1
