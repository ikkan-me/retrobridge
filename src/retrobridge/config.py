import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Optional, List

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
import tomli_w

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "retrobridge" / "config.toml"
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "retrobridge"

@dataclass
class TransmissionConfig:
    host: str = "127.0.0.1"
    port: int = 9091
    path: str = "/transmission/rpc"
    user: str = ""
    password_env: str = "RETROBRIDGE_RPC_PASSWORD"
    timeout: float = 30.0

    @property
    def rpc_url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"

    @property
    def password(self) -> Optional[str]:
        return os.environ.get(self.password_env)

@dataclass
class TTLConfig:
    scan_ttl: int = 3600
    # `status` triggers a sync if the last check exceeds this delay.
    status_ttl: int = 600

@dataclass
class ScoringConfig:
    region_preference: str = "NTSC" # PAL or NTSC
    pal_language: str = "french"   # legacy field, kept for compatibility
    # Ordered list: the first language is preferred, "en" is always accepted.
    preferred_languages: List[str] = field(default_factory=lambda: ["fr", "en"])

@dataclass
class PathsConfig:
    download_dir: Path = Path("/tmp/retrobridge/downloads")
    library_dir: Path = Path("/tmp/retrobridge/library")
    data_dir: Path = DEFAULT_DATA_DIR

@dataclass
class TargetConfig:
    name: str
    profile: str
    mount_path: Path
    uuid: str = ""

@dataclass
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    transmission: TransmissionConfig = field(default_factory=TransmissionConfig)
    ttl: TTLConfig = field(default_factory=TTLConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    targets: Dict[str, TargetConfig] = field(default_factory=dict)

    def validate_same_filesystem(self) -> bool:
        """Checks that download_dir and library_dir are on the same filesystem for hard links."""
        p_dl = self.paths.download_dir
        p_lib = self.paths.library_dir
        if not p_dl.exists() or not p_lib.exists():
            return False
        return os.stat(p_dl).st_dev == os.stat(p_lib).st_dev

LANGUAGE_CODES = {
    "french": "fr", "français": "fr", "francais": "fr", "fr": "fr",
    "english": "en", "anglais": "en", "en": "en",
    "german": "de", "allemand": "de", "deutsch": "de", "de": "de",
    "spanish": "es", "espagnol": "es", "espanol": "es", "es": "es",
    "italian": "it", "italien": "it", "it": "it",
    "dutch": "nl", "néerlandais": "nl", "neerlandais": "nl", "nl": "nl",
    "portuguese": "pt", "portugais": "pt", "pt": "pt",
    "japanese": "ja", "japonais": "ja", "ja": "ja",
    "russian": "ru", "russe": "ru", "ru": "ru",
}

LANGUAGE_NAMES = {
    "fr": "french", "en": "english", "de": "german", "es": "spanish",
    "it": "italian", "nl": "dutch", "pt": "portuguese", "ja": "japanese",
    "ru": "russian",
}


def language_code(value: Any) -> Optional[str]:
    """Converts 'french', 'fr-FR' or 'FR' into a two-letter code."""
    if value is None:
        return None
    raw = str(value).strip().lower().replace("_", "-")
    if not raw:
        return None
    if raw in LANGUAGE_CODES:
        return LANGUAGE_CODES[raw]
    short = raw.split("-")[0][:2]
    return short if len(short) == 2 and short.isalpha() else None


def normalize_languages(values: Any, fallback: Any = "french") -> List[str]:
    """Ordered list of language codes, without duplicates, with 'en' as a fallback."""
    raw_values = values if isinstance(values, (list, tuple)) else []
    codes: List[str] = []
    for value in raw_values:
        code = language_code(value)
        if code and code not in codes:
            codes.append(code)
    if not codes:
        code = language_code(fallback)
        if code:
            codes.append(code)
    if "en" not in codes:
        codes.append("en")
    return codes


def get_config_path(custom_path: Optional[str] = None) -> Path:
    if custom_path:
        return Path(custom_path)
    env_cfg = os.environ.get("RETROBRIDGE_CONFIG")
    if env_cfg:
        return Path(env_cfg)
    return DEFAULT_CONFIG_PATH

def load_config(config_path: Optional[str] = None) -> Config:
    p = get_config_path(config_path)
    if not p.is_file():
        return Config()

    with open(p, "rb") as f:
        data = tomllib.load(f)

    paths_data = data.get("paths", {})
    paths = PathsConfig(
        download_dir=Path(paths_data.get("download_dir", "/tmp/retrobridge/downloads")),
        library_dir=Path(paths_data.get("library_dir", "/tmp/retrobridge/library")),
        data_dir=Path(paths_data.get("data_dir", str(DEFAULT_DATA_DIR)))
    )

    t_data = data.get("transmission", {})
    transmission = TransmissionConfig(
        host=t_data.get("host", "127.0.0.1"),
        port=t_data.get("port", 9091),
        path=t_data.get("path", "/transmission/rpc"),
        user=t_data.get("user", ""),
        password_env=t_data.get("password_env", "RETROBRIDGE_RPC_PASSWORD"),
        timeout=t_data.get("timeout", 30.0),
    )

    ttl_data = data.get("ttl", {})
    ttl = TTLConfig(
        scan_ttl=ttl_data.get("scan_ttl", 3600),
        status_ttl=ttl_data.get("status_ttl", 600),
    )

    sc_data = data.get("scoring", {})
    legacy_language = sc_data.get("pal_language", "french")
    scoring = ScoringConfig(
        region_preference=sc_data.get("region_preference", "NTSC"),
        pal_language=legacy_language,
        preferred_languages=normalize_languages(
            sc_data.get("preferred_languages"),
            fallback=legacy_language,
        ),
    )

    targets = {}
    for name, t in data.get("targets", {}).items():
        targets[name] = TargetConfig(
            name=name,
            profile=t.get("profile", "esde"),
            mount_path=Path(t.get("mount_path", "")),
            uuid=t.get("uuid", "")
        )

    return Config(paths=paths, transmission=transmission, ttl=ttl, scoring=scoring, targets=targets)

def save_config(config: Config, config_path: Optional[str] = None) -> None:
    p = get_config_path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    data: Dict[str, Any] = {
        "paths": {
            "download_dir": str(config.paths.download_dir),
            "library_dir": str(config.paths.library_dir),
            "data_dir": str(config.paths.data_dir),
        },
        "transmission": {
            "host": config.transmission.host,
            "port": config.transmission.port,
            "path": config.transmission.path,
            "user": config.transmission.user,
            "password_env": config.transmission.password_env,
            "timeout": config.transmission.timeout,
        },
        "ttl": {
            "scan_ttl": config.ttl.scan_ttl,
            "status_ttl": config.ttl.status_ttl,
        },
        "scoring": {
            "region_preference": config.scoring.region_preference,
            "pal_language": config.scoring.pal_language,
            "preferred_languages": list(config.scoring.preferred_languages),
        },
        "targets": {
            name: {
                "profile": t.profile,
                "mount_path": str(t.mount_path),
                "uuid": t.uuid,
            }
            for name, t in config.targets.items()
        }
    }

    with open(p, "wb") as f:
        tomli_w.dump(data, f)
