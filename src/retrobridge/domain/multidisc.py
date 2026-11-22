import re
from typing import List, Optional

DISC_PATTERN = re.compile(r"[\(\[\s](?:Disc|CD|Disk)\s*([0-9]+)[\)\]\s]?", re.IGNORECASE)

def extract_disc_number(filename: str) -> Optional[int]:
    match = DISC_PATTERN.search(filename)
    if match:
        return int(match.group(1))
    return None

def get_base_title(filename: str) -> str:
    cleaned = DISC_PATTERN.sub("", filename)
    return re.sub(r"\s+", " ", cleaned).strip()


def get_disc_family(filename: str) -> str:
    """Release name before the disc marker.

    Disc subtitles are common in Redump sets:
    `Metal Gear Solid 3 - Subsistence (USA) (En,Es) (Disc 1) (Subsistence)`
    and `... (Disc 2) (Persistence)` share the same family even though
    `get_base_title` leaves a different trailing subtitle on each disc.
    """
    match = DISC_PATTERN.search(filename)
    if not match:
        return re.sub(r"\s+", " ", filename).strip()
    prefix = filename[:match.start()]
    return re.sub(r"\s+", " ", prefix).strip()


def get_disc_search_title(filename: str) -> str:
    """Main title used to query the torrent cache for the other discs.

    Region, language and disc-specific groups are removed so that every
    disc of the same release can be found by a single search.
    """
    family = get_disc_family(filename)
    title = re.sub(r"[\(\[]\s*[^\)\]]*[\)\]]", " ", family)
    title = re.sub(r"\s+", " ", title).strip(" -_")
    return title or family


def generate_m3u_content(disc_filenames: List[str]) -> str:
    # Sort by extracted disc number or name
    sorted_files = sorted(
        disc_filenames,
        key=lambda f: (extract_disc_number(f) or 999, f)
    )
    return "\n".join(sorted_files) + "\n"
