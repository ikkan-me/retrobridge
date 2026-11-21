from typing import Dict, List, Optional

# Canonical codes: these are the keys used by profiles (esde/knulli),
# tiers and the Library-Dir tree.
SYSTEM_KEYWORDS: Dict[str, List[str]] = {
    "snes": ["super famicom", "super entertainment system", "snes", "super nintendo entertainment system"],
    "nes": ["famicom & entertainment system", "[nes]", "nintendo entertainment system"],
    "gba": ["game boy advance", "gba"],
    "gb": ["game boy/", "[gb]"],
    "gbc": ["game boy color", "[gbc]"],
    "megadrive": ["sega genesis", "mega drive", "genesis"],
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
    "md": "megadrive",
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
    """Retourne le code système canonique, en majuscules (ex: 'GC' -> 'GAMECUBE')."""
    key = (system or "").strip().lower()
    if not key:
        return ""
    return SYSTEM_ALIASES.get(key, key).upper()


def file_matches_system(file_path: str, system: Optional[str]) -> bool:
    if not system:
        return True
    sys_k = normalize_system(system).lower()
    keywords = SYSTEM_KEYWORDS.get(sys_k)
    if not keywords:
        return sys_k in file_path.lower()

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
