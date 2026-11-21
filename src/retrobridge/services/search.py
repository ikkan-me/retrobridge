import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from retrobridge.config import Config, load_config
from retrobridge.storage.db import get_db_connection
from retrobridge.domain.scoring import normalize_text, score_rom, get_lang_label
from retrobridge.domain.systems import file_matches_system


def _title_match_bonus(file_name: str, norm_query: str) -> int:
    """Pertinence du titre : un jeu exact doit toujours battre un homonyme partiel."""
    stem = Path(file_name).stem
    stem = re.sub(r"[\(\[].*?[\)\]]", " ", stem)
    # "Super Mario Land Rev 1" must stay the same game as "Super Mario Land".
    stem = re.sub(r"\bRev(?:ision)?\s*[0-9A-Z]+\b", " ", stem, flags=re.IGNORECASE)
    # Space-insensitive comparison: "Kuru Kuru Kururin" == "Kurukuru Kururin".
    stem_key = normalize_text(stem).replace(" ", "")
    query_key = norm_query.replace(" ", "")
    if not query_key:
        return 0

    if stem_key == query_key:
        return 3000
    if len(query_key) >= 4 and stem_key.startswith(query_key):
        return 2000
    if len(stem_key) >= 4 and query_key.startswith(stem_key):
        return 1500
    return 0

def search_candidates(
    query: str,
    system: Optional[str] = None,
    config: Optional[Config] = None,
    db_path: Optional[str] = None,
    region_mode: Optional[str] = None,
    pal_language: Optional[str] = None,
    preferred_languages: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    cfg = config or load_config()
    r_mode = region_mode or cfg.scoring.region_preference
    p_lang = pal_language or cfg.scoring.pal_language
    p_langs = preferred_languages or cfg.scoring.preferred_languages

    norm_query = normalize_text(query)
    tokens = norm_query.split()
    if not tokens:
        return []

    candidates = []
    with get_db_connection(db_path) as conn:
        cur = conn.cursor()
        token_clauses = " AND ".join(["file_name_norm LIKE ?"] * len(tokens))
        params = [f"%{token}%" for token in tokens]
        params.append(f"%{norm_query.replace(' ', '')}%")
        cur.execute(
            "SELECT torrent_id, torrent_name, file_index, file_name, file_length "
            "FROM torrent_cache "
            f"WHERE file_name_norm IS NULL OR ({token_clauses}) OR file_name_compact LIKE ?;",
            params,
        )
        rows = cur.fetchall()

        for r in rows:
            fn = r["file_name"]

            # Filter by system when specified
            if system and not file_matches_system(f"{r['torrent_name']}/{fn}", system):
                continue

            norm_fn = normalize_text(fn)
            # All words, or the full query without spaces: tolerates
            # names concatenated/split differently (DuckTales / Duck Tales,
            # ActRaiser / Act Raiser, Bomberman / Bomber Man).
            compact_match = norm_query.replace(" ", "") in norm_fn.replace(" ", "")
            if all(tok in norm_fn for tok in tokens) or compact_match:
                is_arcade = (system and system.lower() in ("arcade", "fbneo", "mame"))
                if is_arcade:
                    score = 1000
                    match_bonus = 0
                else:
                    score = score_rom(
                        fn,
                        region_mode=r_mode,
                        pal_language=p_lang,
                        preferred_languages=p_langs,
                        system=system,
                    )
                    match_bonus = _title_match_bonus(fn, norm_query)

                candidates.append({
                    "torrent_id": r["torrent_id"],
                    "torrent_name": r["torrent_name"],
                    "file_index": r["file_index"],
                    "file_name": fn,
                    "file_length": r["file_length"],
                    "size_mb": round(r["file_length"] / (1024 * 1024), 1),
                    "score": score,
                    "match_bonus": match_bonus,
                    "lang_label": get_lang_label(score, p_langs, system=system)
                })

    # Ranking: score first. The title bonus only breaks ties
    # two files with the same score (e.g. partial namesake vs exact title).
    candidates.sort(key=lambda c: (c["score"], c["match_bonus"], c["file_length"]), reverse=True)
    return candidates
