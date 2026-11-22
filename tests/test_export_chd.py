"""Export tests: disc images (cue + bin tracks) and CLI error reporting."""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from retrobridge.adapters import compress
from retrobridge.adapters.compress import extract_disc_set
from retrobridge.cli import user as user_cli
from retrobridge.config import Config, PathsConfig, TargetConfig
from retrobridge.services import export as export_module
from retrobridge.storage.db import get_db_connection, init_db

runner = CliRunner()


def _make_disc_zip(path: Path, name: str = "Vagrant Story (USA)", tracks: int = 1) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        members = []
        for index in range(1, tracks + 1):
            suffix = f" (Track {index})" if tracks > 1 else ""
            track = f"{name}{suffix}.bin"
            zf.writestr(track, b"BIN" * 100)
            members.append((track, "BINARY"))
        cue = f'FILE "{members[0][0]}" BINARY\n'
        zf.writestr(f"{name}.cue", cue)
        # Noise that must not be extracted.
        zf.writestr("readme.txt", "not a disc image")


def test_extract_disc_set_pulls_cue_and_bin_tracks(tmp_path: Path):
    archive = tmp_path / "game.zip"
    _make_disc_zip(archive, "Castlevania - Symphony of the Night (USA)", tracks=2)
    dest = tmp_path / "out"

    entry = extract_disc_set(archive, dest)

    assert entry.name == "Castlevania - Symphony of the Night (USA).cue"
    assert (dest / "Castlevania - Symphony of the Night (USA) (Track 1).bin").is_file()
    assert (dest / "Castlevania - Symphony of the Night (USA) (Track 2).bin").is_file()
    assert not (dest / "readme.txt").exists()


def test_unpack_rvz_archive_selects_the_rvz(tmp_path: Path):
    from types import SimpleNamespace
    from retrobridge.services.export import _unpack_if_archive

    archive = tmp_path / "gamecube.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Metroid Prime (USA) (Rev 2).rvz", b"RVZ" * 100)
        zf.writestr("readme.txt", "not a disc image")

    staging = tmp_path / "staging"
    staging.mkdir()
    profile = SimpleNamespace(extensions=[".rvz", ".iso", ".gcm"])

    extracted = _unpack_if_archive(archive, staging, profile, [])

    assert extracted.name == "Metroid Prime (USA) (Rev 2).rvz"
    assert extracted.read_bytes() == b"RVZ" * 100


def test_unpack_disc_set_archive_still_extracts_tracks(tmp_path: Path):
    from types import SimpleNamespace
    from retrobridge.services.export import _unpack_if_archive

    archive = tmp_path / "disc.zip"
    _make_disc_zip(archive, "Vagrant Story (USA)", tracks=2)
    staging = tmp_path / "staging"
    staging.mkdir()
    profile = SimpleNamespace(extensions=[".cue", ".bin"])

    extracted = _unpack_if_archive(archive, staging, profile, [])

    assert extracted.name == "Vagrant Story (USA).cue"
    assert len(list(extracted.parent.glob("*.bin"))) == 2


def test_convert_to_chd_times_out_on_malformed_image(tmp_path: Path, monkeypatch):
    """chdman can loop forever on a truncated image: bounded by a timeout."""
    if not shutil.which("chdman"):
        pytest.skip("chdman is not installed")

    monkeypatch.setattr(compress, "CONVERSION_TIMEOUT_S", 2)
    cue = tmp_path / "Broken (USA).cue"
    # A cue with no usable track makes chdman spin without ever finishing.
    cue.write_text('FILE "Broken (USA).bin" BINARY\n')

    with pytest.raises(compress.CompressionError, match="timed out"):
        compress.convert_to_chd(cue, tmp_path / "out.chd")


def _export_fixture(tmp_path: Path) -> tuple[Config, str]:
    """Download-Dir with a cue+bin zip, empty library and SD card."""
    dl_dir = tmp_path / "downloads"
    lib_dir = tmp_path / "library"
    sd = tmp_path / "sd"
    for directory in (dl_dir, lib_dir, sd):
        directory.mkdir(parents=True)

    rel = "Redump/Sony - PlayStation/Vagrant Story (USA).zip"
    archive = dl_dir / rel
    archive.parent.mkdir(parents=True, exist_ok=True)
    _make_disc_zip(archive)

    db_path = tmp_path / "export.db"
    init_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_torrent_id, matched_file_index, "
            "matched_file_path, matched_file_name) VALUES "
            "('Vagrant Story', 'psx', 'linked', 1, 0, ?, ?);",
            (rel, rel),
        )
        game_id = conn.execute("SELECT id FROM games;").fetchone()["id"]
        conn.execute(
            "INSERT INTO game_files (game_id, disc_number, torrent_id, file_index, file_path, file_name, file_length) "
            "VALUES (?, 1, 1, 0, ?, ?, 300);",
            (game_id, rel, rel),
        )
        conn.commit()

    cfg = Config(
        paths=PathsConfig(download_dir=dl_dir, library_dir=lib_dir, data_dir=tmp_path),
        targets={"sd": TargetConfig(name="sd", profile="esde", mount_path=sd, uuid="uuid")},
    )
    return cfg, str(db_path)


def test_export_converts_the_cue_with_its_tracks(tmp_path: Path, monkeypatch):
    cfg, db_path = _export_fixture(tmp_path)
    seen: dict = {}

    def fake_convert_to_chd(source: Path, output: Path, is_dvd: bool = False) -> Path:
        # chdman needs the cue AND its bin tracks in the same directory.
        seen["source"] = source
        seen["bin_exists"] = source.with_suffix(".bin").is_file()
        output.write_bytes(b"CHD")
        return output

    monkeypatch.setattr(export_module, "convert_to_chd", fake_convert_to_chd)

    result = export_module.export_to_target("sd", system="psx", config=cfg, db_path=db_path)

    assert result["errors"] == []
    assert result["exported"] == 1
    assert seen["source"].name == "Vagrant Story (USA).cue"
    assert seen["bin_exists"] is True
    assert (tmp_path / "sd" / "ROMs" / "psx" / "Vagrant Story (USA).chd").is_file()


def _gamecube_fixture(tmp_path: Path) -> tuple[Config, str]:
    dl_dir = tmp_path / "downloads"
    lib_dir = tmp_path / "library"
    sd = tmp_path / "sd"
    for directory in (dl_dir, lib_dir, sd):
        directory.mkdir(parents=True)

    rel = "Redump/Nintendo - GameCube - NKit RVZ/Metroid Prime (USA) (Rev 2).zip"
    archive = dl_dir / rel
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Metroid Prime (USA) (Rev 2).rvz", b"RVZ" * 1000)

    db_path = tmp_path / "gc.db"
    init_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO games (title, system, state, matched_torrent_id, matched_file_index, "
            "matched_file_path, matched_file_name) VALUES "
            "('Metroid Prime', 'gamecube', 'linked', 1, 0, ?, ?);",
            (rel, rel),
        )
        game_id = conn.execute("SELECT id FROM games;").fetchone()["id"]
        conn.execute(
            "INSERT INTO game_files (game_id, disc_number, torrent_id, file_index, file_path, file_name, file_length) "
            "VALUES (?, 1, 1, 0, ?, ?, 3000);",
            (game_id, rel, rel),
        )
        conn.commit()

    cfg = Config(
        paths=PathsConfig(download_dir=dl_dir, library_dir=lib_dir, data_dir=tmp_path),
        targets={"sd": TargetConfig(name="sd", profile="esde", mount_path=sd, uuid="uuid")},
    )
    return cfg, str(db_path)


def test_export_gamecube_rvz_zip_copies_without_reconversion(tmp_path: Path, monkeypatch):
    cfg, db_path = _gamecube_fixture(tmp_path)
    monkeypatch.setattr(export_module.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        export_module, "convert_to_rvz", lambda *args, **kwargs: pytest.fail("RVZ reconverted")
    )
    monkeypatch.setattr(
        export_module, "_unpack_if_archive", lambda *args, **kwargs: pytest.fail("staged")
    )

    result = export_module.export_to_target(
        "sd", system="gamecube", config=cfg, db_path=db_path
    )

    assert result["errors"] == []
    assert result["exported"] == 1
    dest = tmp_path / "sd" / "ROMs" / "gc" / "Metroid Prime (USA) (Rev 2).rvz"
    assert dest.is_file()
    assert dest.read_bytes() == b"RVZ" * 1000


def test_export_cleans_up_staging(tmp_path: Path, monkeypatch):
    cfg, db_path = _export_fixture(tmp_path)
    staging = tmp_path / "staging"
    monkeypatch.setattr(export_module, "get_staging_dir", lambda custom_path=None: _mkdir(staging))
    monkeypatch.setattr(
        export_module,
        "convert_to_chd",
        lambda source, output, is_dvd=False: _write(output),
    )

    export_module.export_to_target("sd", system="psx", config=cfg, db_path=db_path)

    assert list(staging.iterdir()) == []


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path: Path) -> Path:
    path.write_bytes(b"CHD")
    return path


def _ux_fixture(tmp_path: Path) -> tuple[Config, str]:
    """Download-Dir + DB + card with planned, present, unmanaged and waiting."""
    dl_dir = tmp_path / "downloads"
    lib_dir = tmp_path / "library"
    sd = tmp_path / "sd"
    for directory in (dl_dir, lib_dir, sd):
        directory.mkdir(parents=True)

    planned_rel = "No-Intro/Nintendo - Nintendo Entertainment System/Contra (USA).zip"
    present_rel = "No-Intro/Nintendo - Nintendo Entertainment System/Duck Tales (USA).zip"
    waiting_rel = "No-Intro/Sega - Mega Drive - Genesis/Sonic The Hedgehog (USA).zip"
    for rel, inner in ((planned_rel, "Contra (USA).nes"),
                       (present_rel, "Duck Tales (USA).nes"),
                       (waiting_rel, "Sonic The Hedgehog (USA).md")):
        archive = dl_dir / rel
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(inner, b"ROM" * 1000)

    nes_dir = sd / "ROMs" / "nes"
    nes_dir.mkdir(parents=True)
    # Already on the card, expected name (extract -> .nes): accepted by name.
    (nes_dir / "Duck Tales (USA).nes").write_bytes(b"ROM" * 1000)
    # Metadata that must be ignored.
    (nes_dir / "systeminfo.txt").write_bytes(b"ignored")
    (nes_dir / "gamelist.xml").write_bytes(b"ignored")
    # Unmanaged: matches no game of the collection.
    (nes_dir / "Zelda II (Europe).nes").write_bytes(b"x" * 4096)

    db_path = tmp_path / "ux.db"
    init_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        for title, system, rel, state in (
            ("Contra", "nes", planned_rel, "downloaded"),
            ("Duck Tales", "nes", present_rel, "downloaded"),
            ("Sonic The Hedgehog", "megadrive", waiting_rel, "downloading"),
        ):
            conn.execute(
                "INSERT INTO games (title, system, state, matched_file_path, matched_file_name) "
                "VALUES (?, ?, ?, ?, ?);",
                (title, system, state, rel, rel),
            )
            game_id = conn.execute("SELECT id FROM games WHERE title = ?;", (title,)).fetchone()["id"]
            conn.execute(
                "INSERT INTO game_files (game_id, disc_number, file_path, file_name, file_length) "
                "VALUES (?, 1, ?, ?, 1000);",
                (game_id, rel, rel),
            )
        conn.commit()

    cfg = Config(
        paths=PathsConfig(download_dir=dl_dir, library_dir=lib_dir, data_dir=tmp_path),
        targets={"sd": TargetConfig(name="sd", profile="esde", mount_path=sd, uuid="uuid")},
    )
    return cfg, str(db_path)


def test_export_plan_reconciles_present_unmanaged_and_waiting(tmp_path: Path):
    cfg, db_path = _ux_fixture(tmp_path)

    res = export_module.export_to_target("sd", config=cfg, db_path=db_path, dry_run=True)

    counts = res["counts"]
    assert counts["planned_games"] == 1
    assert counts["present_games"] == 1
    assert counts["unmanaged_files"] == 1
    assert counts["waiting_games"] == 1
    assert [g["title"] for g in res["planned"]] == ["Contra"]
    assert [g["title"] for g in res["present"]] == ["Duck Tales"]
    # Converted/extracted file with no record: present, but flagged unverified.
    assert res["present"][0]["verified"] is False
    assert [u["name"] for u in res["unmanaged"]] == ["Zelda II (Europe).nes"]
    assert res["destination_only"] == res["unmanaged"]
    assert [g["title"] for g in res["waiting"]] == ["Sonic The Hedgehog"]


def test_export_ignores_metadata_files_on_the_card(tmp_path: Path):
    cfg, db_path = _ux_fixture(tmp_path)

    res = export_module.export_to_target("sd", config=cfg, db_path=db_path, dry_run=True)

    names = {item["name"] for item in res["unmanaged"]}
    assert "systeminfo.txt" not in names
    assert "gamelist.xml" not in names


def test_export_emits_progress_events_during_conversion(tmp_path: Path, monkeypatch):
    cfg, db_path = _export_fixture(tmp_path)
    events: list[dict] = []
    monkeypatch.setattr(
        export_module,
        "convert_to_chd",
        lambda source, output, is_dvd=False: _write(output),
    )

    res = export_module.export_to_target(
        "sd", system="psx", config=cfg, db_path=db_path, progress=events.append
    )

    kinds = [event["event"] for event in events]
    assert kinds[0] == "plan"
    assert kinds[-1] == "done"
    assert "game_start" in kinds and "game_done" in kinds
    phases = [event["phase"] for event in events if event["event"] == "phase"]
    assert "convert" in phases and "copy" in phases
    assert res["exported"] == 1


def test_export_cli_renders_the_plan_like_status(tmp_path: Path, monkeypatch):
    cfg, db_path = _ux_fixture(tmp_path)
    real_export = export_module.export_to_target
    fixed_db = db_path

    def wrapped(target_name, system=None, tier=None, raw=False, dry_run=False, db_path=None):
        return real_export(
            target_name=target_name, system=system, tier=tier,
            raw=raw, dry_run=dry_run, config=cfg, db_path=fixed_db,
        )

    monkeypatch.setattr(user_cli, "export_to_target", wrapped)

    result = runner.invoke(user_cli.app, ["export", "sd", "--dry-run"])

    assert result.exit_code == 0, result.stdout
    assert "Export Plan" in result.stdout
    assert "Downloaded 2 − present 1 = 1 to copy" in result.stdout
    for heading in ("Planned —", "Present —", "Unmanaged on card —", "Waiting —"):
        assert heading in result.stdout
    assert "Zelda II (Europe).nes" in result.stdout
    # Full lists: no truncation marker.
    assert "… and" not in result.stdout


def test_export_cli_reports_bracketed_errors_without_markup_crash(monkeypatch):
    def fake_export(**kwargs):
        return {
            "target": "sd",
            "profile": "esde",
            "uuid": "dc519894-0343-4597-acdf-f520c7de9822",
            "dry_run": False,
            "exported": 1,
            "skipped": 0,
            "destination_only": [
                {"system": "PSX", "file": "/run/media/x/ROMs/psx/Weird [Hack] Game.chd", "size_bytes": 1}
            ],
            "errors": [
                "Error exporting Vagrant Story: chdman failed [/tmp/retrobridge/staging/Vagrant Story (USA).bin]"
            ],
        }

    monkeypatch.setattr(user_cli, "export_to_target", fake_export)

    result = runner.invoke(user_cli.app, ["export", "sd"])

    assert result.exit_code == 0, result.stdout
    assert "Errors encountered" in result.stdout
    assert "/tmp/retrobridge/staging/Vagrant Story (USA).bin" in result.stdout
    assert "Weird [Hack] Game.chd" in result.stdout
