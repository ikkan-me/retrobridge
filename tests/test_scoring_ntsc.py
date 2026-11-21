from retrobridge.domain.scoring import score_rom, get_lang_label

def test_ntsc_priority_hierarchy():
    us_fr = score_rom("Chrono Trigger (USA) [T+Fre].zip", region_mode="NTSC")
    us = score_rom("Crash Bandicoot (USA).zip", region_mode="NTSC")
    wld = score_rom("Crash Bandicoot (World).zip", region_mode="NTSC")
    jp = score_rom("Crash Bandicoot (Japan).zip", region_mode="NTSC")
    pal_fr = score_rom("Crash Bandicoot (Europe) (En,Fr).zip", region_mode="NTSC")
    pal = score_rom("Crash Bandicoot (Europe).zip", region_mode="NTSC")

    # 1. USA 60Hz wins over World and Japan, and over the PAL 50Hz fallback
    assert us_fr > us
    assert us > wld
    assert wld > jp
    assert jp > pal_fr
    assert pal_fr > pal

    # 2. Check labels
    assert "US 60Hz [FR]" in get_lang_label(us_fr)
    assert "US 60Hz" in get_lang_label(us)
    assert "PAL [FR]" in get_lang_label(pal_fr)
    assert "PAL" in get_lang_label(pal)

def test_non_french_translation_does_not_beat_original():
    original = score_rom("Contra (USA).zip", region_mode="NTSC")
    spanish = score_rom("Contra (USA)[tr es Star Trans][v1.00].zip", region_mode="NTSC")
    portuguese = score_rom("Crystalis (USA)[tr pt RomHackBR][v1.2].zip", region_mode="NTSC")
    assert original > spanish
    assert original > portuguese


def test_hack_and_amateur_patch_do_not_beat_clean_dump():
    clean = score_rom("Blades of Steel (1988-12)(Konami)(US).zip", region_mode="NTSC")
    hack = score_rom("Blades of Steel (1988-12)(Konami)(US)[h Vimm][iNES title].zip", region_mode="NTSC")
    patch = score_rom("Comix Zone (USA) (Es) (v0.90) (Kale).zip", region_mode="NTSC")
    original = score_rom("Comix Zone (USA).zip", region_mode="NTSC")
    assert clean > hack
    assert original > patch
    assert score_rom("Jeu (USA) (v1.00).zip", region_mode="NTSC") > score_rom("Jeu (USA).zip", region_mode="NTSC")
    assert score_rom("Jeu (USA) (v0.90).zip", region_mode="NTSC") == score_rom("Jeu (USA).zip", region_mode="NTSC")


def test_old_style_translation_penalized():
    original = score_rom("Donkey Kong Country 2 (US).zip", region_mode="NTSC")
    old_patch = score_rom("Donkey Kong Country 2 (US)[t +1 Nightfall].zip", region_mode="NTSC")
    assert original > old_patch


def test_title_bonus_ignores_revision_suffix():
    from retrobridge.services.search import _title_match_bonus
    assert _title_match_bonus("Super Mario Land Rev 1 (1989)(Nintendo).zip", "super mario land") == 3000
    assert _title_match_bonus("Super Mario Land 2 (1992)(Nintendo)(JP).zip", "super mario land") == 2000


def test_french_translation_still_preferred():
    original = score_rom("Contra (USA).zip", region_mode="NTSC")
    french = score_rom("Contra (USA)[tr fr Someone][v1.00].zip", region_mode="NTSC")
    assert french > original


def test_revisions_within_ntsc():
    v1 = score_rom("Gran Turismo 4 (USA) (v1.01).zip", region_mode="NTSC")
    v2 = score_rom("Gran Turismo 4 (USA) (v2.00).zip", region_mode="NTSC")
    assert v2 > v1
