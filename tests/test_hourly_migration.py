"""
Тесты миграции 013 — таблица ad_hourly_metrics (почасовые метрики первых 48ч
жизни объявления), applied через services.creative_intelligence.
_apply_hourly_metrics_migration.

Покрывают:
1) Идемпотентность: двойной прогон apply-функции не падает (CREATE TABLE/INDEX
   IF NOT EXISTS безопасны при повторном запуске).
2) Таблица и колонки реально появляются (PRAGMA table_info) после первого прогона.
3) UNIQUE(ad_id, datetime_hour) и индексы на месте.
4) init_kb сам применяет миграцию 013 при (пере)старте (как остальные миграции).
5) Повторный прогон не плодит новых объектов и не трогает чужие таблицы (creative_kb).

Сеть не используется — временная SQLite, ничего не мокать.
"""

import sqlite3

import pytest

from services import creative_intelligence as ci


@pytest.fixture(autouse=True)
def reset_kb_path():
    """Сбрасываем глобальный DB_PATH перед и после каждого теста (образец
    tests/test_cdp_payments_migration.py) — не трогаем реальную data/decisions.db."""
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


def _table_columns(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cur.fetchall()}
    finally:
        conn.close()


def _table_exists(db_path: str, table: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def _index_names(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(f"PRAGMA index_list({table})")
        return {row[1] for row in cur.fetchall()}
    finally:
        conn.close()


def test_migration_creates_table_with_columns(tmp_path):
    """После первого прогона миграции 013 таблица ad_hourly_metrics существует
    со всеми полями из DDL."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)

    assert _table_exists(db_path, "ad_hourly_metrics")
    columns = _table_columns(db_path, "ad_hourly_metrics")
    expected = {
        "id",
        "ad_id",
        "datetime_hour",
        "spend",
        "impressions",
        "clicks",
        "actions_lead",
        "video_3s",
        "created_at",
    }
    assert expected <= columns


def test_migration_creates_indexes(tmp_path):
    """Индексы idx_ahm_ad_id / idx_ahm_hour и UNIQUE-индекс по (ad_id, datetime_hour)
    создаются миграцией."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)

    indexes = _index_names(db_path, "ad_hourly_metrics")
    assert "idx_ahm_ad_id" in indexes
    assert "idx_ahm_hour" in indexes
    # UNIQUE(ad_id, datetime_hour) создаёт автогенерируемый unique-индекс
    assert any("ad_hourly_metrics" in name or "sqlite_autoindex" in name for name in indexes)


def test_unique_constraint_ad_id_datetime_hour(tmp_path):
    """UNIQUE(ad_id, datetime_hour) не даёт вставить дубликат — повторная вставка
    с тем же ключом падает с IntegrityError."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO ad_hourly_metrics (ad_id, datetime_hour, spend) VALUES (?, ?, ?)",
            ("ad_1", "2026-07-05T08:00:00", 1.0),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO ad_hourly_metrics (ad_id, datetime_hour, spend) VALUES (?, ?, ?)",
                ("ad_1", "2026-07-05T08:00:00", 2.0),
            )
    finally:
        conn.close()


def test_migration_idempotent_double_apply_via_function(tmp_path):
    """Повторный прямой вызов _apply_hourly_metrics_migration на уже
    мигрированной БД не падает (CREATE TABLE/INDEX IF NOT EXISTS безопасны)."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)

    conn = sqlite3.connect(db_path)
    try:
        ci._apply_hourly_metrics_migration(conn)
        ci._apply_hourly_metrics_migration(conn)
    finally:
        conn.close()

    assert _table_exists(db_path, "ad_hourly_metrics")
    columns = _table_columns(db_path, "ad_hourly_metrics")
    assert {"ad_id", "datetime_hour", "spend"} <= columns


def test_migration_applied_automatically_via_init_kb_restart(tmp_path):
    """init_kb, вызванный повторно (как при рестарте сервера), снова прогоняет
    миграцию 013 и не падает — таблица остаётся на месте, данные не теряются."""
    db_path = str(tmp_path / "auto.db")
    ci.init_kb(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO ad_hourly_metrics (ad_id, datetime_hour, spend) VALUES (?, ?, ?)",
            ("ad_1", "2026-07-05T08:00:00", 3.14),
        )
        conn.commit()
    finally:
        conn.close()

    # Симулируем рестарт процесса: сбрасываем DB_PATH и инициализируем заново
    ci.DB_PATH = None
    ci.init_kb(db_path)

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT spend FROM ad_hourly_metrics WHERE ad_id = ?", ("ad_1",)
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == 3.14


def test_migration_does_not_touch_other_tables(tmp_path):
    """Повторный прогон миграции 013 не трогает существующие таблицы (creative_kb) —
    их набор колонок не меняется."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)

    columns_before = _table_columns(db_path, "creative_kb")

    conn = sqlite3.connect(db_path)
    try:
        ci._apply_hourly_metrics_migration(conn)
    finally:
        conn.close()

    columns_after = _table_columns(db_path, "creative_kb")
    assert columns_before == columns_after


def test_migration_missing_file_is_noop(tmp_path, monkeypatch):
    """Если файл миграции отсутствует (гипотетически, до создания) — apply-функция
    просто ничего не делает и не падает."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)

    conn = sqlite3.connect(db_path)
    try:
        from pathlib import Path as _Path

        original_exists = _Path.exists

        def _fake_exists(self):
            if self.name == "013_ad_hourly_metrics.sql":
                return False
            return original_exists(self)

        monkeypatch.setattr(_Path, "exists", _fake_exists)
        # Не должно бросать исключение
        ci._apply_hourly_metrics_migration(conn)
    finally:
        conn.close()
