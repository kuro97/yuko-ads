"""
Тесты сервиса уведомлений: лента событий, фильтрация, mark read, email.
"""

from unittest.mock import patch, MagicMock

import pytest

from services.notifications import (
    add_event,
    get_events,
    mark_read,
    mark_all_read,
    clear_events,
    EVENT_AD_PAUSED,
    EVENT_AD_DISMISSED,
    EVENT_LAUNCH_DONE,
    EVENT_ALERT,
)


@pytest.fixture(autouse=True)
def _clean_events():
    """Очистка ленты перед каждым тестом."""
    clear_events()
    yield
    clear_events()


# --- Добавление событий ---

class TestAddEvent:
    """Создание событий."""

    def test_basic_event(self):
        """Базовое создание события."""
        event = add_event(EVENT_ALERT, "CPL выше нормы", "CityA L2: CPL=45$")
        assert event["type"] == EVENT_ALERT
        assert event["title"] == "CPL выше нормы"
        assert event["detail"] == "CityA L2: CPL=45$"
        assert event["level"] == "info"
        assert event["read"] is False
        assert "timestamp" in event
        assert "id" in event

    def test_event_with_level(self):
        """Событие с уровнем critical."""
        event = add_event(EVENT_AD_PAUSED, "Объявление отключено", level="critical")
        assert event["level"] == "critical"

    def test_event_with_meta(self):
        """Событие с метаданными."""
        meta = {"ad_id": "123", "city": "CityA"}
        event = add_event(EVENT_AD_PAUSED, "Отключено", meta=meta)
        assert event["meta"]["ad_id"] == "123"
        assert event["meta"]["city"] == "CityA"

    def test_events_have_unique_ids(self):
        """Каждое событие имеет уникальный ID."""
        e1 = add_event(EVENT_ALERT, "Первое")
        e2 = add_event(EVENT_ALERT, "Второе")
        assert e1["id"] != e2["id"]

    def test_newest_first(self):
        """Новые события первые в списке."""
        add_event(EVENT_ALERT, "Старое")
        add_event(EVENT_ALERT, "Новое")
        result = get_events()
        assert result["events"][0]["title"] == "Новое"
        assert result["events"][1]["title"] == "Старое"


# --- Получение событий ---

class TestGetEvents:
    """Фильтрация и пагинация."""

    def test_empty_list(self):
        """Пустая лента."""
        result = get_events()
        assert result["events"] == []
        assert result["total"] == 0
        assert result["unread"] == 0

    def test_limit_offset(self):
        """Пагинация: limit и offset."""
        for i in range(10):
            add_event(EVENT_ALERT, f"Событие {i}")
        result = get_events(limit=3, offset=2)
        assert len(result["events"]) == 3
        assert result["total"] == 10

    def test_filter_by_type(self):
        """Фильтр по типу события."""
        add_event(EVENT_ALERT, "Алерт")
        add_event(EVENT_AD_PAUSED, "Пауза")
        add_event(EVENT_ALERT, "Ещё алерт")

        result = get_events(event_type=EVENT_ALERT)
        assert result["total"] == 2
        assert all(e["type"] == EVENT_ALERT for e in result["events"])

    def test_filter_unread_only(self):
        """Фильтр: только непрочитанные."""
        e1 = add_event(EVENT_ALERT, "Прочитанное")
        add_event(EVENT_ALERT, "Непрочитанное")
        mark_read(e1["id"])

        result = get_events(unread_only=True)
        assert result["total"] == 1
        assert result["events"][0]["title"] == "Непрочитанное"

    def test_unread_count(self):
        """Счётчик непрочитанных в ответе."""
        add_event(EVENT_ALERT, "Первое")
        e2 = add_event(EVENT_ALERT, "Второе")
        add_event(EVENT_ALERT, "Третье")
        mark_read(e2["id"])

        result = get_events()
        assert result["unread"] == 2


# --- Отметка прочитанным ---

class TestMarkRead:
    """Отметка событий прочитанными."""

    def test_mark_single(self):
        """Отметить одно событие."""
        event = add_event(EVENT_ALERT, "Тест")
        assert mark_read(event["id"]) is True

        result = get_events()
        assert result["events"][0]["read"] is True

    def test_mark_nonexistent(self):
        """Несуществующий ID → False."""
        assert mark_read(99999) is False

    def test_mark_all_read(self):
        """Отметить все как прочитанные."""
        add_event(EVENT_ALERT, "Первое")
        add_event(EVENT_ALERT, "Второе")
        add_event(EVENT_ALERT, "Третье")

        count = mark_all_read()
        assert count == 3

        result = get_events()
        assert result["unread"] == 0

    def test_mark_all_read_idempotent(self):
        """Повторный mark_all_read → 0."""
        add_event(EVENT_ALERT, "Тест")
        mark_all_read()
        count = mark_all_read()
        assert count == 0


# --- Email ---

class TestEmail:
    """Email уведомления."""

    @patch.dict("os.environ", {
        "NOTIFY_EMAIL": "test@example.com",
        "SMTP_HOST": "smtp.test.com",
        "SMTP_USER": "user",
        "SMTP_PASS": "pass",
    })
    @patch("services.notifications.threading.Thread")
    def test_email_sent_on_ad_paused(self, mock_thread):
        """Email отправляется при отключении объявления."""
        add_event(EVENT_AD_PAUSED, "Объявление отключено")
        mock_thread.assert_called_once()
        # Проверяем что Thread запущен с daemon=True
        call_kwargs = mock_thread.call_args[1]
        assert call_kwargs["daemon"] is True

    @patch("services.notifications._try_send_email")
    def test_no_email_for_alerts(self, mock_email):
        """Email НЕ отправляется для обычных алертов."""
        add_event(EVENT_ALERT, "CPL высокий")
        mock_email.assert_not_called()

    @patch("services.notifications._try_send_email")
    def test_no_email_for_dismissed(self, mock_email):
        """Email НЕ отправляется при отклонении."""
        add_event(EVENT_AD_DISMISSED, "Отклонено")
        mock_email.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    @patch("services.notifications.threading.Thread")
    def test_no_email_without_config(self, mock_thread):
        """Без env переменных email не отправляется."""
        add_event(EVENT_AD_PAUSED, "Тест")
        mock_thread.assert_not_called()


# --- Лимит хранилища ---

class TestStorageLimit:
    """Ограничение размера ленты."""

    def test_max_500_events(self):
        """Максимум 500 событий, старые вытесняются."""
        for i in range(510):
            add_event(EVENT_ALERT, f"Событие {i}")

        result = get_events(limit=1000)
        assert result["total"] == 500
        # Первое — самое новое (509)
        assert result["events"][0]["title"] == "Событие 509"


# --- API тест ---

class TestNotificationsAPI:
    """Тесты FastAPI эндпоинтов."""

    def test_get_notifications(self):
        """GET /api/notifications — базовый."""
        from fastapi.testclient import TestClient
        from web.app import app

        add_event(EVENT_ALERT, "Тестовый алерт")

        client = TestClient(app)
        resp = client.get("/api/notifications")
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert "total" in data
        assert "unread" in data
        assert data["total"] >= 1

    def test_get_notifications_with_filter(self):
        """GET /api/notifications?type=alert — фильтр по типу."""
        from fastapi.testclient import TestClient
        from web.app import app

        add_event(EVENT_ALERT, "Алерт")
        add_event(EVENT_AD_PAUSED, "Пауза")

        client = TestClient(app)
        resp = client.get(f"/api/notifications?type={EVENT_ALERT}")
        assert resp.status_code == 200
        data = resp.json()
        assert all(e["type"] == EVENT_ALERT for e in data["events"])

    def test_mark_read_endpoint(self):
        """POST /api/notifications/read-all — отметить все прочитанными."""
        from fastapi.testclient import TestClient
        from web.app import app

        add_event(EVENT_ALERT, "Непрочитанное")

        client = TestClient(app)
        resp = client.post("/api/notifications/read-all")
        assert resp.status_code == 200
        assert resp.json()["marked"] >= 1

    def test_mark_single_read_endpoint(self):
        """POST /api/notifications/{id}/read — отметить одно прочитанным."""
        from fastapi.testclient import TestClient
        from web.app import app

        event = add_event(EVENT_ALERT, "Тест")

        client = TestClient(app)
        resp = client.post(f"/api/notifications/{event['id']}/read")
        assert resp.status_code == 200
