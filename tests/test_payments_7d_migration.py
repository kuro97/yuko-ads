"""
Тесты миграции 015 — source-specific колонки честных 7d-оплат в creative_kb
(payments_amo_7d/revenue_amo_7d/amo_7d_* и payments_erp_7d/revenue_erp_7d/erp_7d_*),
applied через services.creative_intelligence._apply_payments_7d_migration.

Покрывают (план ревью, Wave 3A):
1) Миграция на ПУСТОЙ DB (init_kb): все 12 колонок появляются, значения NULL.
2) Миграция на УЖЕ ЗАПОЛНЕННОЙ legacy-DB (без 015-колонок): колонки добавляются,
   существующие данные НЕ тронуты, новые колонки NULL («нет данных», не 0).
3) Идемпотентность: двойной прогон apply-функции не падает ('duplicate column').
4) init_kb сам применяет миграцию 015 при (пере)старте.

Сеть не используется — временная SQLite, ничего не мокать.
"""

import sqlite3

import pytest

from services import creative_intelligence as ci


_SEVEN_D_COLUMNS = {
    "payments_amo_7d", "revenue_amo_7d",
    "amo_7d_window_from", "amo_7d_window_to", "amo_7d_synced_at", "amo_7d_complete",
    "payments_erp_7d", "revenue_erp_7d",
    "erp_7d_window_from", "erp_7d_window_to", "erp_7d_synced_at", "erp_7d_complete",
}


@pytest.fixture(autouse=True)
def reset_kb_path():
    """Сбрасываем глобальный DB_PATH перед и после каждого теста — не трогаем
    реальную data/decisions.db (образец test_cdp_payments_migration.py)."""
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


def _table_columns(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(creative_kb)")}
    finally:
        conn.close()


def test_migration_adds_all_12_columns_on_empty_db(tmp_path):
    """Миграция 015 на пустой БД (через init_kb) добавляет все 12 source-specific колонок."""
    db_path = str(tmp_path / "empty.db")
    ci.init_kb(db_path)

    columns = _table_columns(db_path)
    assert _SEVEN_D_COLUMNS <= columns, f"не хватает: {_SEVEN_D_COLUMNS - columns}"


def test_new_columns_default_to_null(tmp_path):
    """Новые 7d-колонки для существующей строки по умолчанию NULL («нет
    подтверждённых полных данных», НЕ ноль оплат — семантика миграций 010/012)."""
    db_path = str(tmp_path / "nulls.db")
    ci.init_kb(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("INSERT INTO creative_kb (ad_id) VALUES (?)", ("ad_null_7d",))
        conn.commit()
        row = conn.execute(
            "SELECT payments_amo_7d, amo_7d_complete, payments_erp_7d, erp_7d_complete "
            "FROM creative_kb WHERE ad_id = ?",
            ("ad_null_7d",),
        ).fetchone()
    finally:
        conn.close()

    assert row["payments_amo_7d"] is None
    assert row["amo_7d_complete"] is None
    assert row["payments_erp_7d"] is None
    assert row["erp_7d_complete"] is None


def test_migration_on_populated_legacy_db_preserves_data(tmp_path):
    """Миграция на УЖЕ ЗАПОЛНЕННОЙ legacy-схеме (без 015-колонок): колонки
    добавляются, существующие данные не тронуты, новые колонки NULL."""
    db_path = str(tmp_path / "legacy.db")
    # Legacy creative_kb БЕЗ 7d-колонок, с данными.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE creative_kb (
            ad_id TEXT PRIMARY KEY,
            ad_name TEXT DEFAULT '',
            payments INTEGER,
            revenue REAL
        );
        INSERT INTO creative_kb (ad_id, ad_name, payments, revenue)
            VALUES ('legacy_1', 'Старое объявление', 7, 123456.0);
        """
    )
    conn.commit()
    conn.close()

    # Применяем миграцию 015 к заполненной legacy-БД.
    conn = sqlite3.connect(db_path)
    try:
        ci._apply_payments_7d_migration(conn)
    finally:
        conn.close()

    columns = _table_columns(db_path)
    assert _SEVEN_D_COLUMNS <= columns

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT payments, revenue, ad_name, payments_amo_7d, payments_erp_7d "
            "FROM creative_kb WHERE ad_id = ?",
            ("legacy_1",),
        ).fetchone()
    finally:
        conn.close()

    # Старые данные целы
    assert row["payments"] == 7
    assert row["revenue"] == 123456.0
    assert row["ad_name"] == "Старое объявление"
    # Новые колонки NULL (нет данных за 7d)
    assert row["payments_amo_7d"] is None
    assert row["payments_erp_7d"] is None


def test_migration_idempotent_double_apply(tmp_path):
    """Повторный прямой вызов _apply_payments_7d_migration на уже мигрированной
    БД не падает (ADD COLUMN → 'duplicate column' должен ловиться)."""
    db_path = str(tmp_path / "idem.db")
    ci.init_kb(db_path)

    conn = sqlite3.connect(db_path)
    try:
        ci._apply_payments_7d_migration(conn)
        ci._apply_payments_7d_migration(conn)
    finally:
        conn.close()

    assert _SEVEN_D_COLUMNS <= _table_columns(db_path)


def test_migration_applied_automatically_via_init_kb_restart(tmp_path):
    """init_kb, вызванный повторно (как при рестарте сервера), снова прогоняет
    миграцию 015 и не падает — колонки остаются на месте."""
    db_path = str(tmp_path / "restart.db")
    ci.init_kb(db_path)
    ci.DB_PATH = None
    ci.init_kb(db_path)

    assert _SEVEN_D_COLUMNS <= _table_columns(db_path)


def test_migration_missing_file_is_noop(tmp_path, monkeypatch):
    """Если файл миграции 015 отсутствует — apply-функция ничего не делает и не падает."""
    db_path = str(tmp_path / "nofile.db")
    ci.init_kb(db_path)

    from pathlib import Path as _Path
    original_exists = _Path.exists

    def _fake_exists(self):
        if self.name == "015_payments_7d.sql":
            return False
        return original_exists(self)

    monkeypatch.setattr(_Path, "exists", _fake_exists)

    conn = sqlite3.connect(db_path)
    try:
        ci._apply_payments_7d_migration(conn)  # не должно бросать
    finally:
        conn.close()
