import json
from pathlib import Path
from typer.testing import CliRunner

from retrobridge.cli.user import app
from retrobridge.domain.systems import normalize_system, file_matches_system
from retrobridge.services.catalog import list_games
from retrobridge.storage.db import init_db, get_db_connection
from retrobridge.storage.migrations import migrate_db, backup_db

runner = CliRunner()


def test_normalize_system_aliases():
    assert normalize_system("GC") == "GAMECUBE"
    assert normalize_system("gc") == "GAMECUBE"
    assert normalize_system("GENESIS") == "MEGADRIVE"
    assert normalize_system("PS1") == "PSX"
    assert normalize_system("PSX") == "PSX"
    assert normalize_system("SMS") == "MASTERSYSTEM"
    assert normalize_system("GG") == "GAME GEAR".replace(" ", "")
    assert normalize_system("TG-16") == "PCENGINE"
    assert normalize_system("PS2") == "PS2"
    assert normalize_system("snes") == "SNES"


def test_file_matches_system_with_aliases():
    assert file_matches_system("Nintendo - GameCube/x.zip", "GC") is True
    assert file_matches_system("Sony - PlayStation 2/x.zip", "PS1") is False
    assert file_matches_system("Sega Genesis/x.zip", "GENESIS") is True


def test_system_detection_nintendo_packs():
    no_intro_snes = "Minerva_Myrient/No-Intro/Nintendo - Super Nintendo Entertainment System/ActRaiser (Europe).zip"
    no_intro_nes = "Minerva_Myrient/No-Intro/Nintendo - Nintendo Entertainment System/Duck Tales.zip"
    tosec_n64 = "Minerva_Myrient/TOSEC/Nintendo/64/Games/Super Mario 64 (1996)(Nintendo)(US).zip"

    assert file_matches_system(no_intro_snes, "SNES") is True
    assert file_matches_system(no_intro_snes, "NES") is False
    assert file_matches_system(no_intro_nes, "NES") is True
    assert file_matches_system(tosec_n64, "N64") is True


def test_search_tolerates_split_or_joined_words(tmp_path: Path):
    from retrobridge.services.search import search_candidates

    db_file = tmp_path / "search_words.db"
    init_db(str(db_file))
    with get_db_connection(str(db_file)) as conn:
        conn.execute("""
            INSERT INTO torrent_cache (torrent_id, torrent_name, file_index, file_name, file_length)
            VALUES
            (1, 'TOSEC SNES', 0, 'TOSEC/Nintendo/Super Famicom & Super Entertainment System/Games/Act Raiser 2 (1994)(Enix)(EU).zip', 1000),
            (1, 'TOSEC NES', 1, 'TOSEC/Nintendo/Famicom & Entertainment System/Games/[NES]/Duck Tales (1990)(Capcom)(EU).zip', 1000);
        """)
        conn.commit()

    assert search_candidates("ActRaiser 2", "SNES", db_path=str(db_file))[0]["file_name"].endswith("Act Raiser 2 (1994)(Enix)(EU).zip")
    assert search_candidates("DuckTales", "NES", db_path=str(db_file))[0]["file_name"].endswith("Duck Tales (1990)(Capcom)(EU).zip")


def test_migrate_canonicalizes_and_infers(tmp_path: Path):
    db_file = tmp_path / "test_migrate.db"
    init_db(str(db_file))

    with get_db_connection(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_name) "
            "VALUES ('Sonic', 'GENESIS', 'linked', 'Sega Genesis/Sonic.zip');"
        )
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_name) "
            "VALUES ('Soulcalibur', 'GC', 'linked', 'GameCube/Soulcalibur.zip');"
        )
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_name) "
            "VALUES ('Fussball', 'wilden', 'linked', "
            "'No-Intro/Nintendo - Game Boy Advance/Fussball.zip');"
        )
        conn.commit()

    dry = migrate_db(db_path=str(db_file), dry_run=True)
    assert len(dry["renamed"]) == 3
    with get_db_connection(str(db_file)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM games WHERE system = 'GENESIS';"
        ).fetchone()[0] == 1

    res = migrate_db(db_path=str(db_file))
    assert len(res["renamed"]) == 3
    assert res["unresolved"] == []

    assert {g["system"] for g in list_games(db_path=str(db_file))} == {
        "MEGADRIVE", "GAMECUBE", "GBA"
    }

    # Idempotence: a second migration changes nothing
    res_again = migrate_db(db_path=str(db_file))
    assert res_again["renamed"] == []


def test_migrate_cli_json(tmp_path: Path):
    db_file = tmp_path / "test_migrate_cli.db"
    init_db(str(db_file))
    with get_db_connection(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_name) "
            "VALUES ('Sonic', 'GENESIS', 'linked', 'Sega Genesis/Sonic.zip');"
        )
        conn.commit()

    result = runner.invoke(app, ["tools", "db", "migrate", "--json", "--db", str(db_file)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["renamed"][0]["to"] == "MEGADRIVE"


def test_prepare_export_stages_without_target(tmp_path: Path, monkeypatch):
    import zipfile
    from retrobridge.config import Config, PathsConfig
    from retrobridge.services import export as export_module

    db_file = tmp_path / "prepare.db"
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir()
    init_db(str(db_file))

    rom_zip = dl_dir / "Super Mario Bros (USA).zip"
    with zipfile.ZipFile(rom_zip, "w") as zf:
        zf.writestr("Super Mario Bros (USA).nes", b"NES_DUMMY")

    with get_db_connection(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_path, matched_file_name) "
            "VALUES ('Super Mario Bros', 'NES', 'linked', ?, ?);",
            (rom_zip.name, rom_zip.name),
        )
        conn.commit()

    staging = tmp_path / "staging"
    monkeypatch.setattr(export_module, "get_staging_dir", lambda: staging)

    cfg = Config(paths=PathsConfig(download_dir=dl_dir, library_dir=tmp_path / "lib", data_dir=tmp_path))

    dry = export_module.prepare_export(config=cfg, db_path=str(db_file), dry_run=True)
    assert dry["prepared"] == 1
    assert not staging.exists()

    real = export_module.prepare_export(config=cfg, db_path=str(db_file))
    assert real["prepared"] == 1
    assert (staging / "prepare-export" / "nes" / "Super Mario Bros (USA).nes").is_file()


class _FakeTransmission:
    def __init__(self):
        self.calls = []

    def test_connection(self):
        return True

    def set_file_priorities(self, torrent_id, wanted_indices=None, unwanted_indices=None, chunk_size=1000):
        self.calls.append((torrent_id, list(wanted_indices or []), list(unwanted_indices or [])))


def test_apply_selection_only_keeps_game_files(tmp_path: Path):
    from retrobridge.services.apply import apply_selection

    db_file = tmp_path / "apply.db"
    init_db(str(db_file))
    with get_db_connection(str(db_file)) as conn:
        conn.executemany(
            "INSERT INTO torrent_cache (torrent_id, torrent_name, file_index, file_name, file_length, is_wanted) "
            "VALUES (7, 'Pack', ?, ?, 100, 1);",
            [(i, f"rom{i}.zip") for i in range(5)],
        )
        conn.execute(
            "INSERT INTO games (title, system, state, matched_torrent_id, matched_file_index, matched_file_name) "
            "VALUES ('Jeu', 'SNES', 'linked', 7, 2, 'rom2.zip');"
        )
        conn.commit()

    fake = _FakeTransmission()
    res = apply_selection(db_path=str(db_file), client=fake)
    assert res["files_wanted"] == 0
    assert res["files_unwanted"] == 4
    assert fake.calls == [(7, [], [0, 1, 3, 4])]

    with get_db_connection(str(db_file)) as conn:
        wanted = [r["file_index"] for r in conn.execute(
            "SELECT file_index FROM torrent_cache WHERE torrent_id = 7 AND is_wanted = 1;"
        ).fetchall()]
    assert wanted == [2]

    # Idempotence: no more RPC calls on the second run
    fake2 = _FakeTransmission()
    res2 = apply_selection(db_path=str(db_file), client=fake2)
    assert res2["torrents_processed"] == 0
    assert fake2.calls == []


def test_status_auto_sync_when_stale(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "status_refresh.db"
    init_db(str(db_file))
    calls = []

    def fake_sync(**kwargs):
        calls.append(kwargs)
        return {"newly_downloaded": 0, "links_created_or_verified": 0, "errors": []}

    monkeypatch.setattr("retrobridge.cli.user.run_sync", fake_sync)
    monkeypatch.setattr(
        "retrobridge.cli.user.get_system_status",
        lambda db_path=None, **kwargs: {"games": {}, "systems": [], "storage": {}, "transmission": {}},
    )

    # Last check 20 minutes ago → automatic sync
    with get_db_connection(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES ('last_sync', 'completed', datetime('now', '-20 minutes'));"
        )
        conn.commit()

    res = runner.invoke(app, ["status", "--json", "--db", str(db_file)])
    assert res.exit_code == 0
    assert len(calls) == 1
    assert json.loads(res.output)["refresh"]["ran"] is True

    # Recent check → no sync
    calls.clear()
    with get_db_connection(str(db_file)) as conn:
        conn.execute("UPDATE meta SET updated_at = datetime('now') WHERE key = 'last_sync';")
        conn.commit()

    res2 = runner.invoke(app, ["status", "--json", "--db", str(db_file)])
    assert res2.exit_code == 0
    assert calls == []

    # --no-refresh forces no sync even when stale
    with get_db_connection(str(db_file)) as conn:
        conn.execute("UPDATE meta SET updated_at = datetime('now', '-1 hour') WHERE key = 'last_sync';")
        conn.commit()

    res3 = runner.invoke(app, ["status", "--json", "--no-refresh", "--db", str(db_file)])
    assert res3.exit_code == 0
    assert calls == []


def test_apply_selection_dry_run(tmp_path: Path):
    from retrobridge.services.apply import apply_selection

    db_file = tmp_path / "apply_dry.db"
    init_db(str(db_file))
    with get_db_connection(str(db_file)) as conn:
        conn.executemany(
            "INSERT INTO torrent_cache (torrent_id, torrent_name, file_index, file_name, file_length, is_wanted) "
            "VALUES (8, 'Pack', ?, ?, 100, 1);",
            [(i, f"rom{i}.zip") for i in range(3)],
        )
        conn.execute(
            "INSERT INTO games (title, system, state, matched_torrent_id, matched_file_index, matched_file_name) "
            "VALUES ('Jeu', 'SNES', 'linked', 8, 1, 'rom1.zip');"
        )
        conn.commit()

    fake = _FakeTransmission()
    res = apply_selection(db_path=str(db_file), dry_run=True, client=fake)
    assert res["files_unwanted"] == 2
    assert fake.calls == []
    with get_db_connection(str(db_file)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM torrent_cache WHERE torrent_id = 8 AND is_wanted = 1;"
        ).fetchone()[0] == 3


def test_export_copies_already_converted_chd(tmp_path: Path):
    """Une source déjà en .chd ne repasse pas par chdman."""
    from retrobridge.config import Config, PathsConfig, TargetConfig
    from retrobridge.services.export import export_to_target

    db_file = tmp_path / "chd.db"
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir()
    mount = tmp_path / "sdcard"
    mount.mkdir()
    init_db(str(db_file))

    rom = dl_dir / "Crash Bandicoot (Europe).chd"
    rom.write_bytes(b"CHD_DUMMY")
    with get_db_connection(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_path, matched_file_name) "
            "VALUES ('Crash Bandicoot', 'PSX', 'linked', ?, ?);",
            (rom.name, rom.name),
        )
        conn.commit()

    cfg = Config(
        paths=PathsConfig(download_dir=dl_dir, library_dir=tmp_path / "lib", data_dir=tmp_path),
        targets={"sd": TargetConfig(name="sd", profile="esde", mount_path=mount, uuid="uuid-sd")},
    )
    res = export_to_target("sd", config=cfg, db_path=str(db_file))
    assert res["exported"] == 1
    assert (mount / "ROMs" / "psx" / rom.name).read_bytes() == b"CHD_DUMMY"


def test_tools_torrent_actions_dry_run_json():
    for action in ("pause", "resume", "verify"):
        result = runner.invoke(app, ["tools", action, "42", "--dry-run", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert data["torrent_id"] == 42


def test_backup_db(tmp_path: Path):
    db_file = tmp_path / "test_backup.db"
    init_db(str(db_file))
    dest = backup_db(db_path=str(db_file), destination=str(tmp_path / "backups"))
    assert dest.is_file()
    assert dest.stat().st_size > 0
    assert dest.parent == tmp_path / "backups"


def test_search_prefers_exact_title_over_region(tmp_path: Path):
    """Un homonyme partiel en import/JP ne doit pas battre le jeu exact en PAL."""
    from retrobridge.services.search import search_candidates

    db_file = tmp_path / "search_relevance.db"
    init_db(str(db_file))
    with get_db_connection(str(db_file)) as conn:
        conn.execute("""
            INSERT INTO torrent_cache (torrent_id, torrent_name, file_index, file_name, file_length)
            VALUES
            (1, 'GBA Pack', 0, 'Kururin Paradise (Japan).zip', 1000),
            (1, 'GBA Pack', 1, 'Kurukuru Kururin (Europe).zip', 1000);
        """)
        conn.commit()

    candidates = search_candidates("Kuru Kuru Kururin", system="GBA", db_path=str(db_file))
    assert candidates[0]["file_name"] == "Kurukuru Kururin (Europe).zip"
    assert candidates[0]["match_bonus"] > candidates[1]["match_bonus"]


def test_sync_reports_broken_library_dir_without_crashing(tmp_path: Path):
    """Un lien cassé vers le dossier système ne doit ni planter sync ni mentir sur l'état."""
    from retrobridge.config import Config, PathsConfig
    from retrobridge.services.sync import run_sync

    db_file = tmp_path / "test_broken_dir.db"
    init_db(str(db_file))

    dl_dir = tmp_path / "downloads"
    lib_dir = tmp_path / "library"
    (dl_dir / "Sega Genesis").mkdir(parents=True)
    lib_dir.mkdir(parents=True)
    rom = dl_dir / "Sega Genesis" / "Sonic.zip"
    rom.write_text("dummy")

    # Broken link at the canonical system directory location
    (lib_dir / "megadrive").symlink_to(lib_dir / "cible-inexistante")

    with get_db_connection(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_path, matched_file_name) "
            "VALUES ('Sonic', 'MEGADRIVE', 'linked', ?, ?);",
            (str(Path("Sega Genesis") / "Sonic.zip"), "Sonic.zip"),
        )
        conn.commit()

    cfg = Config(paths=PathsConfig(download_dir=dl_dir, library_dir=lib_dir, data_dir=tmp_path))
    res = run_sync(config=cfg, db_path=str(db_file), skip_scan=True, apply=False)

    assert any("Lien cassé" in e for e in res["errors"])
    with get_db_connection(str(db_file)) as conn:
        row = conn.execute("SELECT state FROM games WHERE title = 'Sonic';").fetchone()
        assert row["state"] == "downloaded"


def test_tools_clean_keeps_pack_and_partial_files(tmp_path: Path, monkeypatch):
    from retrobridge.config import Config, PathsConfig, save_config

    db_file = tmp_path / "clean.db"
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir(parents=True)
    init_db(str(db_file))

    pack_file = dl_dir / "Sonic.zip"
    pack_file.write_text("pack")
    partial_file = dl_dir / "Mario.zip.part"
    partial_file.write_text("partial")
    orphan_file = dl_dir / "Inconnu.zip"
    orphan_file.write_text("orphan")

    with get_db_connection(str(db_file)) as conn:
        # Mario is indexed in a pack but not selected yet
        conn.execute(
            "INSERT INTO torrent_cache (torrent_id, torrent_name, file_index, file_name, file_length) "
            "VALUES (1, 'Pack', 0, 'Sonic.zip', 4), (1, 'Pack', 1, 'Mario.zip', 100);"
        )
        conn.commit()

    cfg = Config(paths=PathsConfig(download_dir=dl_dir, library_dir=tmp_path / "lib", data_dir=tmp_path))
    cfg_file = tmp_path / "config.toml"
    save_config(cfg, cfg_file)
    monkeypatch.setenv("RETROBRIDGE_CONFIG", str(cfg_file))

    dry = runner.invoke(app, ["tools", "clean", "--db", str(db_file)])
    assert dry.exit_code == 0
    assert orphan_file.exists()

    res = runner.invoke(app, ["tools", "clean", "--apply", "--db", str(db_file)])
    assert res.exit_code == 0
    assert pack_file.exists()
    assert partial_file.exists()
    assert not orphan_file.exists()


def test_add_game_stores_canonical_system(tmp_path: Path):
    db_file = tmp_path / "test_add_canonical.db"
    init_db(str(db_file))

    from retrobridge.services.add import add_game
    with get_db_connection(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO torrent_cache (torrent_id, torrent_name, file_index, file_name, file_length) "
            "VALUES (1, 'GameCube Pack', 0, 'Soulcalibur II (Europe).zip', 1000);"
        )
        conn.commit()

    res = add_game(title="Soulcalibur II", system="GC", db_path=str(db_file), apply_rpc=False)
    assert res["system"] == "GAMECUBE"
    with get_db_connection(str(db_file)) as conn:
        row = conn.execute(
            "SELECT system FROM games WHERE title = 'Soulcalibur II';"
        ).fetchone()
        assert row["system"] == "GAMECUBE"
