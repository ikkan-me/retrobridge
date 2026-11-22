import json
from pathlib import Path
from typer.testing import CliRunner

from retrobridge.cli.user import app
from retrobridge.domain.systems import normalize_system, file_matches_system
from retrobridge.services.catalog import list_games
from retrobridge.storage.db import init_db, get_db_connection
from retrobridge.storage.migrations import migrate_db, backup_db
from retrobridge.storage.legacy import import_legacy_db

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


def test_megadrive_and_genesis_are_the_same_system():
    # Every spelling of the alias maps to the canonical code.
    for alias in ("genesis", "GENESIS", "md", "mega-drive", "mega_drive", "sega genesis"):
        assert normalize_system(alias) == "MEGADRIVE"

    # Real Mega Drive layouts still match.
    assert file_matches_system("Minerva_Myrient/RetroAchievements/RA - Sega Genesis/Comix Zone (USA).zip", "megadrive") is True
    assert file_matches_system("Minerva_Myrient/No-Intro/Sega - Mega Drive - Genesis/Sonic (USA).zip", "megadrive") is True
    assert file_matches_system("Minerva_Myrient/TOSEC/Sega/Genesis/Games/Sonic.zip", "megadrive") is True
    assert file_matches_system("Sega Genesis - Sonic.zip", "megadrive") is True

    # "Genesis" is also a common game title: no cross-system false positives.
    assert file_matches_system("Minerva_Myrient/No-Intro/Nintendo - Game Boy Advance/Sonic The Hedgehog - Genesis (USA).zip", "megadrive") is False
    assert file_matches_system("Minerva_Myrient/Redump/Sega - Saturn/Neon Genesis - Evangelion (Japan).zip", "megadrive") is False
    assert file_matches_system("Minerva_Myrient/Redump/Sony - PlayStation 2/Jurassic Park - Operation Genesis (Europe).zip", "megadrive") is False
    assert file_matches_system("Minerva_Myrient/Redump/Sony - PlayStation Portable/Sega Genesis Collection (USA).zip", "megadrive") is False
    assert file_matches_system("Minerva_Myrient/TOSEC/Nintendo/DS/Games/Lunar - Genesis (2005).zip", "megadrive") is False


def test_migrate_infers_and_merges_genesis_into_megadrive(tmp_path: Path):
    db_file = tmp_path / "genesis.db"
    init_db(str(db_file))
    with get_db_connection(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_name) "
            "VALUES ('Comix Zone', 'GENESIS', 'linked', "
            "'Minerva_Myrient/RetroAchievements/RA - Sega Genesis/Comix Zone (USA).zip');"
        )
        # Same game already recorded under the canonical code: must be merged.
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_name) "
            "VALUES ('Altered Beast', 'MEGADRIVE', 'linked', "
            "'Minerva_Myrient/RetroAchievements/RA - Sega Genesis/Altered Beast (USA).zip');"
        )
        conn.commit()

    result = migrate_db(db_path=str(db_file))
    assert result["unresolved"] == []
    assert not [r for r in result["renamed"] if r["to"] != "MEGADRIVE"]

    with get_db_connection(str(db_file)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM games WHERE system = 'GENESIS';").fetchone()[0] == 0
    assert {g["system"] for g in list_games(db_path=str(db_file))} == {"MEGADRIVE"}


def test_system_codes_are_alias_aware(tmp_path: Path):
    from retrobridge.domain.systems import system_codes
    from retrobridge.services.find import find_existing_game

    assert set(system_codes("MEGADRIVE")) == {
        "MEGADRIVE", "GENESIS", "SEGA GENESIS", "MD", "MEGA-DRIVE", "MEGA_DRIVE"
    }

    db_file = tmp_path / "aliases.db"
    init_db(str(db_file))
    with get_db_connection(str(db_file)) as conn:
        # Legacy row stored with the old alias code, before any migration.
        conn.execute(
            "INSERT INTO games (title, system, state, matched_file_name) "
            "VALUES ('Comix Zone', 'GENESIS', 'linked', 'Comix Zone (USA).zip');"
        )
        conn.commit()

    # Reads are canonical: the row is found under either spelling.
    assert [g["title"] for g in list_games(system="megadrive", db_path=str(db_file))] == ["Comix Zone"]
    assert [g["system"] for g in list_games(system="megadrive", db_path=str(db_file))] == ["MEGADRIVE"]
    assert find_existing_game("Comix Zone", "megadrive", db_path=str(db_file)) is not None
    assert find_existing_game("Comix Zone", "genesis", db_path=str(db_file)) is not None


def test_legacy_import_uses_canonical_system_codes(tmp_path: Path):
    import sqlite3

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.executescript("""
        CREATE TABLE games (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, system TEXT NOT NULL,
            download_status TEXT, matched_torrent_id INTEGER, matched_file_index INTEGER,
            matched_file_path TEXT, matched_file_name TEXT, match_score INTEGER DEFAULT 0,
            match_lang_tag TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE torrent_cache (
            torrent_id INTEGER NOT NULL, torrent_name TEXT NOT NULL, file_index INTEGER NOT NULL,
            file_name TEXT NOT NULL, file_length INTEGER DEFAULT 0, is_wanted INTEGER DEFAULT 0,
            bytes_completed INTEGER DEFAULT 0, PRIMARY KEY(torrent_id, file_index)
        );
    """)
    conn.execute(
        "INSERT INTO games (title, system, download_status, matched_file_name) "
        "VALUES ('Sonic', 'GENESIS', 'LINKED', 'Sonic (USA).zip');"
    )
    conn.execute(
        "INSERT INTO games (title, system, download_status, matched_file_name) "
        "VALUES ('Mario', 'GC', 'LINKED', 'Mario (USA).zip');"
    )
    conn.commit()
    conn.close()

    target = tmp_path / "target.db"
    import_legacy_db(legacy_db_path=str(legacy), target_db_path=str(target))
    systems = {g["system"] for g in list_games(db_path=str(target))}
    assert systems == {"MEGADRIVE", "GAMECUBE"}


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
    """A source already in .chd does not go through chdman again."""
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
    """A partial namesake in import/JP must not beat the exact game in PAL."""
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
    """A broken link to the system directory must neither crash sync nor lie about state."""
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

    assert any("Broken link" in e for e in res["errors"])
    with get_db_connection(str(db_file)) as conn:
        row = conn.execute("SELECT state FROM games WHERE title = 'Sonic';").fetchone()
        assert row["state"] == "downloaded"


def _seed_cleanup_db(db_file: Path, dl_dir: Path) -> dict:
    """Creates a DB + files covering every cleanup category."""
    init_db(str(db_file))

    wanted = dl_dir / "Wanted.zip"
    wanted.write_text("wanted")
    complete = dl_dir / "Old PAL.zip"
    complete.write_bytes(b"x" * 100)
    partial = dl_dir / "Partial.zip.part"
    partial.write_bytes(b"y" * 50)
    orphan = dl_dir / "Orphan.zip"
    orphan.write_text("orphan")
    linked = dl_dir / "Linked.zip"
    linked.write_text("linked")
    # Complete on disk while the stale bytes_completed column says 0.
    complete_zero = dl_dir / "ZeroBytesComplete.zip"
    complete_zero.write_bytes(b"z" * 50)

    with get_db_connection(str(db_file)) as conn:
        conn.executemany(
            "INSERT INTO torrent_cache "
            "(torrent_id, torrent_name, file_index, file_name, file_length, bytes_completed, is_wanted) "
            "VALUES (1, 'Pack', ?, ?, ?, ?, ?);",
            [
                (0, "Wanted.zip", 6, 6, 1),
                (1, "Old PAL.zip", 100, 100, 0),
                (2, "Partial.zip", 200, 50, 0),
                (3, "Linked.zip", 6, 6, 0),
                (4, "ZeroBytesComplete.zip", 50, 0, 0),
            ],
        )
        conn.execute(
            "INSERT INTO games "
            "(title, system, state, matched_torrent_id, matched_file_index, matched_file_path, matched_file_name) "
            "VALUES ('Linked Game', 'SNES', 'linked', 1, 3, 'Linked.zip', 'Linked.zip');"
        )
        conn.commit()

    return {
        "wanted": wanted,
        "complete": complete,
        "partial": partial,
        "orphan": orphan,
        "linked": linked,
        "complete_zero": complete_zero,
    }


def test_tools_clean_reports_then_reclaims(tmp_path: Path, monkeypatch):
    from retrobridge.config import Config, PathsConfig, save_config

    db_file = tmp_path / "clean.db"
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir(parents=True)
    files = _seed_cleanup_db(db_file, dl_dir)

    cfg = Config(paths=PathsConfig(download_dir=dl_dir, library_dir=tmp_path / "lib", data_dir=tmp_path))
    cfg_file = tmp_path / "config.toml"
    save_config(cfg, cfg_file)
    monkeypatch.setenv("RETROBRIDGE_CONFIG", str(cfg_file))

    # Default behaviour: state of play only, nothing deleted.
    dry = runner.invoke(app, ["tools", "clean", "--no-refresh", "--db", str(db_file)])
    assert dry.exit_code == 0
    for path in files.values():
        assert path.exists()

    res = runner.invoke(app, ["tools", "clean", "--no-refresh", "--apply", "--db", str(db_file)])
    assert res.exit_code == 0
    assert files["wanted"].exists()      # still wanted in Transmission
    assert files["linked"].exists()      # referenced by a game
    assert not files["complete"].exists()
    assert not files["complete_zero"].exists()
    assert not files["partial"].exists()
    assert not files["orphan"].exists()

    report = runner.invoke(app, ["tools", "clean", "--no-refresh", "--json", "--db", str(db_file)])
    assert report.exit_code == 0
    data = json.loads(report.output)
    assert data["totals"]["reclaimable_bytes"] == 0


def test_tools_clean_partial_only_keeps_complete_files(tmp_path: Path, monkeypatch):
    from retrobridge.config import Config, PathsConfig, save_config

    db_file = tmp_path / "clean_partial.db"
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir(parents=True)
    files = _seed_cleanup_db(db_file, dl_dir)

    cfg = Config(paths=PathsConfig(download_dir=dl_dir, library_dir=tmp_path / "lib", data_dir=tmp_path))
    cfg_file = tmp_path / "config.toml"
    save_config(cfg, cfg_file)
    monkeypatch.setenv("RETROBRIDGE_CONFIG", str(cfg_file))

    res = runner.invoke(
        app, ["tools", "clean", "--no-refresh", "--apply", "--partial-only", "--db", str(db_file)]
    )
    assert res.exit_code == 0
    assert files["complete"].exists()    # kept for seeding
    assert files["complete_zero"].exists()
    assert not files["partial"].exists()
    assert not files["orphan"].exists()


def test_tools_clean_prompt_yes_and_no(tmp_path: Path, monkeypatch):
    from retrobridge.config import Config, PathsConfig, save_config

    db_file = tmp_path / "clean_prompt.db"
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir(parents=True)
    files = _seed_cleanup_db(db_file, dl_dir)

    cfg = Config(paths=PathsConfig(download_dir=dl_dir, library_dir=tmp_path / "lib", data_dir=tmp_path))
    cfg_file = tmp_path / "config.toml"
    save_config(cfg, cfg_file)
    monkeypatch.setenv("RETROBRIDGE_CONFIG", str(cfg_file))

    monkeypatch.setattr("retrobridge.cli.tools._interactive_confirm", lambda message: False)
    no = runner.invoke(app, ["tools", "clean", "--no-refresh", "--db", str(db_file)])
    assert no.exit_code == 0
    assert files["complete"].exists()

    monkeypatch.setattr("retrobridge.cli.tools._interactive_confirm", lambda message: True)
    yes = runner.invoke(app, ["tools", "clean", "--no-refresh", "--db", str(db_file)])
    assert yes.exit_code == 0
    assert not files["complete"].exists()
    assert not files["partial"].exists()


def test_tools_clean_purges_orphan_games(tmp_path: Path, monkeypatch):
    from retrobridge.config import Config, PathsConfig, save_config

    db_file = tmp_path / "clean_games.db"
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir(parents=True)
    init_db(str(db_file))

    with get_db_connection(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_torrent_id, matched_file_index, "
            "matched_file_path, matched_file_name) "
            "VALUES ('Ghost', 'SNES', 'downloaded', 99, 0, 'Ghost.zip', 'Ghost.zip');"
        )
        conn.commit()

    cfg = Config(paths=PathsConfig(download_dir=dl_dir, library_dir=tmp_path / "lib", data_dir=tmp_path))
    cfg_file = tmp_path / "config.toml"
    save_config(cfg, cfg_file)
    monkeypatch.setenv("RETROBRIDGE_CONFIG", str(cfg_file))

    res = runner.invoke(app, ["tools", "clean", "--no-refresh", "--apply", "--db", str(db_file)])
    assert res.exit_code == 0
    with get_db_connection(str(db_file)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM games;").fetchone()["n"] == 0


def test_cleanup_report_refreshes_transmission_first(tmp_path, monkeypatch):
    from retrobridge.config import Config, PathsConfig
    from retrobridge.services import cleanup as cleanup_service

    db_file = tmp_path / "clean_refresh.db"
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir(parents=True)
    init_db(str(db_file))
    cfg = Config(paths=PathsConfig(download_dir=dl_dir, library_dir=tmp_path / "lib", data_dir=tmp_path))

    calls = []

    def fake_scan(**kwargs):
        calls.append(kwargs)
        return {"torrents_count": 15, "changed_torrents": 0}

    monkeypatch.setattr(cleanup_service, "scan_torrents", fake_scan)
    report = cleanup_service.build_cleanup_report(
        config=cfg, db_path=str(db_file), refresh=True
    )
    assert len(calls) == 1
    assert report["refresh"] == {
        "ran": True,
        "torrents": 15,
        "changed": 0,
        "error": None,
    }

    def failing_scan(**kwargs):
        raise RuntimeError("Transmission down")

    monkeypatch.setattr(cleanup_service, "scan_torrents", failing_scan)
    report = cleanup_service.build_cleanup_report(
        config=cfg, db_path=str(db_file), refresh=True
    )
    assert report["refresh"]["ran"] is False
    assert "Transmission down" in report["refresh"]["error"]


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
