"""Classification de région, partagée par le scoring et les bascules.

Objectif : une seule source de vérité pour lire les tags de région des noms
No-Intro / Redump / TOSEC, y compris les listes multi-régions et les combinaisons
TOSEC (`AU-US`, `JP-US`, `EU-US`...).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# Handhelds have no PAL/NTSC standard: the region preference
# must not influence them. Language and revision still matter.
PORTABLE_SYSTEMS = {
    "GB", "GBC", "GBA", "NDS", "PSP", "GAMEGEAR",
    # Prepared for systems that may be added later.
    "LYNX", "NGP", "NGPC", "WONDERSWAN", "POKEMONMINI",
}

FULL_REGIONS = {
    # North America / 60 Hz
    "usa": "US", "united states": "US", "us": "US", "america": "US",
    "north america": "US", "canada": "US", "mexico": "US",
    # Europe / PAL
    "europe": "PAL", "eu": "PAL", "uk": "PAL", "england": "PAL",
    "france": "PAL", "germany": "PAL", "spain": "PAL", "italy": "PAL",
    "netherlands": "PAL", "australia": "PAL", "belgium": "PAL",
    "switzerland": "PAL", "austria": "PAL", "scandinavia": "PAL",
    "sweden": "PAL", "denmark": "PAL", "norway": "PAL", "finland": "PAL",
    "portugal": "PAL", "new zealand": "PAL",
    # Japan / 60 Hz
    "japan": "JP", "jp": "JP",
    # Other
    "asia": "OTHER", "korea": "OTHER", "south korea": "OTHER",
    "china": "OTHER", "taiwan": "OTHER", "hong kong": "OTHER",
    "brazil": "OTHER", "argentina": "OTHER", "russia": "OTHER",
    "poland": "OTHER", "hungary": "OTHER", "czech": "OTHER",
    "greece": "OTHER", "india": "OTHER",
}

UPPER_ABBR = {
    "US": "US", "USA": "US", "EU": "PAL", "UK": "PAL", "JP": "JP",
    "AU": "PAL", "DE": "PAL", "FR": "PAL", "ES": "PAL", "IT": "PAL",
    "NL": "PAL", "PT": "PAL", "SE": "PAL", "DK": "PAL", "NO": "PAL",
    "FI": "PAL", "BE": "PAL", "CH": "PAL", "AT": "PAL", "SC": "PAL",
    "NZ": "PAL", "KR": "OTHER", "BR": "OTHER", "TW": "OTHER",
    "CN": "OTHER", "AS": "OTHER", "RU": "OTHER", "PL": "OTHER",
    "HU": "OTHER", "CZ": "OTHER", "GR": "OTHER", "MX": "OTHER",
    "AR": "OTHER", "IN": "OTHER", "HK": "OTHER",
}

# Two/three-letter languages used in No-Intro lists `(En,Fr,De)`.
LANGUAGE_CODES = {
    "en", "fr", "de", "es", "it", "nl", "pt", "ru", "pl", "ja", "jp",
    "ko", "zh", "sv", "no", "da", "fi", "ca", "el", "tr", "hu", "cs",
    "ro", "sr", "lt", "lv", "et", "uk", "ar", "he", "hi", "th", "vi",
    "id", "ms", "tl", "sw", "af", "bg", "hr", "sk", "sl", "bs", "mk",
    "sq", "hy", "ka", "az", "kk", "uz", "mn", "ne", "si", "ta", "te",
    "ml", "kn", "mr", "gu", "pa", "bn", "ur", "fa", "ku",
}

TAG_RE = re.compile(r'\(([^()]*)\)|\[([^\[\]]*)\]')
SPLIT_RE = re.compile(r'[,+/&]')


def is_portable_system(system: Optional[str]) -> bool:
    if not system:
        return False
    key = str(system).strip().upper()
    # Any aliases (`gg`) are already normalized by the caller.
    return key in PORTABLE_SYSTEMS


def _token_region(token: str) -> Optional[str]:
    raw = token.strip()
    if not raw:
        return None
    low = raw.lower()
    upper = raw.upper()

    # Uppercase TOSEC abbreviations: (US), (EU), (FR), (DE)...
    if raw.isupper() and upper in UPPER_ABBR:
        return UPPER_ABBR[upper]

    if low in FULL_REGIONS:
        # `(us)`, `(eu)`, `(jp)` are regions even in lowercase.
        if low in {"us", "eu", "jp", "usa"}:
            return FULL_REGIONS[low]
        # A lowercase country code in a list is a language: do not
        # turn `(En,Fr,De)` into Germany/France.
        if len(low) <= 3 and low in LANGUAGE_CODES:
            return None
        return FULL_REGIONS[low]

    return UPPER_ABBR.get(upper) if raw.isupper() else None


def _tag_region(tag: str) -> Optional[str]:
    raw = tag.strip()
    if not raw:
        return None
    low = raw.lower()
    if low in {"world", "worldwide"}:
        return "WORLD"

    # TOSEC combinations: (AU-US), (JP-US), (EU-US), (AU-EU-US)...
    lowered = raw.lower()
    if re.fullmatch(r'[a-z]{2}(?:-[a-z]{2})+', lowered):
        parts = lowered.split("-")
        if all(part in LANGUAGE_CODES for part in parts):
            return None
        regions = {_token_region(part.upper()) for part in parts}
        if "US" in regions:
            return "US"
        if "PAL" in regions:
            return "PAL"
        if "JP" in regions:
            return "JP"
        return "OTHER"

    parts = [part.strip() for part in SPLIT_RE.split(raw) if part.strip()]
    if not parts:
        return None

    if len(parts) > 1:
        # Language list: (En,Fr,De), (en,fr), (Es,It)...
        if all(len(part) <= 3 and part.lower() in LANGUAGE_CODES for part in parts):
            return None
        # Region list: (USA, Europe), (USA, Australia), (US, EU)...
        regions = [_token_region(part) for part in parts]
        if any(region is not None for region in regions):
            return _priority(regions)
        return None

    return _token_region(raw)


def _priority(regions: Iterable[Optional[str]]) -> Optional[str]:
    values = {region for region in regions if region}
    if not values:
        return None
    if "US" in values:
        return "US"
    if "WORLD" in values:
        return "WORLD"
    if "JP" in values:
        return "JP"
    if "PAL" in values:
        return "PAL"
    return "OTHER"


def classify_region(filename: str) -> str:
    """Retourne US, WORLD, JP, PAL, OTHER ou UNKNOWN pour un nom de ROM."""
    if not filename:
        return "UNKNOWN"
    found = []
    for paren, bracket in TAG_RE.findall(filename):
        region = _tag_region(paren or bracket)
        if region:
            found.append(region)
    return _priority(found) or "UNKNOWN"


def region_of(filename: str) -> str:
    """Alias historique de `classify_region`."""
    return classify_region(filename)
