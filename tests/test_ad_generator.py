"""Тесты для services/ad_generator.py — бизнес-логика генерации черновиков."""

import json
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agent.copywriter_v2 import AdBrief, AdVariant, GeneratedAdBatch

# Путь к миграции 007
MIGRATION_PATH = Path(__file__).parent.parent / "migrations" / "007_ad_agent_v2.sql"


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Создаёт временную БД с таблицами из миграции 007.

    Сначала создаём creative_kb (нужна для ALTER TABLE в 007),
    затем накатываем миграцию идемпотентно.
    """
    db_file = tmp_path / "test_kb.db"
    conn = sqlite3.connect(str(db_file))

    # Создаём базовую creative_kb (требуется для ALTER TABLE в 007)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS creative_kb (
            ad_id TEXT PRIMARY KEY,
            ad_name TEXT DEFAULT '',
            ad_body TEXT DEFAULT '',
            city TEXT DEFAULT '',
            creative_class TEXT DEFAULT '',
            spend REAL DEFAULT 0,
            cpl REAL DEFAULT 0,
            leads INTEGER DEFAULT 0,
            hook_rate REAL DEFAULT 0,
            hold_rate REAL DEFAULT 0
        )
    """)
    conn.commit()

    # Применяем миграцию 007 — идемпотентно (пропускаем дубли и already exists)
    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    for stmt in migration_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            raise
    conn.commit()
    conn.close()
    return str(db_file)


def _make_variant_dict(i: int = 0) -> dict:
    """Вспомогательная функция — один вариант рекламного текста."""
    return {
        "hook": f"Хук {i}",
        "body": f"Тело текста {i}",
        "cta": f"CTA {i}",
        "angle": "social_proof",
        "hook_type": "question",
        "format": "short_post",
        "target_persona": "client_l1",
        "primary_language": "l1",
        "rationale": f"Причина {i}",
    }


def _make_brief(**kwargs) -> AdBrief:
    """Создаёт тестовый бриф с возможностью переопределить поля."""
    defaults = {
        "product": "Продукт A",
        "audience": "Клиенты 25-45 лет",
        "offer": "Бесплатный пробный период",
        "count": 2,
    }
    defaults.update(kwargs)
    return AdBrief(**defaults)


# ---------------------------------------------------------------------------
# Тест 1: generate_drafts — полный пайплайн
# ---------------------------------------------------------------------------


def test_generate_drafts(db_path):
    """generate_drafts сохраняет черновики в ad_drafts и логирует LLM-вызов.

    Мокаем: generate_ad_batch (Claude) и get_few_shot_examples (KB запрос).
    Проверяем: черновики в БД, llm_call_id проставлен, ответ содержит нужные ключи.
    """
    # Формируем мок-батч с 2 вариантами
    mock_batch = GeneratedAdBatch(
        variants=[AdVariant(**_make_variant_dict(i)) for i in range(2)]
    )
    mock_usage = {
        "model": "claude-sonnet-4-20250514",
        "input_tokens": 500,
        "output_tokens": 200,
        "latency_ms": 1200,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }

    brief = _make_brief()

    with patch("services.creative_intelligence.DB_PATH", db_path), \
         patch("services.ad_generator._get_connection") as mock_conn_factory, \
         patch("services.llm_logger._get_connection") as mock_llm_conn_factory, \
         patch("services.ad_generator.generate_ad_batch", return_value=(mock_batch, mock_usage)), \
         patch("services.ad_generator.get_few_shot_examples", return_value=([], [])):

        # Оба get_connection должны открывать одну и ту же тестовую БД
        def make_conn():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        mock_conn_factory.side_effect = make_conn
        mock_llm_conn_factory.side_effect = make_conn

        from services.ad_generator import generate_drafts
        result = generate_drafts(brief)

    # Проверяем структуру ответа
    assert "drafts" in result
    assert "model" in result
    assert "llm_call_id" in result
    assert len(result["drafts"]) == 2

    # Проверяем что черновики действительно попали в БД
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM ad_drafts ORDER BY id").fetchall()
    conn.close()

    assert len(rows) == 2
    assert rows[0]["status"] == "pending_review"
    assert rows[0]["hook"] == "Хук 0"

    # llm_calls тоже должны быть в БД
    conn = sqlite3.connect(db_path)
    llm_rows = conn.execute("SELECT * FROM llm_calls").fetchall()
    conn.close()
    assert len(llm_rows) >= 1


# ---------------------------------------------------------------------------
# Тест 2: get_drafts — все черновики
# ---------------------------------------------------------------------------


def test_get_drafts_all(db_path):
    """get_drafts() без фильтра возвращает total=3 для 3 вставленных черновиков."""
    conn = sqlite3.connect(db_path)
    for i in range(3):
        conn.execute(
            """INSERT INTO ad_drafts (hook, body, cta, status)
               VALUES (?, ?, ?, 'pending_review')""",
            (f"Хук {i}", f"Тело {i}", f"CTA {i}"),
        )
    conn.commit()
    conn.close()

    with patch("services.ad_generator._get_connection") as mock_conn_factory:
        def make_conn():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        mock_conn_factory.side_effect = make_conn

        from services.ad_generator import get_drafts
        result = get_drafts()

    assert result["total"] == 3
    assert len(result["drafts"]) == 3


# ---------------------------------------------------------------------------
# Тест 3: get_drafts — фильтрация по статусу
# ---------------------------------------------------------------------------


def test_get_drafts_filtered(db_path):
    """get_drafts(status='pending_review') возвращает только pending черновики."""
    conn = sqlite3.connect(db_path)
    # 2 pending + 1 approved
    conn.execute(
        "INSERT INTO ad_drafts (hook, body, cta, status) VALUES (?, ?, ?, ?)",
        ("Хук 1", "Тело 1", "CTA 1", "pending_review"),
    )
    conn.execute(
        "INSERT INTO ad_drafts (hook, body, cta, status) VALUES (?, ?, ?, ?)",
        ("Хук 2", "Тело 2", "CTA 2", "pending_review"),
    )
    conn.execute(
        "INSERT INTO ad_drafts (hook, body, cta, status) VALUES (?, ?, ?, ?)",
        ("Хук 3", "Тело 3", "CTA 3", "approved"),
    )
    conn.commit()
    conn.close()

    with patch("services.ad_generator._get_connection") as mock_conn_factory:
        def make_conn():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        mock_conn_factory.side_effect = make_conn

        from services.ad_generator import get_drafts
        result = get_drafts(status="pending_review")

    assert result["total"] == 2
    assert all(d["status"] == "pending_review" for d in result["drafts"])


# ---------------------------------------------------------------------------
# Тест 4: approve_draft — успешное одобрение
# ---------------------------------------------------------------------------


def test_approve_draft(db_path):
    """approve_draft(id) меняет status на 'approved' и проставляет approved_at."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO ad_drafts (hook, body, cta, status) VALUES (?, ?, ?, 'pending_review')",
        ("Хук", "Тело", "CTA"),
    )
    draft_id = cur.lastrowid
    conn.commit()
    conn.close()

    with patch("services.ad_generator._get_connection") as mock_conn_factory:
        def make_conn():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        mock_conn_factory.side_effect = make_conn

        from services.ad_generator import approve_draft
        result = approve_draft(draft_id)

    assert result["status"] == "approved"
    assert result["draft_id"] == draft_id
    assert result["approved_at"] is not None

    # Проверяем что в БД действительно обновилось
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ad_drafts WHERE id = ?", (draft_id,)).fetchone()
    conn.close()

    assert row["status"] == "approved"
    assert row["approved_at"] is not None


# ---------------------------------------------------------------------------
# Тест 5: approve_draft — уже обработан → ValueError
# ---------------------------------------------------------------------------


def test_approve_already_processed(db_path):
    """approve_draft для approved черновика бросает ValueError 'уже обработан'."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO ad_drafts (hook, body, cta, status) VALUES (?, ?, ?, 'approved')",
        ("Хук", "Тело", "CTA"),
    )
    draft_id = cur.lastrowid
    conn.commit()
    conn.close()

    with patch("services.ad_generator._get_connection") as mock_conn_factory:
        def make_conn():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        mock_conn_factory.side_effect = make_conn

        from services.ad_generator import approve_draft
        with pytest.raises(ValueError, match="уже обработан"):
            approve_draft(draft_id)


# ---------------------------------------------------------------------------
# Тест 6: approve_draft — не найден → ValueError
# ---------------------------------------------------------------------------


def test_approve_not_found(db_path):
    """approve_draft(999) при отсутствии записи бросает ValueError 'не найден'."""
    with patch("services.ad_generator._get_connection") as mock_conn_factory:
        def make_conn():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        mock_conn_factory.side_effect = make_conn

        from services.ad_generator import approve_draft
        with pytest.raises(ValueError, match="не найден"):
            approve_draft(999)


# ---------------------------------------------------------------------------
# Тест 7: reject_draft — успешное отклонение
# ---------------------------------------------------------------------------


def test_reject_draft(db_path):
    """reject_draft(id, feedback) меняет status на 'rejected' и сохраняет feedback."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO ad_drafts (hook, body, cta, status) VALUES (?, ?, ?, 'pending_review')",
        ("Хук", "Тело", "CTA"),
    )
    draft_id = cur.lastrowid
    conn.commit()
    conn.close()

    with patch("services.ad_generator._get_connection") as mock_conn_factory:
        def make_conn():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        mock_conn_factory.side_effect = make_conn

        from services.ad_generator import reject_draft
        result = reject_draft(draft_id, "плохой текст, нет хука")

    assert result["status"] == "rejected"
    assert result["draft_id"] == draft_id

    # Проверяем что feedback сохранён в БД
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ad_drafts WHERE id = ?", (draft_id,)).fetchone()
    conn.close()

    assert row["status"] == "rejected"
    assert "плохой текст" in row["feedback"]


# ---------------------------------------------------------------------------
# Тест 8: reject_draft — пустой feedback → ValueError
# ---------------------------------------------------------------------------


def test_reject_empty_feedback(db_path):
    """reject_draft с пустым feedback бросает ValueError 'feedback обязателен'."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO ad_drafts (hook, body, cta, status) VALUES (?, ?, ?, 'pending_review')",
        ("Хук", "Тело", "CTA"),
    )
    draft_id = cur.lastrowid
    conn.commit()
    conn.close()

    with patch("services.ad_generator._get_connection") as mock_conn_factory:
        def make_conn():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        mock_conn_factory.side_effect = make_conn

        from services.ad_generator import reject_draft
        with pytest.raises(ValueError, match="feedback обязателен"):
            reject_draft(draft_id, "")


# ---------------------------------------------------------------------------
# Тест 9: get_taxonomy — seed-данные из миграции
# ---------------------------------------------------------------------------


def test_get_taxonomy(db_path):
    """get_taxonomy() возвращает hook_types, angles, offer_types с seed-данными из 007."""
    with patch("services.ad_generator._get_connection") as mock_conn_factory:
        def make_conn():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        mock_conn_factory.side_effect = make_conn

        from services.ad_generator import get_taxonomy
        result = get_taxonomy()

    assert "hook_types" in result
    assert "angles" in result
    assert "offer_types" in result

    # Seed-данные из миграции 007: 9 хуков, 8 углов, 10 офферов
    assert len(result["hook_types"]) == 9
    assert len(result["angles"]) == 8
    assert len(result["offer_types"]) == 10

    # Проверяем конкретные slug из seed-данных
    hook_slugs = [h["slug"] for h in result["hook_types"]]
    assert "social_proof" in hook_slugs
    assert "fear_left_behind" in hook_slugs

    angle_slugs = [a["slug"] for a in result["angles"]]
    assert "family_pride" in angle_slugs


# ---------------------------------------------------------------------------
# Тест 10: add_learning + get_learnings
# ---------------------------------------------------------------------------


def test_add_and_get_learnings(db_path):
    """add_learning добавляет запись, get_learnings() возвращает total=1 с нужным statement."""
    with patch("services.ad_generator._get_connection") as mock_conn_factory:
        def make_conn():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        mock_conn_factory.side_effect = make_conn

        from services.ad_generator import add_learning, get_learnings

        # Добавляем learning
        result = add_learning("тест инсайт: хук со страхом работает лучше")

    assert result["status"] == "created"
    assert isinstance(result["learning_id"], int)
    assert result["learning_id"] > 0

    # Читаем через get_learnings
    with patch("services.ad_generator._get_connection") as mock_conn_factory:
        def make_conn():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        mock_conn_factory.side_effect = make_conn

        from services.ad_generator import get_learnings
        learnings = get_learnings()

    assert learnings["total"] == 1
    assert learnings["learnings"][0]["statement"] == "тест инсайт: хук со страхом работает лучше"
