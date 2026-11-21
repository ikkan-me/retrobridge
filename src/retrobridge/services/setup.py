import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Any, Optional

from retrobridge.config import Config, load_config
from retrobridge.adapters.transmission import TransmissionClient

DEFAULT_TIMER_MINUTES = 10


def systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def resolve_retrobridge_exec() -> str:
    """Chemin absolu du binaire retrobridge (jamais un chemin de projet codé en dur).

    On privilégie le binaire réellement utilisé pour lancer `setup`, afin que le
    service tourne avec le même interpréteur/venv.
    """
    argv0 = Path(sys.argv[0]).resolve()
    if argv0.name == "retrobridge" and argv0.is_file():
        return str(argv0)
    found = shutil.which("retrobridge")
    if found:
        return str(Path(found).resolve())
    candidate = Path(sys.executable).parent / "retrobridge"
    if candidate.is_file():
        return str(candidate.resolve())
    return "retrobridge"


def _calendar(interval_minutes: int) -> str:
    interval = max(1, int(interval_minutes))
    if interval == 60:
        return "hourly"
    if interval < 60 and 60 % interval == 0:
        return f"*:0/{interval}"
    return f"*:0/{interval}"


def render_service_unit(
    exec_path: Optional[str] = None,
    working_directory: Optional[Path] = None,
    path_env: Optional[str] = None,
) -> str:
    exec_path = exec_path or resolve_retrobridge_exec()
    working_directory = working_directory or Path.home()
    path_env = path_env or os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    config_dir = Path.home() / ".config" / "retrobridge"
    return f"""[Unit]
Description=RetroBridge - synchronisation locale de la collection
Documentation=https://github.com/ikkan-me/retrobridge
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
SyslogIdentifier=retrobridge-sync
ExecStart="{exec_path}" sync
WorkingDirectory={working_directory}
Environment=PATH={path_env}
EnvironmentFile=-{config_dir / 'rpc.env'}
"""


def render_timer_unit(interval_minutes: int = DEFAULT_TIMER_MINUTES) -> str:
    return f"""[Unit]
Description=RetroBridge - timer de synchronisation périodique
Documentation=https://github.com/ikkan-me/retrobridge

[Timer]
OnCalendar={_calendar(interval_minutes)}
AccuracySec=1min
RandomizedDelaySec=30s
Persistent=true
Unit=retrobridge-sync.service

[Install]
WantedBy=timers.target
"""


def _run_systemctl(
    args: list[str],
    runner: Optional[Callable[[list[str]], tuple[int, str]]] = None,
) -> tuple[int, str]:
    if runner is not None:
        return runner(args)
    if not shutil.which("systemctl"):
        return 1, "systemctl introuvable"
    try:
        proc = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        output = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode, output
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def get_timer_status(
    unit_dir: Optional[Path] = None,
    runner: Optional[Callable[[list[str]], tuple[int, str]]] = None,
) -> Dict[str, Any]:
    """État du timer sans installer quoi que ce soit."""
    directory = Path(unit_dir) if unit_dir else systemd_user_dir()
    service = directory / "retrobridge-sync.service"
    timer = directory / "retrobridge-sync.timer"
    installed = service.is_file() and timer.is_file()
    status: Dict[str, Any] = {
        "installed": installed,
        "enabled": False,
        "active": False,
        "next_run": None,
        "unit_dir": str(directory),
        "service_path": str(service),
        "timer_path": str(timer),
    }
    if not installed:
        return status

    enabled_rc, enabled_out = _run_systemctl(["is-enabled", "retrobridge-sync.timer"], runner)
    active_rc, _ = _run_systemctl(["is-active", "retrobridge-sync.timer"], runner)
    next_rc, next_out = _run_systemctl(
        ["show", "-p", "NextElapseUSecRealtime", "--value", "retrobridge-sync.timer"],
        runner,
    )
    status.update({
        "enabled": enabled_rc == 0 and enabled_out.strip() == "enabled",
        "active": active_rc == 0,
        "next_run": (next_out.strip() or None) if next_rc == 0 else None,
    })
    return status


def install_timer(
    interval_minutes: int = DEFAULT_TIMER_MINUTES,
    unit_dir: Optional[Path] = None,
    runner: Optional[Callable[[list[str]], tuple[int, str]]] = None,
    dry_run: bool = False,
    exec_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Écrit les units systemd --user, puis daemon-reload + enable --now."""
    directory = Path(unit_dir) if unit_dir else systemd_user_dir()
    service_path = directory / "retrobridge-sync.service"
    timer_path = directory / "retrobridge-sync.timer"
    service_content = render_service_unit(exec_path=exec_path)
    timer_content = render_timer_unit(interval_minutes)

    result: Dict[str, Any] = {
        "service_path": str(service_path),
        "timer_path": str(timer_path),
        "interval_minutes": interval_minutes,
        "dry_run": dry_run,
        "service": service_content,
        "timer": timer_content,
        "commands": [],
        "systemctl_ok": False,
    }
    if dry_run:
        return result

    directory.mkdir(parents=True, exist_ok=True)
    service_path.write_text(service_content, encoding="utf-8")
    timer_path.write_text(timer_content, encoding="utf-8")

    commands = [
        ["daemon-reload"],
        ["enable", "--now", "retrobridge-sync.timer"],
    ]
    for command in commands:
        rc, out = _run_systemctl(command, runner)
        result["commands"].append({"args": command, "returncode": rc, "output": out})
        result["systemctl_ok"] = result["systemctl_ok"] or rc == 0

    result["status"] = get_timer_status(directory, runner)
    return result


def uninstall_timer(
    unit_dir: Optional[Path] = None,
    runner: Optional[Callable[[list[str]], tuple[int, str]]] = None,
) -> Dict[str, Any]:
    directory = Path(unit_dir) if unit_dir else systemd_user_dir()
    service_path = directory / "retrobridge-sync.service"
    timer_path = directory / "retrobridge-sync.timer"
    commands = []
    if timer_path.is_file() or service_path.is_file():
        rc, out = _run_systemctl(["disable", "--now", "retrobridge-sync.timer"], runner)
        commands.append({"args": ["disable", "--now", "retrobridge-sync.timer"], "returncode": rc, "output": out})
    for path in (timer_path, service_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    rc, out = _run_systemctl(["daemon-reload"], runner)
    commands.append({"args": ["daemon-reload"], "returncode": rc, "output": out})
    return {"removed": True, "commands": commands, "status": get_timer_status(directory, runner)}


def check_environment(config: Optional[Config] = None) -> Dict[str, Any]:
    """Vérifie l'environnement, les dépendances, Transmission et les chemins de stockage."""
    cfg = config or load_config()

    # 1. Python version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 11)

    # 2. Transmission RPC check
    trans_client = TransmissionClient(
        rpc_url=cfg.transmission.rpc_url,
        username=cfg.transmission.user,
        password=cfg.transmission.password,
        timeout=cfg.transmission.timeout,
    )
    trans_ok = trans_client.test_connection()
    trans_error = None
    if not trans_ok:
        trans_error = f"Impossible de joindre le RPC Transmission sur {cfg.transmission.rpc_url}"

    # 3. External tools check
    chdman_path = shutil.which("chdman")
    dolphin_path = shutil.which("dolphin-tool")

    # 4. Storage check
    dl_path = cfg.paths.download_dir
    lib_path = cfg.paths.library_dir
    data_path = cfg.paths.data_dir

    dl_exists = dl_path.exists()
    lib_exists = lib_path.exists()
    data_exists = data_path.exists()

    same_fs = False
    if dl_exists and lib_exists:
        try:
            same_fs = (os.stat(dl_path).st_dev == os.stat(lib_path).st_dev)
        except OSError:
            same_fs = False

    def get_free_gb(path: Path) -> float:
        if path.exists():
            try:
                st = shutil.disk_usage(path)
                return round(st.free / (1024 ** 3), 1)
            except OSError:
                return 0.0
        return 0.0

    report = {
        "python": {
            "version": py_ver,
            "status": "ok" if py_ok else "fail",
            "message": "Python >= 3.11" if py_ok else "Python 3.11+ requis"
        },
        "transmission": {
            "url": cfg.transmission.rpc_url,
            "status": "ok" if trans_ok else "fail",
            "message": "Connecté au RPC Transmission" if trans_ok else trans_error
        },
        "tools": {
            "chdman": {
                "installed": chdman_path is not None,
                "path": chdman_path,
                "status": "ok" if chdman_path else "warn",
                "message": "Installé" if chdman_path else "Optionnel : requis pour compression CHD (mame-tools)"
            },
            "dolphin_tool": {
                "installed": dolphin_path is not None,
                "path": dolphin_path,
                "status": "ok" if dolphin_path else "warn",
                "message": "Installé" if dolphin_path else "Optionnel : requis pour compression RVZ (dolphin-emu)"
            }
        },
        "timer": get_timer_status(),
        "paths": {
            "download_dir": {
                "path": str(dl_path),
                "exists": dl_exists,
                "free_gb": get_free_gb(dl_path),
                "status": "ok" if dl_exists else "warn"
            },
            "library_dir": {
                "path": str(lib_path),
                "exists": lib_exists,
                "free_gb": get_free_gb(lib_path),
                "status": "ok" if lib_exists else "warn"
            },
            "data_dir": {
                "path": str(data_path),
                "exists": data_exists,
                "status": "ok" if data_exists else "info"
            },
            "same_filesystem": {
                "ok": same_fs,
                "status": "ok" if same_fs else "warn",
                "message": "Même système de fichiers (hard links instantanés)" if same_fs else "Systèmes de fichiers différents : les hard links seront impossibles (copies nécessaires)"
            }
        },
        "overall_ok": py_ok and trans_ok and dl_exists and lib_exists and same_fs
    }
    return report

def initialize_directories(config: Config) -> None:
    """Crée les répertoires s'ils n'existent pas encore."""
    config.paths.download_dir.mkdir(parents=True, exist_ok=True)
    config.paths.library_dir.mkdir(parents=True, exist_ok=True)
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
