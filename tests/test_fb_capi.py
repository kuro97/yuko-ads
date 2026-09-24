"""Тесты для integrations/fb_capi.py."""

from unittest.mock import MagicMock, patch

import pytest

from integrations.fb_capi import _make_event_id, send_crm_event, send_mql_batch


# --- _make_event_id ---

def test_make_event_id_is_deterministic():
    """Один и тот же вход → один и тот же event_id (дедупликация)."""
    id1 = _make_event_id(973523146039534, "MQL")
    id2 = _make_event_id(973523146039534, "MQL")
    assert id1 == id2


def test_make_event_id_differs_by_event_name():
    id_mql = _make_event_id(973523146039534, "MQL")
    id_sql = _make_event_id(973523146039534, "SQL")
    assert id_mql != id_sql


def test_make_event_id_max_32_chars():
    assert len(_make_event_id(123456789012345, "MQL")) <= 32


# --- send_crm_event: валидация входных данных ---

def test_send_crm_event_no_dataset_id():
    result = send_crm_event(fb_lead_id=973523146039534, dataset_id="", access_token="tok")
    assert result["success"] is False
    assert "FB_DATASET_ID" in result["error"]


def test_send_crm_event_no_access_token():
    result = send_crm_event(fb_lead_id=973523146039534, dataset_id="123456", access_token="")
    assert result["success"] is False
    assert "FB_CAPI_TOKEN" in result["error"]


def test_send_crm_event_no_lead_id():
    result = send_crm_event(fb_lead_id=None, dataset_id="123456", access_token="tok")
    assert result["success"] is False
    assert "fb_lead_id" in result["error"]


# --- send_crm_event: правильный payload ---

def test_send_crm_event_payload_contains_required_crm_fields():
    """Payload ОБЯЗАН содержать event_source=crm и lead_event_source=AMO CRM."""
    captured = {}

    def mock_post(url, json=None, timeout=None):
        captured["payload"] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"events_received": 1}
        return resp

    with patch("integrations.fb_capi.requests.post", side_effect=mock_post):
        result = send_crm_event(
            fb_lead_id=973523146039534,
            event_name="MQL",
            dataset_id="419430144273",
            access_token="test_token",
        )

    assert result["success"] is True
    assert result["events_received"] == 1

    event = captured["payload"]["data"][0]
    # Это и было root cause — Zapier не добавлял эти поля:
    assert event["custom_data"]["event_source"] == "crm"
    assert event["custom_data"]["lead_event_source"] == "AMO CRM"
    assert event["action_source"] == "system_generated"
    # lead_id — целое число, не строка, не хешировано
    assert event["user_data"]["lead_id"] == 973523146039534
    assert isinstance(event["user_data"]["lead_id"], int)


def test_send_crm_event_url_contains_dataset_id():
    """URL должен содержать dataset_id."""
    captured_url = {}

    def mock_post(url, json=None, timeout=None):
        captured_url["url"] = url
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"events_received": 1}
        return resp

    with patch("integrations.fb_capi.requests.post", side_effect=mock_post):
        send_crm_event(
            fb_lead_id=973523146039534,
            dataset_id="419430144273",
            access_token="test_token",
        )

    assert "419430144273" in captured_url["url"]


def test_send_crm_event_facebook_error():
    """FB вернул ошибку → success=False с текстом ошибки."""
    def mock_post(url, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 400
        resp.json.return_value = {"error": {"message": "Invalid dataset"}}
        return resp

    with patch("integrations.fb_capi.requests.post", side_effect=mock_post):
        result = send_crm_event(
            fb_lead_id=973523146039534,
            dataset_id="bad_id",
            access_token="test_token",
        )

    assert result["success"] is False
    assert "Invalid dataset" in result["error"]


def test_send_crm_event_network_error():
    """Сетевая ошибка → success=False."""
    with patch("integrations.fb_capi.requests.post", side_effect=ConnectionError("timeout")):
        result = send_crm_event(
            fb_lead_id=973523146039534,
            dataset_id="456789",
            access_token="tok",
        )

    assert result["success"] is False
    assert result["error"] is not None


# --- send_mql_batch ---

def test_send_mql_batch_skips_leads_without_fb_lead_id():
    leads = [
        {"id": 1, "fb_lead_id": None},
        {"id": 2},
    ]
    result = send_mql_batch(leads)
    assert result["sent"] == 0
    assert result["skipped"] == 2
    assert result["errors"] == 0


def test_send_mql_batch_skips_already_sent():
    leads = [{"id": 1, "fb_lead_id": 973523146039534}]
    already_sent = {973523146039534}
    result = send_mql_batch(leads, already_sent=already_sent)
    assert result["sent"] == 0
    assert result["skipped"] == 1


def test_send_mql_batch_sends_and_deduplicates():
    """Один лид отправлен → попадает в already_sent → второй вызов пропускает."""
    def mock_post(url, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"events_received": 1}
        return resp

    leads = [{"id": 10, "fb_lead_id": 124812642473004}]
    already_sent = set()

    with patch("integrations.fb_capi.requests.post", side_effect=mock_post), \
         patch("integrations.fb_capi.FB_DATASET_ID", "test_dataset"), \
         patch("integrations.fb_capi.FB_CAPI_TOKEN", "test_token"):
        result1 = send_mql_batch(leads, already_sent=already_sent)

    assert result1["sent"] == 1
    assert 124812642473004 in already_sent

    # Второй вызов — already_sent теперь содержит lead_id
    with patch("integrations.fb_capi.requests.post", side_effect=mock_post), \
         patch("integrations.fb_capi.FB_DATASET_ID", "test_dataset"), \
         patch("integrations.fb_capi.FB_CAPI_TOKEN", "test_token"):
        result2 = send_mql_batch(leads, already_sent=already_sent)

    assert result2["sent"] == 0
    assert result2["skipped"] == 1


def test_send_mql_batch_counts_errors():
    def mock_post(url, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 400
        resp.json.return_value = {"error": {"message": "err"}}
        return resp

    leads = [{"id": 5, "fb_lead_id": 902111382353073}]
    with patch("integrations.fb_capi.requests.post", side_effect=mock_post):
        result = send_mql_batch(leads)

    assert result["errors"] == 1
    assert result["sent"] == 0
