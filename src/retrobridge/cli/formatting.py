"""Petits helpers d'affichage partagés par `status` et `list`."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple


BAR_FULL = "🟩"
BAR_PARTIAL = "🟨"
BAR_EMPTY = "⬜"


def basename(path: Optional[str]) -> str:
    if not path:
        return "-"
    return Path(path).name


def truncate(text: Optional[str], max_len: int = 48) -> str:
    if not text:
        return "-"
    if max_len <= 1 or len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def format_size(value: Optional[int], decimals: int = 1) -> str:
    if value is None:
        return "-"
    size = float(value)
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.{decimals}f} Go"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.{decimals}f} Mo"
    if size >= 1024:
        return f"{size / 1024:.0f} Ko"
    return f"{int(size)} o"


def format_size_compact(value: Optional[int]) -> str:
    """Format court pour les tableaux étroits : 14Mo, 494Mo, 1.2Go."""
    if value is None:
        return "-"
    size = float(value)
    if size <= 0:
        return "0"
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f}Go"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.0f}Mo"
    if size >= 1024:
        return f"{size / 1024:.0f}Ko"
    return f"{int(size)}o"


def bar_parts(percent: Optional[float], width: int = 8) -> Tuple[str, str, str]:
    """Barre en emojis : 🟩 plein, 🟨 partiel, ⬜ vide.

    Le carré jaune matérialise une fraction (< 1 bloc) pour que 2 % ne soit
    ni invisible ni affiché comme 1 bloc plein.
    """
    pct = 0.0 if percent is None else max(0.0, min(100.0, float(percent)))
    units = pct / 100.0 * width
    full = int(units)
    remainder = units - full
    if full >= width:
        return BAR_FULL * width, "", ""
    partial = BAR_PARTIAL if remainder > 0 else ""
    empty = width - full - (1 if partial else 0)
    return BAR_FULL * full, partial, BAR_EMPTY * empty


def progress_bar(percent: Optional[float], width: int = 8) -> str:
    full, partial, empty = bar_parts(percent, width)
    return full + partial + empty


def progress_label(percent: Optional[float]) -> str:
    """2.8 % -> 2 %, 0.4 % -> <1 %."""
    pct = 0.0 if percent is None else max(0.0, min(100.0, float(percent)))
    if pct <= 0:
        return "0%"
    if pct < 1:
        return "<1%"
    return f"{int(pct)}%"


def progress_text(
    percent: Optional[float],
    completed: Optional[int] = None,
    total: Optional[int] = None,
    width: int = 8,
    compact: bool = False,
) -> str:
    pct = 0.0 if percent is None else max(0.0, min(100.0, float(percent)))
    text = f"{progress_bar(pct, width)} {pct:5.1f}%"
    if completed is not None and total:
        if compact:
            text += f" {format_size_compact(completed)}/{format_size_compact(total)}"
        else:
            text += f"  {format_size(completed)} / {format_size(total)}"
    return text
