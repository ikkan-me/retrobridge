import json
from typer.testing import CliRunner

from retrobridge.cli.user import app
from retrobridge.config import Config, PathsConfig, TransmissionConfig, ScoringConfig
from retrobridge.services.setup import check_environment, initialize_directories

runner = CliRunner()

def test_check_environment(tmp_path):
    dl_dir = tmp_path / "dl"
    lib_dir = tmp_path / "lib"
    dl_dir.mkdir()
    lib_dir.mkdir()

    cfg = Config(
        paths=PathsConfig(download_dir=dl_dir, library_dir=lib_dir, data_dir=tmp_path / "data"),
        transmission=TransmissionConfig(host="127.0.0.1", port=65530), # port unassigned
        scoring=ScoringConfig()
    )

    report = check_environment(cfg)
    assert report["python"]["status"] == "ok"
    assert "chdman" in report["tools"]
    assert "dolphin_tool" in report["tools"]
    assert report["paths"]["download_dir"]["exists"] is True
    assert report["paths"]["library_dir"]["exists"] is True
    assert report["paths"]["same_filesystem"]["ok"] is True

def test_initialize_directories(tmp_path):
    cfg = Config(
        paths=PathsConfig(
            download_dir=tmp_path / "sub_dl",
            library_dir=tmp_path / "sub_lib",
            data_dir=tmp_path / "sub_data"
        )
    )
    assert not cfg.paths.download_dir.exists()
    initialize_directories(cfg)
    assert cfg.paths.download_dir.exists()
    assert cfg.paths.library_dir.exists()
    assert cfg.paths.data_dir.exists()

def test_cli_setup_check():
    result = runner.invoke(app, ["setup", "--check", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "python" in data
    assert "paths" in data
