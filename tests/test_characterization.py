import json
from pathlib import Path
from retrobridge.domain.scoring import score_rom, get_lang_label

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "characterization_matches.json"

def test_scoring_regression_on_real_filenames():
    """Freezes NTSC scoring on real file names from the v1 packs."""
    with open(FIXTURES_PATH, "r", encoding="utf-8") as f:
        matches = json.load(f)

    for item in matches:
        system = item["system"]
        filename = item["matched_file_name"]
        expected_score = item["match_score"]
        expected_lang = item["match_lang_tag"]

        if system == "ARCADE":
            # Arcade ROMs get a forced score of 1000 (FBNeo/MAME romset)
            computed_score = 1000
        else:
            computed_score = score_rom(filename, region_mode="NTSC")

        computed_lang = get_lang_label(computed_score)

        assert computed_score == expected_score, f"Score mismatch for [{system}] {filename}: {computed_score} != {expected_score}"
        assert computed_lang == expected_lang, f"Lang mismatch for [{system}] {filename}: {computed_lang} != {expected_lang}"
