"""Fraîcheur du cache : lecture des métadonnées `last_scan` / `last_sync`.

SQLite stocke `CURRENT_TIMESTAMP` en UTC (sans fuseau). Toute conversion vers
l'heure locale doit être explicite pour éviter les décalages d'une heure.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_sqlite_utc(value: Optional[str]) -> Optional[datetime]:
    """Parse un timestamp SQLite (`YYYY-MM-DD HH:MM:SS`, UTC) en datetime aware."""
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
    """Formate un âge en français, sans dépendance à l'horloge."""
    if seconds is None:
        return "inconnu"
    total = int(max(0, seconds))
    if total < 10:
        return "à l'instant"
    if total < 60:
        return f"il y a {total} s"
    minutes = total // 60
    if minutes < 60:
        return f"il y a {minutes} min"
    hours = minutes // 60
    if hours < 24:
        rest = minutes % 60
        return f"il y a {hours} h" + (f" {rest:02d}" if rest else "")
    days = hours // 24
    return f"il y a {days} j"


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
    """Retourne l'état de fraîcheur du cache à partir de la table `meta`."""
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
