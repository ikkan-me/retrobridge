"""Day 4 tests: languages (B1/B2) and systemd timer (B3/B5/B6/B7)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finalize_harness import make_config, seed_minimal_db  # noqa: E402

from retrobridge.cli import user as user_cli  # noqa: E402
from retrobridge.config import (
    Config,
    ScoringConfig,
    load_config,
    normalize_languages,
    save_config,
)
from retrobridge.domain.scoring import get_lang_label, score_rom  # noqa: E402
from retrobridge.services import status as status_service  # noqa: E402
from retrobridge.services.setup import (  # noqa: E402
    get_timer_status,
    install_timer,
    render_service_unit,
    render_timer_unit,
    uninstall_timer,
)
from retrobridge.storage.lock import LockError  # noqa: E402

runner = CliRunner()


def test_languages_roundtrip_and_legacy_compat(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg = Config(scoring=ScoringConfig(
        region_preference="NTSC",
        pal_language="french",
        preferred_languages=["de", "fr", "en"],
    ))
    save_config(cfg, cfg_file)

    loaded = load_config(cfg_file)
    assert loaded.scoring.preferred_languages == ["de", "fr", "en"]

    # Old file without preferred_languages: derived from pal_language.
    legacy = tmp_path / "legacy.toml"
    legacy.write_text(
        '[scoring]\nregion_preference = "NTSC"\npal_language = "german"\n',
        encoding="utf-8",
    )
    loaded_legacy = load_config(legacy)
    assert loaded_legacy.scoring.preferred_languages == ["de", "en"]

    assert normalize_languages(["French", "EN", "fr"]) == ["fr", "en"]
    assert normalize_languages(None, fallback="spanish") == ["es", "en"]


def test_scoring_uses_preferred_languages():
    french = score_rom("Game (USA) (En,Fr,De).zip", preferred_languages=["fr", "en"])
    plain = score_rom("Game (USA).zip", preferred_languages=["fr", "en"])
    german = score_rom("Game (USA) (De).zip", preferred_languages=["de", "en"])
    german_fr = score_rom("Game (USA) (De).zip", preferred_languages=["fr", "en"])

    assert french > plain
    assert german > german_fr
    assert "[DE]" in get_lang_label(german, ["de", "en"])
    # Historical behavior kept when no explicit preference is set.
    assert "[FR]" in get_lang_label(1100)


def test_timer_unit_rendering(tmp_path):
    service = render_service_unit(
        exec_path="/opt/retrobridge/bin/retrobridge",
        working_directory=tmp_path,
        path_env="/usr/bin:/bin",
    )
    timer = render_timer_unit(10)
    assert 'ExecStart="/opt/retrobridge/bin/retrobridge" sync' in service
    assert f"WorkingDirectory={tmp_path}" in service
    assert "Environment=PATH=/usr/bin:/bin" in service
    assert "EnvironmentFile=-" in service
    assert "OnCalendar=*:0/10" in timer
    assert "RandomizedDelaySec=30s" in timer
    assert "Persistent=true" in timer


def _fake_systemctl(calls):
    def run(args):
        calls.append(args)
        if args[0] == "is-enabled":
            return 0, "enabled"
        if args[0] == "is-active":
            return 0, "active"
        if args[0] == "show":
            return 0, "Sat 2026-11-21 22:10:00 CET"
        return 0, ""
    return run


def test_timer_install_status_uninstall(tmp_path):
    calls = []
    fake = _fake_systemctl(calls)

    result = install_timer(unit_dir=tmp_path, runner=fake, interval_minutes=10)
    assert Path(result["service_path"]).is_file()
    assert Path(result["timer_path"]).is_file()
    assert result["systemctl_ok"] is True
    assert ["daemon-reload"] in calls
    assert ["enable", "--now", "retrobridge-sync.timer"] in calls

    status = get_timer_status(tmp_path, fake)
    assert status["installed"] is True
    assert status["enabled"] is True
    assert status["active"] is True
    assert "22:10" in status["next_run"]

    removed = uninstall_timer(unit_dir=tmp_path, runner=fake)
    assert removed["removed"] is True
    assert not Path(result["service_path"]).exists()
    assert not Path(result["timer_path"]).exists()
    assert get_timer_status(tmp_path, fake)["installed"] is False


def test_timer_dry_run_writes_nothing(tmp_path):
    result = install_timer(unit_dir=tmp_path, dry_run=True)
    assert result["dry_run"] is True
    assert not Path(result["service_path"]).exists()
    assert "ExecStart=" in result["service"]


def test_status_exposes_timer_state(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    db_path = tmp_path / "timer.db"
    seed_minimal_db(db_path, cfg.paths.download_dir)

    monkeypatch.setattr(
        status_service,
        "get_timer_status",
        lambda: {"installed": True, "active": True, "next_run": "demain"},
    )

    result = status_service.get_system_status(config=cfg, db_path=str(db_path), live=False)
    assert "timer" in result
    assert result["timer"] == {"installed": True, "active": True, "next_run": "demain"}
    # A queued target is not a broken target.
    assert result["target_issues"] == 0


def test_sync_lock_is_not_a_failure(monkeypatch, tmp_path):
    db_path = tmp_path / "lock.db"

    def locked(**kwargs):
        raise LockError("Another RetroBridge command is currently running.")

    monkeypatch.setattr(user_cli, "run_sync", locked)
    result = runner.invoke(user_cli.app, ["sync", "--json", "--db", str(db_path)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {"skipped": True, "reason": "locked"}
