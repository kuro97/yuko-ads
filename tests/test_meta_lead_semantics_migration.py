"""Миграция 018: provenance Meta lead semantics и legacy isolation."""

import sqlite3

from services import creative_intelligence as ci


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def test_init_kb_adds_lead_semantics_provenance(tmp_path):
    db_path = str(tmp_path / "fresh.db")

    ci.init_kb(db_path)

    conn = sqlite3.connect(db_path)
    try:
        for table in ("ad_daily_metrics", "ad_hourly_metrics"):
            assert {"lead_semantics_version", "lead_parse_status"} <= _columns(conn, table)
    finally:
        conn.close()


def test_migration_marks_existing_rows_legacy_and_is_idempotent(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE ad_daily_metrics (ad_id TEXT, date TEXT, leads INTEGER)")
        conn.execute(
            "CREATE TABLE ad_hourly_metrics (ad_id TEXT, datetime_hour TEXT, actions_lead INTEGER)"
        )
        conn.execute("INSERT INTO ad_daily_metrics VALUES ('ad-1', '2026-07-22', 9)")
        conn.execute("INSERT INTO ad_hourly_metrics VALUES ('ad-1', '2026-07-22T08:00:00', 9)")

        ci._apply_meta_lead_semantics_migration(conn)
        ci._apply_meta_lead_semantics_migration(conn)

        daily = conn.execute(
            "SELECT lead_semantics_version, lead_parse_status FROM ad_daily_metrics"
        ).fetchone()
        hourly = conn.execute(
            "SELECT lead_semantics_version, lead_parse_status FROM ad_hourly_metrics"
        ).fetchone()
    finally:
        conn.close()

    assert daily == (1, "legacy")
    assert hourly == (1, "legacy")
