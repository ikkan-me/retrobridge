from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
import yaml

BUILTIN_PROFILES_DIR = Path(__file__).parent / "builtin"

@dataclass
class ProfileSystem:
    dir: str
    extensions: List[str]
    export_format: str = "copy" # copy, extract, chd, rvz
    target_ext: str = ""
    multidisc_m3u: bool = False

@dataclass
class Profile:
    name: str
    display_name: str
    roms_subpath: str = "ROMs"
    bios_subpath: str = "BIOS"
    systems: Dict[str, ProfileSystem] = field(default_factory=dict)

    def get_system(self, system_name: str) -> Optional[ProfileSystem]:
        return self.systems.get(system_name.lower())

def load_profile(name_or_path: str) -> Profile:
    path = Path(name_or_path)
    if not path.is_file():
        # Try built-in profiles
        builtin_path = BUILTIN_PROFILES_DIR / f"{name_or_path}.yaml"
        if builtin_path.is_file():
            path = builtin_path
        else:
            raise FileNotFoundError(f"Profil introuvable: {name_or_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    systems = {}
    for sys_key, sys_data in data.get("systems", {}).items():
        systems[sys_key.lower()] = ProfileSystem(
            dir=sys_data.get("dir", sys_key),
            extensions=sys_data.get("extensions", []),
            export_format=sys_data.get("export_format", "copy"),
            target_ext=sys_data.get("target_ext", ""),
            multidisc_m3u=sys_data.get("multidisc_m3u", False)
        )

    return Profile(
        name=data.get("name", path.stem),
        display_name=data.get("display_name", data.get("name", path.stem)),
        roms_subpath=data.get("roms_subpath", "ROMs"),
        bios_subpath=data.get("bios_subpath", "BIOS"),
        systems=systems
    )

def list_profiles() -> List[str]:
    profiles = []
    if BUILTIN_PROFILES_DIR.is_dir():
        for p in BUILTIN_PROFILES_DIR.glob("*.yaml"):
            profiles.append(p.stem)
    return sorted(profiles)
