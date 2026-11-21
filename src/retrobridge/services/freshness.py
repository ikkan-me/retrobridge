"""Cache freshness: reads the `last_scan` / `last_sync` metadata.

SQLite stores `CURRENT_TIMESTAMP` in UTC (with no timezone). Any conversion to
local time must be explicit to avoid one-hour offsets.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_sqlite_utc(value: Optional[str]) -> Optional[datetime]:
    """Parses a SQLite timestamp (`YYYY-MM-DD HH:MM:SS`, UTC) into an aware datetime."""
    if not value:
        return None
    raw = str(value).strip().replace("T", " ")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_local(value: datetime) -> datetime:
    return value.astimezone()


def format_local(value: Optional[datetime], with_seconds: bool = False) -> Optional[str]:
    if value is None:
        return None
    fmt = "%d/%m/%Y %H:%M:%S" if with_seconds else "%d/%m/%Y %H:%M"
    return to_local(value).strftime(fmt)


def format_age(seconds: Optional[float]) -> str:
    """Formats an age without clock dependency."""
    if seconds is None:
        return "unknown"
    total = int(max(0, seconds))
    if total < 10:
        return "just now"
    if total < 60:
        return f"{total} s ago"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        rest = minutes % 60
        suffix = f" {rest:02d} min" if rest else ""
        return f"{hours} h{suffix} ago"
    days = hours // 24
    return f"{days} d ago"


def _entry(raw_value: Optional[str], ttl_seconds: int, now: datetime) -> Dict[str, Any]:
    parsed = parse_sqlite_utc(raw_value)
    age = (now - parsed).total_seconds() if parsed else None
    return {
        "at_utc": parsed.isoformat() if parsed else None,
        "at_local": format_local(parsed),
        "age_seconds": int(age) if age is not None else None,
        "age_label": format_age(age),
        "stale": age is None or age >= ttl_seconds,
        "ttl_seconds": ttl_seconds,
    }


def get_freshness(
    conn,
    scan_ttl: int = 3600,
    sync_ttl: int = 600,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Returns the cache freshness state from the `meta` table."""
    reference = now or utc_now()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    reference = reference.astimezone(timezone.utc)

    rows = {
        row["key"]: row["updated_at"]
        for row in conn.execute(
            "SELECT key, updated_at FROM meta WHERE key IN ('last_scan', 'last_sync');"
        )
    }
    scan = _entry(rows.get("last_scan"), scan_ttl, reference)
    sync = _entry(rows.get("last_sync"), sync_ttl, reference)
    return {
        "generated_at_utc": reference.isoformat(),
        "scan": scan,
        "sync": sync,
        # The global badge follows the torrent cache: it drives matching.
        "stale": scan["stale"],
    }
