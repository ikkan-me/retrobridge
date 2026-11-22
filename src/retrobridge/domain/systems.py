from typing import Dict, List, Optional

# Canonical codes: these are the keys used by profiles (esde/knulli),
# tiers and the Library-Dir tree.
SYSTEM_KEYWORDS: Dict[str, List[str]] = {
    "snes": ["super famicom", "super entertainment system", "snes", "super nintendo entertainment system"],
    "nes": ["famicom & entertainment system", "[nes]", "nintendo entertainment system"],
    "gba": ["game boy advance", "gba"],
    "gb": ["game boy/", "[gb]"],
    "gbc": ["game boy color", "[gbc]"],
    "megadrive": ["sega genesis", "sega - genesis", "mega drive", "megadrive"],
    "mastersystem": ["master system", "mark iii", "sms"],
    "gamegear": ["game gear", "gg"],
    "pcengine": ["pc engine", "turbografx"],
    "n64": ["nintendo 64", "n64", "nintendo/64"],
    "nds": ["nintendo ds", "nds"],
    "arcade": ["arcade", "fbneo", "mame"],
    "psx": ["sony - playstation/", "playstation 1", "ps1", "psx"],
    "saturn": ["sega - saturn", "saturn"],
    "dreamcast": ["sega - dreamcast", "dreamcast"],
    "ps2": ["playstation 2", "ps2"],
    "psp": ["playstation portable", "psp"],
    "gamecube": ["gamecube", "gcn"],
    "wii": ["nintendo - wii", "wii"],
}

# Legacy aliases (old v1 database) to canonical code.
# Every new DB write uses the canonical form.
SYSTEM_ALIASES: Dict[str, str] = {
    "genesis": "megadrive",
    "sega genesis": "megadrive",
    "md": "megadrive",
    "mega-drive": "megadrive",
    "mega_drive": "megadrive",
    "sms": "mastersystem",
    "gg": "gamegear",
    "tg16": "pcengine",
    "tg-16": "pcengine",
    "pce": "pcengine",
    "pc-engine": "pcengine",
    "turbografx": "pcengine",
    "turbografx16": "pcengine",
    "gc": "gamecube",
    "ngc": "gamecube",
    "ps1": "psx",
    "psone": "psx",
    "playstation": "psx",
}


def normalize_system(system: str) -> str:
    """Returns the canonical system code, in uppercase (e.g. 'GC' -> 'GAMECUBE')."""
    key = (system or "").strip().lower()
    if not key:
        return ""
    return SYSTEM_ALIASES.get(key, key).upper()


def system_codes(system: str) -> List[str]:
    """Every stored spelling that refers to the same canonical system.

    `genesis` and `megadrive` are the same console: querying one must find
    rows stored under the other, even before `tools db migrate` runs.
    """
    canonical = normalize_system(system)
    codes = {canonical}
    for alias, target in SYSTEM_ALIASES.items():
        if target.upper() == canonical:
            codes.add(alias.upper())
    return sorted(codes)


MEGADRIVE_DIR_HINTS = ("mega drive", "megadrive", "mega-drive")


def _matches_megadrive(file_path: str) -> bool:
    """Genesis is also a common game title: match it only in platform paths."""
    parts = str(file_path).replace("\\", "/").split("/")
    directories = [part.lower() for part in parts[:-1]]
    for directory in directories:
        if any(hint in directory for hint in MEGADRIVE_DIR_HINTS):
            return True
        if "genesis" in directory and ("sega" in directory or directory.strip() == "genesis"):
            return True

    if len(parts) > 1:
        # Inside a pack tree the platform comes from the directory only:
        # `.../Sega - PlayStation Portable/Sega Genesis Collection (USA).zip`
        # is a PSP game, not a Mega Drive one.
        return False

    # Flat layouts such as `Sega Genesis - Sonic.zip`.
    name = parts[-1].lower() if parts else ""
    return name.startswith(("sega genesis", "sega - genesis", "mega drive", "megadrive", "mega-drive"))


def file_matches_system(file_path: str, system: Optional[str]) -> bool:
    if not system:
        return True
    sys_k = normalize_system(system).lower()
    keywords = SYSTEM_KEYWORDS.get(sys_k)
    if not keywords:
        return sys_k in file_path.lower()

    if sys_k == "megadrive":
        return _matches_megadrive(file_path)

    fp_lower = file_path.lower()
    # PS1 special case to avoid matching PS2 or PSP
    if sys_k == "psx":
        if "playstation 2" in fp_lower or "playstation portable" in fp_lower:
            return False
        return any(kw in fp_lower for kw in keywords)

    # "Nintendo Entertainment System" is also contained in
    # "Super Nintendo Entertainment System": do not confuse NES and SNES.
    if sys_k == "nes" and "super nintendo" in fp_lower:
        return False

    return any(kw in fp_lower for kw in keywords)
