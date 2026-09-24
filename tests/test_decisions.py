"""Тесты для S15: История решений — database functions."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from agent.database import (
    init_db,
    save_decision,
    get_decisions_for_ad,
    get_decision_counts,
    has_repeat_problem,
    get_decisions_history,
)


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    """Инициализирует временную БД для каждого теста."""
    db_path = str(tmp_path / "test_decisions.db")
    init_db(db_path=db_path, json_path=str(tmp_path / "nonexistent.json"))
    yield db_path


class TestSaveDecisionWithMetrics:
    """save_decision сохраняет все метрики."""

    def test_save_with_all_metrics(self):
        save_decision("ad1", "Creative 1", "PAUSED", "CPL too high",
                      spend=100.5, leads=5, cpl=20.1, ctr=1.2, cpm=8.5, romi=150.0, qual_pct=30.0)
        decisions = get_decisions_for_ad("ad1")
        assert len(decisions) == 1
        d = decisions[0]
        assert d["action"] == "PAUSED"
        assert d["metrics"]["spend"] == 100.5
        assert d["metrics"]["ctr"] == 1.2
        assert d["metrics"]["cpm"] == 8.5
        assert d["metrics"]["romi"] == 150.0
        assert d["metrics"]["qual_pct"] == 30.0

    def test_save_without_metrics(self):
        """Обратная совместимость — метрики None."""
        save_decision("ad2", "Creative 2", "DISMISSED", "Оставить")
        decisions = get_decisions_for_ad("ad2")
        assert len(decisions) == 1
        assert decisions[0]["metrics"]["spend"] is None
        assert decisions[0]["metrics"]["ctr"] is None

    def test_save_partial_metrics(self):
        save_decision("ad3", "Creative 3", "PAUSED", "Reason", spend=50.0, leads=3)
        decisions = get_decisions_for_ad("ad3")
        assert decisions[0]["metrics"]["spend"] == 50.0
        assert decisions[0]["metrics"]["romi"] is None

    def test_gateway_effect_projection_and_outbox_commit_atomically(self, temp_db):
        effect_id = "operation-1:decision:PAUSED"
        payload = {"ad_id": "ad-uow", "action": "PAUSED", "success": True}
        notification = {
            "event_type": "ad_paused",
            "title": "Пауза",
            "detail": "ID: ad-uow",
            "level": "critical",
            "meta": {"ad_id": "ad-uow"},
        }

        assert save_decision(
            "ad-uow",
            "Creative UoW",
            "PAUSED",
            "confirmed",
            effect_id=effect_id,
            projection_kind="AUTO_ACTION",
            projection_payload=payload,
            outbox_channel="IN_APP",
            outbox_payload=notification,
        ) is True
        assert save_decision(
            "ad-uow",
            "Creative UoW",
            "PAUSED",
            "confirmed",
            effect_id=effect_id,
            projection_kind="AUTO_ACTION",
            projection_payload=payload,
            outbox_channel="IN_APP",
            outbox_payload=notification,
        ) is False

        connection = sqlite3.connect(temp_db)
        try:
            assert connection.execute(
                "SELECT state FROM action_effects WHERE effect_id=?", (effect_id,)
            ).fetchone() == ("APPLIED",)
            assert connection.execute(
                "SELECT projection_kind FROM action_state_projections WHERE effect_id=?",
                (effect_id,),
            ).fetchone() == ("AUTO_ACTION",)
            assert connection.execute(
                "SELECT state FROM action_outbox WHERE effect_id=?", (effect_id,)
            ).fetchone() == ("PENDING",)
            assert connection.execute(
                "SELECT COUNT(*) FROM decisions WHERE effect_id=?", (effect_id,)
            ).fetchone() == (1,)
        finally:
            connection.close()


class TestGetDecisionsForAd:
    """get_decisions_for_ad возвращает решения для конкретного ad_id."""

    def test_returns_decisions_sorted_desc(self):
        save_decision("ad1", "C1", "PAUSED", "R1")
        save_decision("ad1", "C1", "DISMISSED", "R2")
        save_decision("ad2", "C2", "PAUSED", "R3")
        decisions = get_decisions_for_ad("ad1")
        assert len(decisions) == 2
        # Последнее решение первым (DESC по created_at)
        assert decisions[0]["action"] == "DISMISSED"
        assert decisions[1]["action"] == "PAUSED"

    def test_empty_for_unknown_ad(self):
        assert get_decisions_for_ad("nonexistent") == []


class TestGetDecisionCounts:
    """get_decision_counts возвращает количества."""

    def test_counts(self):
        save_decision("ad1", "C1", "PAUSED", "R1")
        save_decision("ad1", "C1", "DISMISSED", "R2")
        save_decision("ad2", "C2", "PAUSED", "R3")
        counts = get_decision_counts()
        assert counts["ad1"] == 2
        assert counts["ad2"] == 1

    def test_empty(self):
        assert get_decision_counts() == {}


class TestHasRepeatProblem:
    """has_repeat_problem — повторная проблема."""

    def test_repeat_problem_true(self):
        save_decision("ad1", "C1", "PAUSED", "CPL high")
        assert has_repeat_problem("ad1", "ОТКЛЮЧИТЬ") is True

    def test_no_repeat_if_not_disable(self):
        save_decision("ad1", "C1", "PAUSED", "CPL high")
        assert has_repeat_problem("ad1", "ЖДАТЬ") is False

    def test_no_repeat_if_no_prior_pause(self):
        save_decision("ad1", "C1", "DISMISSED", "OK")
        assert has_repeat_problem("ad1", "ОТКЛЮЧИТЬ") is False

    def test_no_repeat_if_no_decisions(self):
        assert has_repeat_problem("ad1", "ОТКЛЮЧИТЬ") is False


class TestDecisionsHistoryMetrics:
    """get_decisions_history возвращает расширенные метрики."""

    def test_history_includes_new_metrics(self):
        save_decision("ad1", "C1", "PAUSED", "R", ctr=1.5, cpm=9.0, romi=200.0, qual_pct=40.0)
        result = get_decisions_history()
        d = result["decisions"][0]
        assert d["ctr"] == 1.5
        assert d["cpm"] == 9.0
        assert d["romi"] == 200.0
        assert d["qual_pct"] == 40.0


# --- API тесты для GET /api/decisions/{ad_id} ---


@pytest.fixture
def api_client(temp_db):
    """TestClient для API тестов."""
    import agent.database as db_module
    # web.app import вызовет init_db() с дефолтным путём — перехватываем
    db_module.DB_PATH = temp_db
    from web.app import app
    # Гарантируем что DB_PATH указывает на temp после импорта
    db_module.DB_PATH = temp_db
    return TestClient(app)


class TestDecisionsForAdEndpoint:
    """GET /api/decisions/{ad_id}"""

    def test_returns_decisions(self, api_client):
        save_decision("ad_api1", "Creative API", "PAUSED", "CPL high",
                      spend=100.0, ctr=1.5)
        resp = api_client.get("/api/decisions/ad_api1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ad_id"] == "ad_api1"
        assert data["count"] == 1
        assert data["decisions"][0]["action"] == "PAUSED"
        assert data["decisions"][0]["metrics"]["spend"] == 100.0

    def test_empty_for_unknown(self, api_client):
        resp = api_client.get("/api/decisions/unknown_ad")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["decisions"] == []
