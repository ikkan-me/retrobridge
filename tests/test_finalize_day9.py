"""Tests de l'ultime lot : verify global, setup destination, export info."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retrobridge.cli import tools as tools_module  # noqa: E402
from retrobridge.cli import user as user_cli  # noqa: E402
from retrobridge.config import Config, PathsConfig  # noqa: E402

runner = CliRunner()


class FakeTransmission:
    def __init__(self, torrents):
        self.torrents = torrents
        self.verified = []

    def request(self, method, arguments=None):
        return {"arguments": {"torrents": self.torrents}}

    def verify_torrent(self, torrent_id):
        self.verified.append(torrent_id)


def _verify_payload():
    return [
        {"id": 1, "name": "Bad", "error": 3, "errorString": "Please Verify Local Data! Piece #4", "percentDone": 0.5},
        {"id": 2, "name": "Good", "error": 0, "errorString": "", "percentDone": 1.0},
    ]


def test_verify_without_argument_targets_corrupt_torrents(monkeypatch):
    fake = FakeTransmission(_verify_payload())
    monkeypatch.setattr(tools_module, "_transmission_client", lambda: fake)

    result = runner.invoke(user_cli.app, ["tools", "verify", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["torrent_id"] == 1
    assert fake.verified == [1]


def test_verify_without_argument_dry_run(monkeypatch):
    fake = FakeTransmission(_verify_payload())
    monkeypatch.setattr(tools_module, "_transmission_client", lambda: fake)

    result = runner.invoke(user_cli.app, ["tools", "verify", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["dry_run"] is True
    assert fake.verified == []


def test_setup_destination_creates_target(tmp_path, monkeypatch):
    cfg = Config(paths=PathsConfig(
        download_dir=tmp_path / "download",
        library_dir=tmp_path / "library",
        data_dir=tmp_path,
    ))
    answers = iter([
        str(cfg.paths.download_dir),
        str(cfg.paths.library_dir),
        "esde",
        str(tmp_path / "sd"),
    ])
    monkeypatch.setattr(user_cli.typer, "prompt", lambda *args, **kwargs: next(answers))

    user_cli._run_setup_step("paths", cfg)

    assert "sd" in cfg.targets
    assert cfg.targets["sd"].profile == "esde"
    assert cfg.targets["sd"].mount_path == tmp_path / "sd"


def test_tools_bios_recommends_retrobios(monkeypatch):
    monkeypatch.setattr(tools_module.shutil, "which", lambda name: "/usr/bin/retrobios" if name.lower() == "retrobios" else None)
    result = runner.invoke(user_cli.app, ["tools", "bios"])
    assert result.exit_code == 0
    assert "/usr/bin/retrobios" in result.output
    assert "RetroBios" in result.output


def test_version_flag():
    result = runner.invoke(user_cli.app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip().startswith("retrobridge ")
