from typing import Set

TIERS = {
    1: {
        "name": "Classiques 2D",
        "systems": [
            "nes", "snes", "megadrive", "mastersystem", "gamegear",
            "pcengine", "gb", "gbc", "gba", "arcade"
        ]
    },
    2: {
        "name": "Première 3D",
        "systems": [
            "psx", "saturn", "n64"
        ]
    },
    3: {
        "name": "PS2 et suivantes",
        "systems": [
            "dreamcast", "ps2", "gamecube", "wii", "psp", "nds"
        ]
    }
}

def get_tier_systems(max_tier: int) -> Set[str]:
    """Retourne l'ensemble cumulatif des systèmes jusqu'au tier demandé (inclus)."""
    systems: Set[str] = set()
    for t in range(1, max_tier + 1):
        if t in TIERS:
            systems.update(TIERS[t]["systems"])
    return systems
