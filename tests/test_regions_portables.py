"""Tests du correctif régions + portables (avant lot C)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


from retrobridge.domain.regions import classify_region, is_portable_system  # noqa: E402
from retrobridge.domain.scoring import get_lang_label, score_rom  # noqa: E402
from retrobridge.services.search import search_candidates  # noqa: E402
from retrobridge.storage.db import get_db_connection, init_db  # noqa: E402


def test_region_classifier_handles_multiregion_and_tosec_combos():
    assert classify_region("Game (USA).zip") == "US"
    assert classify_region("Game (USA, Europe).zip") == "US"
    assert classify_region("Game (USA, Australia).zip") == "US"
    assert classify_region("Game (USA, Canada).zip") == "US"
    assert classify_region("Game (AU-US).zip") == "US"
    assert classify_region("Game (JP-US).zip") == "US"
    assert classify_region("Game (EU-US).zip") == "US"
    assert classify_region("Game (World).zip") == "WORLD"
    assert classify_region("Game (Japan).zip") == "JP"
    assert classify_region("Game (Europe).zip") == "PAL"
    assert classify_region("Game (Europe, Australia).zip") == "PAL"
    assert classify_region("Game (Korea).zip") == "OTHER"
    # Language lists: must never become regions.
    assert classify_region("Game (En,Fr,De).zip") == "UNKNOWN"
    assert classify_region("Game (en-fr).zip") == "UNKNOWN"
    assert classify_region("Game (USA) (En,Fr,De).zip") == "US"
    assert classify_region("Game (Japan) (En,Ja,Fr,De).zip") == "JP"


def test_multiregion_scores_as_us():
    usa = score_rom("Game (USA).zip")
    multi = score_rom("Game (USA, Europe).zip")
    australia = score_rom("Game (USA, Australia).zip")
    combo = score_rom("Game (AU-US).zip")
    japan = score_rom("Game (Japan).zip")
    europe = score_rom("Game (Europe).zip")

    assert usa == multi == australia == combo == 1000
    assert usa > japan > europe


def test_portable_systems_ignore_pal_ntsc():
    assert is_portable_system("GBA")
    assert is_portable_system("PSP")
    assert not is_portable_system("SNES")

    usa = score_rom("Game (USA).zip", system="GBA")
    japan = score_rom("Game (Japan).zip", system="GBA")
    europe = score_rom("Game (Europe).zip", system="GBA")
    assert usa == japan == europe == 1000

    # Language remains the deciding factor, not region.
    fr = score_rom("Game (Europe) (En,Fr).zip", system="GBA")
    assert fr == 1100
    assert fr > usa
    ja_only = score_rom("Game (Japan) (Ja).zip", system="GBA", preferred_languages=["fr"])
    assert ja_only == 900

    assert get_lang_label(1000, ["fr"], system="GBA") == "Portable"
    assert get_lang_label(1100, ["fr"], system="GBA") == "Portable [FR]"


def test_search_ranks_multiregion_us_first(tmp_path):
    db = tmp_path / "rank.db"
    init_db(str(db))
    with get_db_connection(str(db)) as conn:
        conn.execute(
            "INSERT INTO torrent_cache (torrent_id, torrent_name, file_index, file_name, file_length) "
            "VALUES (1, 'SNES Pack', 0, 'Game (Japan).zip', 1000), "
            "       (1, 'SNES Pack', 1, 'Game (USA, Europe).zip', 1000);"
        )
        conn.commit()

    candidates = search_candidates("Game", system="SNES", db_path=str(db))
    assert candidates[0]["file_name"] == "Game (USA, Europe).zip"
    assert candidates[0]["score"] > candidates[1]["score"]


def test_search_portable_prefers_language_not_region(tmp_path):
    db = tmp_path / "rank_portable.db"
    init_db(str(db))
    with get_db_connection(str(db)) as conn:
        conn.execute(
            "INSERT INTO torrent_cache (torrent_id, torrent_name, file_index, file_name, file_length) "
            "VALUES (1, 'GBA Pack', 0, 'Game (USA).zip', 1000), "
            "       (1, 'GBA Pack', 1, 'Game (Europe) (En,Fr).zip', 1000), "
            "       (1, 'GBA Pack', 2, 'Game (Japan).zip', 1000);"
        )
        conn.commit()

    candidates = search_candidates("Game", system="GBA", db_path=str(db))
    assert candidates[0]["file_name"] == "Game (Europe) (En,Fr).zip"
    assert candidates[0]["score"] == 1100
    # USA and Japan are equivalent on handhelds.
    assert candidates[1]["score"] == candidates[2]["score"] == 1000
