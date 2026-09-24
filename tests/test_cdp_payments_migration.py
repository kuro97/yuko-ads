"""
Тесты миграции 012 — колонки ERP-оплат в creative_kb (payments_erp/revenue_erp_lcy/
payments_erp_synced_at), applied через services.creative_intelligence.
_apply_cdp_payments_erp_migration.

Покрывают:
1) Идемпотентность: двойной прогон apply-функции не падает (ADD COLUMN на SQLite
   не поддерживает IF NOT EXISTS — 'duplicate column' должен ловиться).
2) Колонки реально появляются в PRAGMA table_info(creative_kb) после первого прогона.
3) init_kb сам применяет миграцию 012 при (пере)старте (как остальные миграции).
4) NULL по умолчанию — новые колонки не «точно 0», а «нет данных».

Сеть не используется — временная SQLite, ничего не мокать.
"""

import sqlite3

import pytest

from services import creative_intelligence as ci


@pytest.fixture(autouse=True)
def reset_kb_path():
    """Сбрасываем глобальный DB_PATH перед и после каждого теста (образец
    tests/test_honest_payments.py) — не трогаем реальную data/decisions.db."""
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


def _table_columns(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("PRAGMA table_info(creative_kb)")
        return {row[1] for row in cur.fetchall()}
    finally:
        conn.close()


def test_migration_adds_columns(tmp_path):
    """После первого прогона миграции 012 три новые колонки видны в table_info."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)

    columns = _table_columns(db_path)
    assert "payments_erp" in columns
    assert "revenue_erp_lcy" in columns
    assert "payments_erp_synced_at" in columns


def test_migration_idempotent_double_apply_via_function(tmp_path):
    """Повторный прямой вызов _apply_cdp_payments_erp_migration на уже
    мигрированной БД не падает (ловит 'duplicate column')."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)

    conn = sqlite3.connect(db_path)
    try:
        # Колонки уже есть после init_kb — повторный прогон не должен бросать исключение
        ci._apply_cdp_payments_erp_migration(conn)
        ci._apply_cdp_payments_erp_migration(conn)
    finally:
        conn.close()

    columns = _table_columns(db_path)
    assert {"payments_erp", "revenue_erp_lcy", "payments_erp_synced_at"} <= columns


def test_migration_applied_automatically_via_init_kb_restart(tmp_path):
    """init_kb, вызванный повторно (как при рестарте сервера), снова прогоняет
    миграцию 012 и не падает — колонки остаются на месте."""
    db_path = str(tmp_path / "auto.db")
    ci.init_kb(db_path)

    # Симулируем рестарт процесса: сбрасываем DB_PATH и инициализируем заново
    ci.DB_PATH = None
    ci.init_kb(db_path)

    columns = _table_columns(db_path)
    assert {"payments_erp", "revenue_erp_lcy", "payments_erp_synced_at"} <= columns


def test_new_columns_default_to_null(tmp_path):
    """Новые колонки для существующей строки по умолчанию NULL («нет данных ERP»),
    не 0 — семантика как у AMO payments (миграция 010)."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("INSERT INTO creative_kb (ad_id) VALUES (?)", ("ad_null_erp",))
        conn.commit()
        row = conn.execute(
            "SELECT payments_erp, revenue_erp_lcy, payments_erp_synced_at "
            "FROM creative_kb WHERE ad_id = ?",
            ("ad_null_erp",),
        ).fetchone()
    finally:
        conn.close()

    assert row["payments_erp"] is None
    assert row["revenue_erp_lcy"] is None
    assert row["payments_erp_synced_at"] is None


def test_migration_missing_file_is_noop(tmp_path, monkeypatch):
    """Если файл миграции отсутствует (гипотетически, до создания) — apply-функция
    просто ничего не делает и не падает."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)

    conn = sqlite3.connect(db_path)
    try:
        # Подменяем Path.exists на False только для проверки ветки "файла нет"
        import services.creative_intelligence as ci_module
        from pathlib import Path as _Path

        original_exists = _Path.exists

        def _fake_exists(self):
            if self.name == "012_cdp_payments_erp.sql":
                return False
            return original_exists(self)

        monkeypatch.setattr(_Path, "exists", _fake_exists)
        # Не должно бросать исключение
        ci_module._apply_cdp_payments_erp_migration(conn)
    finally:
        conn.close()
