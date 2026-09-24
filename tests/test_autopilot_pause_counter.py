"""Дневной счётчик пауз автопилота считает исполненные попытки конвейера (волна 2a).

Раньше _projected_pause_count читал только AUTO_ACTION-проекции с
counter_scope, которые никто не писал — max_pauses_per_day видел 0.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services import autopilot

_TZ = timezone(timedelta(hours=5))


def _db(tmp_path, rows):
    path = tmp_path / "kb.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE owner_action_attempts (attempt_id TEXT, operation_kind TEXT, state TEXT, resource_id TEXT, completed_at TEXT)"
    )
    conn.executemany("INSERT INTO owner_action_attempts VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()

    def _connect():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    return _connect


def test_counts_confirmed_pause_attempts_of_the_local_day(tmp_path):
    today = datetime(2026, 9, 15, tzinfo=_TZ)
    inside = (today + timedelta(hours=10)).astimezone(timezone.utc).isoformat()
    late_evening = (today + timedelta(hours=23, minutes=30)).astimezone(timezone.utc).isoformat()
    yesterday = (today - timedelta(hours=1)).astimezone(timezone.utc).isoformat()
    rows = [
        ("a1", "PAUSE_AD", "CONFIRMED", "ad1", inside),
        ("a2", "PAUSE_AD", "CONFIRMED", "ad1", inside),          # тот же ad — считается один раз
        ("a3", "PAUSE_AD", "CONFIRMED", "ad2", late_evening),
        ("a4", "PAUSE_AD", "CONFIRMED", "ad3", yesterday),       # вчера по локальному времени
        ("a5", "PAUSE_AD", "FAILED_NO_EFFECT", "ad4", inside),   # не исполнено
        ("a6", "LAUNCH_AD", "CONFIRMED", "ad5", inside),         # не пауза
    ]
    connect = _db(tmp_path, rows)
    with patch("services.creative_intelligence._get_connection", side_effect=connect):
        assert autopilot._count_confirmed_pauses_today("2026-09-15") == 2


def test_projected_count_uses_confirmed_attempts_and_is_fail_open(tmp_path):
    with patch.object(autopilot, "_count_confirmed_pauses_today", return_value=3), \
         patch("agent.database.get_action_state_projections", side_effect=RuntimeError("no db")):
        assert autopilot._projected_pause_count("live", "2026-09-15") == 3
    with patch("services.creative_intelligence._get_connection", side_effect=RuntimeError("KB не инициализирована")):
        assert autopilot._count_confirmed_pauses_today("2026-09-15") == 0
