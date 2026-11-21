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

def generate_m3u_content(disc_filenames: List[str]) -> str:
    # Sort by extracted disc number or name
    sorted_files = sorted(
        disc_filenames,
        key=lambda f: (extract_disc_number(f) or 999, f)
    )
    return "\n".join(sorted_files) + "\n"
