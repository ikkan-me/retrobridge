"""Tests du module de fraîcheur (lot A1)."""
from __future__ import annotations

from datetime import datetime, timezone

from retrobridge.services.freshness import (
    format_age,
    get_freshness,
    parse_sqlite_utc,
)
from retrobridge.storage.db import get_db_connection, init_db


def _insert_meta(conn, key: str, updated_at: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value, updated_at) VALUES (?, 'completed', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = 'completed', updated_at = excluded.updated_at;",
        (key, updated_at),
    )


def test_parse_sqlite_utc_is_explicit():
    parsed = parse_sqlite_utc("2026-11-21 20:47:49")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    assert parsed.hour == 20
    assert parse_sqlite_utc(None) is None
    assert parse_sqlite_utc("pas une date") is None


def test_format_age_is_human_readable():
    assert format_age(None) == "inconnu"
    assert format_age(4) == "à l'instant"
    assert format_age(42) == "il y a 42 s"
    assert format_age(12 * 60) == "il y a 12 min"
    assert format_age(2 * 3600 + 5 * 60) == "il y a 2 h 05"
    assert format_age(26 * 3600) == "il y a 1 j"


def test_freshness_marks_stale_and_keeps_utc(tmp_path):
    db_path = tmp_path / "fresh.db"
    init_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        _insert_meta(conn, "last_scan", "2026-11-21 20:00:00")
        _insert_meta(conn, "last_sync", "2026-11-21 20:55:00")
        conn.commit()

        now = datetime(2026, 11, 21, 21, 15, 0, tzinfo=timezone.utc)
        freshness = get_freshness(conn, scan_ttl=3600, sync_ttl=600, now=now)

    assert freshness["scan"]["age_seconds"] == 75 * 60
    assert freshness["scan"]["stale"] is True
    assert freshness["sync"]["age_seconds"] == 20 * 60
    assert freshness["sync"]["stale"] is True  # > 600 s
    assert freshness["stale"] is True
    assert freshness["scan"]["at_utc"].endswith("+00:00")
    assert freshness["scan"]["at_local"] is not None


def test_freshness_recent_is_not_stale(tmp_path):
    db_path = tmp_path / "fresh2.db"
    init_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        _insert_meta(conn, "last_scan", "2026-11-21 20:58:00")
        _insert_meta(conn, "last_sync", "2026-11-21 20:59:00")
        conn.commit()

        now = datetime(2026, 11, 21, 21, 0, 0, tzinfo=timezone.utc)
        freshness = get_freshness(conn, scan_ttl=3600, sync_ttl=600, now=now)

    assert freshness["scan"]["stale"] is False
    assert freshness["sync"]["stale"] is False
    assert freshness["stale"] is False


def test_freshness_without_meta_is_stale(tmp_path):
    db_path = tmp_path / "fresh3.db"
    init_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        freshness = get_freshness(conn)

    assert freshness["scan"]["age_seconds"] is None
    assert freshness["scan"]["age_label"] == "inconnu"
    assert freshness["scan"]["stale"] is True
