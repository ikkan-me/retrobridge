"""Shared text utilities (no UI dependency)."""
from __future__ import annotations

import re
from typing import Optional

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]"
)


def strip_emoji(text: Optional[str]) -> str:
    """Removes legacy flags/emojis from a language label."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", _EMOJI_RE.sub("", text)).strip()
