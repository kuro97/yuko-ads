"""HTTP integration тесты для /api/ad-generator/* эндпоинтов."""

import json
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agent.copywriter_v2 import AdVariant, GeneratedAdBatch

# Путь к миграции 007
MIGRATION_PATH = Path(__file__).parent.parent / "migrations" / "007_ad_agent_v2.sql"


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Создаёт временную БД с таблицами из миграции 007."""
    db_file = tmp_path / "test_kb.db"
    conn = sqlite3.connect(str(db_file))

    # Создаём базовую creative_kb (нужна для ALTER TABLE в 007)
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

    # Накатываем миграцию 007 идемпотентно
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


@pytest.fixture
def client(db_path):
    """TestClient с замоканной БД и заблокированным init_db / init_creative_kb.

    Импортируем web.app внутри fixture (не на уровне модуля), чтобы не запустить
    side-effect init_db() / init_creative_kb() до патча.
    """
    # Патчим get_connection в обоих сервисах и творческий intelligence
    with patch("services.creative_intelligence.DB_PATH", db_path), \
         patch("services.ad_generator._get_connection") as mock_ad_conn, \
         patch("services.llm_logger._get_connection") as mock_llm_conn, \
         patch("services.ad_generator.get_few_shot_examples", return_value=([], [])):

        def make_conn():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        mock_ad_conn.side_effect = make_conn
        mock_llm_conn.side_effect = make_conn

        # Импортируем app внутри контекста патчей
        # (init_creative_kb и init_db уже вызваны при предыдущем импорте,
        #  но это безвредно — они идемпотентны)
        from fastapi.testclient import TestClient
        from web.app import app
        yield TestClient(app)


def _make_variant_dict(i: int = 0) -> dict:
    """Один вариант рекламного текста для мока."""
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


# ---------------------------------------------------------------------------
# Тест 1: POST /api/ad-generator/generate
# ---------------------------------------------------------------------------


def test_generate_endpoint(client, db_path):
    """POST /generate с замоканным Claude → 200 + drafts в ответе."""
    mock_batch = GeneratedAdBatch(
        variants=[AdVariant(**_make_variant_dict(i)) for i in range(2)]
    )
    mock_usage = {
        "model": "claude-sonnet-4-20250514",
        "input_tokens": 400,
        "output_tokens": 180,
        "latency_ms": 900,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }

    with patch("services.ad_generator.generate_ad_batch", return_value=(mock_batch, mock_usage)):
        resp = client.post(
            "/api/ad-generator/generate",
            json={
                "product": "Продукт A",
                "audience": "Клиенты",
                "offer": "Бесплатная консультация",
                "count": 2,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "drafts" in data
    assert len(data["drafts"]) == 2
    assert data["drafts"][0]["hook"] == "Хук 0"


# ---------------------------------------------------------------------------
# Тест 2: GET /api/ad-generator/drafts
# ---------------------------------------------------------------------------


def test_drafts_list(client, db_path):
    """GET /drafts → 200 + структура {"total": int, "drafts": list}."""
    # Вставляем черновики напрямую в тестовую БД
    conn = sqlite3.connect(db_path)
    for i in range(3):
        conn.execute(
            "INSERT INTO ad_drafts (hook, body, cta, status) VALUES (?, ?, ?, 'pending_review')",
            (f"Хук {i}", f"Тело {i}", f"CTA {i}"),
        )
    conn.commit()
    conn.close()

    resp = client.get("/api/ad-generator/drafts")

    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data
    assert "drafts" in data
    assert isinstance(data["drafts"], list)


# ---------------------------------------------------------------------------
# Тест 3: POST /api/ad-generator/drafts/{id}/approve
# ---------------------------------------------------------------------------


def test_approve_endpoint(client, db_path):
    """POST /drafts/{id}/approve → 200 + {"status": "approved"}."""
    # Вставляем черновик
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO ad_drafts (hook, body, cta, status) VALUES (?, ?, ?, 'pending_review')",
        ("Хук тест", "Тело тест", "CTA тест"),
    )
    draft_id = cur.lastrowid
    conn.commit()
    conn.close()

    resp = client.post(
        f"/api/ad-generator/drafts/{draft_id}/approve",
        json={"approved_by": "admin"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["draft_id"] == draft_id


# ---------------------------------------------------------------------------
# Тест 4: POST /api/ad-generator/drafts/{id}/reject
# ---------------------------------------------------------------------------


def test_reject_endpoint(client, db_path):
    """POST /drafts/{id}/reject → 200 + {"status": "rejected"}."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO ad_drafts (hook, body, cta, status) VALUES (?, ?, ?, 'pending_review')",
        ("Хук тест", "Тело тест", "CTA тест"),
    )
    draft_id = cur.lastrowid
    conn.commit()
    conn.close()

    resp = client.post(
        f"/api/ad-generator/drafts/{draft_id}/reject",
        json={"feedback": "Хук слабый, нет эмоции"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected"
    assert data["draft_id"] == draft_id


# ---------------------------------------------------------------------------
# Тест 5: reject без feedback → 422 (Pydantic validation)
# ---------------------------------------------------------------------------


def test_reject_missing_feedback(client, db_path):
    """POST /reject без поля feedback → 422 Unprocessable Entity (Pydantic)."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO ad_drafts (hook, body, cta, status) VALUES (?, ?, ?, 'pending_review')",
        ("Хук", "Тело", "CTA"),
    )
    draft_id = cur.lastrowid
    conn.commit()
    conn.close()

    # feedback обязателен в RejectRequest — Pydantic вернёт 422
    resp = client.post(
        f"/api/ad-generator/drafts/{draft_id}/reject",
        json={},  # нет поля feedback
    )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Тест 6: GET /api/ad-generator/taxonomy
# ---------------------------------------------------------------------------


def test_taxonomy_endpoint(client, db_path):
    """GET /taxonomy → 200 + hook_types/angles/offer_types из seed-данных."""
    resp = client.get("/api/ad-generator/taxonomy")

    assert resp.status_code == 200
    data = resp.json()

    assert "hook_types" in data
    assert "angles" in data
    assert "offer_types" in data

    # Seed-данные из миграции 007
    assert len(data["hook_types"]) == 9
    assert len(data["angles"]) == 8
    assert len(data["offer_types"]) == 10


# ---------------------------------------------------------------------------
# Тест 7: POST + GET /api/ad-generator/learnings
# ---------------------------------------------------------------------------


def test_learnings_crud(client, db_path):
    """POST /learnings создаёт запись, GET /learnings возвращает total=1."""
    # Создаём learning
    post_resp = client.post(
        "/api/ad-generator/learnings",
        json={
            "statement": "Хук со страхом работает на 30% лучше",
            "confidence": "hypothesis",
        },
    )

    assert post_resp.status_code == 200
    post_data = post_resp.json()
    assert post_data["status"] == "created"
    assert isinstance(post_data["learning_id"], int)

    # Читаем список
    get_resp = client.get("/api/ad-generator/learnings")

    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["total"] == 1
    assert get_data["learnings"][0]["statement"] == "Хук со страхом работает на 30% лучше"
