import re
import unicodedata
from typing import Dict, Any, Optional, List

from retrobridge.config import language_code
from retrobridge.domain.regions import classify_region, is_portable_system

DEFAULT_SCORING = {
    "bad_tags_penalty": -9999,
    "hack_penalty": -200,
    "good_dump_bonus": 50,
    "revision_bonus": 20
}

PAL_REGION_TAGS = [
    '(europe)', '(euro)', '(eu)', '(france)', '(fr)', '(germany)', '(de)',
    '(spain)', '(es)', '(italy)', '(it)', '(uk)', '(australia)', '(au)',
    '(eu-us)', '(us-eu)', '(pal)', '[pal]'
]

FRENCH_TAGS = [
    '(france)', '(fr)', '(en,fr', ',fr,', ',fr)', '(fr,'
]

# Languages explicitly present in No-Intro/Redump/TOSEC names.
# English is implicit: it is not used for the language bonus.
LANGUAGE_PATTERNS = {
    "fr": FRENCH_TAGS + ["[t+fre", "(traduction fr"],
    "de": ["(germany)", "(de)", ",de,", "(de,", ",de)", "(de-"],
    "es": ["(spain)", "(es)", ",es,", "(es,", ",es)", "(es-"],
    "it": ["(italy)", "(it)", ",it,", "(it,", ",it)", "(it-"],
    "nl": ["(netherlands)", "(nl)", ",nl,", "(nl,", ",nl)", "(nl-"],
    "pt": ["(portugal)", "(pt)", ",pt,", "(pt,", ",pt)", "(pt-"],
    "ja": ["(japan)", "(jp)", "(ja)", ",ja,", "(ja,", ",ja)"],
    "ru": ["(russia)", "(ru)", ",ru,", "(ru,", ",ru)", "(ru-"],
    "en": ["(usa)", "(us)", ",en,", "(en,", ",en)", "(en-"],
}

TRANSLATION_LANGUAGE_CODES = {
    "fre": "fr", "fra": "fr", "eng": "en", "spa": "es", "ger": "de",
    "deu": "de", "ita": "it", "dut": "nl", "nld": "nl", "por": "pt",
    "rus": "ru", "jpn": "ja",
}


def _resolve_languages(
    preferred_languages: Optional[List[str]],
    pal_language: Optional[str],
) -> List[str]:
    codes: List[str] = []
    for value in preferred_languages or []:
        code = language_code(value)
        if code and code not in codes:
            codes.append(code)
    if not codes:
        code = language_code(pal_language)
        if code:
            codes.append(code)
    if "en" not in codes:
        codes.append("en")
    return codes


def _has_language(fn_lower: str, code: str) -> bool:
    patterns = LANGUAGE_PATTERNS.get(code, [])
    return any(pattern in fn_lower for pattern in patterns)

# Fan translations: [tr es], [tr pt], [t+fre], (traduction fr)...
TRANSLATION_RE = re.compile(r'\[(?:tr|t\+)\s*([a-z]{2,3})')
TRADUCTION_RE = re.compile(r'\(traduction\s+([a-z]{2,3})')

def normalize_text(text: str) -> str:
    text = text.replace("’", "'").replace("`", "'")
    text = unicodedata.normalize('NFKD', text).encode('ASCII', 'ignore').decode('utf-8')
    text = text.lower()
    text = re.sub(r'[\':!?,.\-_]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def extract_revision_score(filename: str, base_bonus: int = 20) -> int:
    fn_lower = filename.lower()
    bonus = 0

    v_match = re.search(r'[\(\[\s]v([0-9]+)(?:\.([0-9]+))?[\)\]\s]', fn_lower)
    if v_match:
        major = int(v_match.group(1))
        # v0.x = amateur/beta patch: no revision bonus.
        if 1 <= major <= 10:
            minor = int(v_match.group(2)) if v_match.group(2) else 0
            bonus = max(bonus, base_bonus + min(major - 1, 5) * 2 + (1 if minor > 0 else 0))

    rev_match = re.search(r'[\(\[\s]rev(?:[\s\.\-]+)([0-9]+|[a-z])[\)\]\s]', fn_lower)
    if rev_match:
        val = rev_match.group(1)
        if val.isdigit():
            r_num = int(val)
            bonus = max(bonus, base_bonus + (r_num - 1))
        elif len(val) == 1 and val.isalpha():
            r_offset = ord(val) - ord('a')
            bonus = max(bonus, base_bonus + r_offset)
        else:
            bonus = max(bonus, base_bonus)

    if bonus == 0 and any(rev in fn_lower for rev in ['rev 1', 'rev a', 'rev prg1', 'v1.1', 'v1.2']):
        bonus = base_bonus

    return bonus

def is_pal_rom(fn_lower: str) -> bool:
    if any(tag in fn_lower for tag in PAL_REGION_TAGS):
        return True
    if any(tag in fn_lower for tag in FRENCH_TAGS):
        return True
    return False

def score_rom(
    filename: str,
    scoring_config: Optional[Dict[str, Any]] = None,
    region_mode: str = "NTSC", # NTSC (60Hz default) or PAL
    pal_language: str = "french",
    preferred_languages: Optional[List[str]] = None,
    system: Optional[str] = None,
) -> int:
    fn_lower = filename.lower()
    scoring = scoring_config or DEFAULT_SCORING
    bad_penalty = scoring.get("bad_tags_penalty", -9999)
    languages = _resolve_languages(preferred_languages, pal_language)
    allowed_languages = set(languages)
    has_preferred_language = any(
        code != "en" and _has_language(fn_lower, code) for code in languages
    )
    portable = is_portable_system(system)
    region = classify_region(filename)

    bad_tags = [
        '(beta)', '(proto)', '(prototype)', '(demo)', '(sample)', '(kiosk)',
        '(unl)', 'action replay', 'cheat code', '(bonus disc)', '(preview disc)',
        # Japanese/English equivalents of trial versions
        'taikenban', '(trial)', '(体験版)',
    ]
    for bt in bad_tags:
        if bt in fn_lower:
            return bad_penalty
    # GoodTools: [b]ad dump, [o]verdump, [p]irate, [t]ranslation (no language)
    if re.search(r'\[(?:b|o|p|t)\d*\]', fn_lower):
        return bad_penalty

    score = 0

    if portable:
        # Handheld console: PAL/NTSC does not exist. Neutral base, only the
        # language criterion (and the penalties below) breaks the tie.
        score = 1000 + (100 if has_preferred_language else 0)
    elif region_mode == "PAL":
        if region == "PAL":
            score += 1000 if has_preferred_language else 800
        elif region == "US":
            score += 300
        elif region == "WORLD":
            score += 200
        elif region == "JP":
            score += 50

    else:
        # NTSC 60Hz priority (USA > World > Japan > PAL fallback).
        if region == "US":
            score += 1100 if has_preferred_language else 1000
        elif region == "WORLD":
            score += 850 if has_preferred_language else 800
        elif region == "JP":
            score += 700 if has_preferred_language else 600
        elif region == "PAL":
            # PAL 50Hz fallback when no NTSC version is found.
            score += 500 if has_preferred_language else 400
        else:
            # Unknown region (Korea, Brazil, missing tag...).
            score += 200

    if re.search(r'\[h(?:\d|\])', fn_lower) or re.search(r'\[h\s[^\]]*\]', fn_lower) \
            or '(rus)' in fn_lower or '(ru)' in fn_lower:
        score += scoring.get("hack_penalty", -200)
    if re.search(r'\[a\d*\]', fn_lower):
        score -= 50
    if '[!]' in fn_lower:
        score += scoring.get("good_dump_bonus", 50)

    rev_bonus = extract_revision_score(filename, scoring.get("revision_bonus", 20))
    score += rev_bonus

    if '(virtual console)' in fn_lower or 'virtual console' in fn_lower:
        score -= 30
    if 'advance collection' in fn_lower:
        score -= 30

    # A translation in an unwanted language must not beat the original
    # (the [v1.0] revision bonus wrongly made it win).
    # Old-style translation tags: [t +1 Nightfall], [t1]...
    if re.search(r'\[t[\s+-]', fn_lower) and 'fre' not in fn_lower and not any(tag in fn_lower for tag in FRENCH_TAGS):
        score -= 100

    trans = TRANSLATION_RE.search(fn_lower) or TRADUCTION_RE.search(fn_lower)
    if trans:
        raw_lang = trans.group(1)[:3]
        trans_lang = TRANSLATION_LANGUAGE_CODES.get(raw_lang, raw_lang[:2])  # fre -> fr, spa -> es
        if trans_lang not in allowed_languages:
            score -= 100

    # Monolingual version in an unwanted language: (USA) (Es), (Japan) (Ja)...
    if portable or '(usa)' in fn_lower or '(us)' in fn_lower:
        single_lang = re.search(r'\((es|pt|de|it|nl|ru|pl|fr|ja|ko|zh)\)', fn_lower)
        if single_lang and single_lang.group(1) not in allowed_languages:
            score -= 100

    return score

def get_lang_label(
    score: int,
    preferred_languages: Optional[List[str]] = None,
    system: Optional[str] = None,
) -> str:
    badge = "FR"
    if preferred_languages:
        for value in preferred_languages:
            code = language_code(value)
            if code and code != "en":
                badge = code.upper()
                break
    if is_portable_system(system):
        return f"Portable [{badge}]" if score >= 1100 else "Portable"
    if score >= 1100:
        return f"US 60Hz [{badge}]"
    if score >= 1000:
        return "US 60Hz"
    if score >= 800:
        return "World 60Hz"
    if score >= 600:
        return "Japan 60Hz"
    if score >= 500:
        return f"PAL [{badge}]"
    if score >= 400:
        return "PAL"
    return "Other"
