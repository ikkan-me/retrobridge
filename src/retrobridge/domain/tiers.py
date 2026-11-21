from typing import Set

TIERS = {
    1: {
        "name": "2D Classics",
        "systems": [
            "nes", "snes", "megadrive", "mastersystem", "gamegear",
            "pcengine", "gb", "gbc", "gba", "arcade"
        ]
    },
    2: {
        "name": "First 3D",
        "systems": [
            "psx", "saturn", "n64"
        ]
    },
    3: {
        "name": "PS2 and later",
        "systems": [
            "dreamcast", "ps2", "gamecube", "wii", "psp", "nds"
        ]
    }
}

def get_tier_systems(max_tier: int) -> Set[str]:
    """Returns the cumulative set of systems up to and including the requested tier."""
    systems: Set[str] = set()
    for t in range(1, max_tier + 1):
        if t in TIERS:
            systems.update(TIERS[t]["systems"])
    return systems
