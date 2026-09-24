"""
Тесты retry с exponential backoff при FB API rate limit.
"""

import time
from unittest.mock import patch, MagicMock

import pytest

from agent.fb_common import (
    _throttled_get,
    FBApiError,
    _FB_MIN_INTERVAL,
    _RATE_LIMIT_MAX_RETRIES,
    _RATE_LIMIT_BASE_DELAY,
)


def _make_response(status_code=200, json_data=None, text=""):
    """Создаёт мок ответа requests."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json = MagicMock(return_value=json_data or {})
    return resp


class TestFBRetryOnRateLimit:
    """Retry при FB API rate limit (400 + 'too many calls')."""

    @patch("agent.fb_common._time.sleep")
    @patch("agent.fb_common.session.get")
    def test_retry_on_rate_limit_then_success(self, mock_get, mock_sleep):
        """Rate limit на первый запрос → retry → успех.

        Гнилой тест (A4): ждал backoff ≥20с, но текущий _RATE_LIMIT_BASE_DELAY=5
        даёт первый backoff 5с (короткий backoff — намеренное решение, чтобы UI
        не висел). Порог адаптирован к текущей константе."""
        rate_limit_resp = _make_response(
            400,
            text='{"error":{"message":"There have been too many calls to this ad-account"}}'
        )
        ok_resp = _make_response(200, json_data={"data": [{"id": "1"}]})

        mock_get.side_effect = [rate_limit_resp, ok_resp]

        result = _throttled_get("https://graph.facebook.com/v21.0/test")

        assert result.status_code == 200
        # Должен быть sleep для backoff (помимо троттлинга) — не меньше базового delay
        backoff_sleeps = [c for c in mock_sleep.call_args_list
                          if c[0][0] >= _RATE_LIMIT_BASE_DELAY]
        assert len(backoff_sleeps) >= 1

    @patch("agent.fb_common._time.sleep")
    @patch("agent.fb_common.session.get")
    def test_retry_exhausted_raises_error(self, mock_get, mock_sleep):
        """Retry исчерпаны → FBApiError.

        Гнилой тест (A4): _RATE_LIMIT_MAX_RETRIES снижен с 3 до 2 (1 оригинал + 2
        retry = 3 попытки), сообщение об ошибке теперь на русском."""
        rate_limit_resp = _make_response(
            400,
            text='{"error":{"message":"There have been too many calls to this ad-account"}}'
        )
        mock_get.return_value = rate_limit_resp

        with pytest.raises(FBApiError, match="ограничил запросы"):
            _throttled_get("https://graph.facebook.com/v21.0/test")

        # 1 оригинальная попытка + _RATE_LIMIT_MAX_RETRIES retry
        assert mock_get.call_count == _RATE_LIMIT_MAX_RETRIES + 1

    @patch("agent.fb_common._time.sleep")
    @patch("agent.fb_common.session.get")
    def test_no_retry_on_other_400_errors(self, mock_get, mock_sleep):
        """Обычная 400 ошибка (не rate limit) → без retry."""
        error_resp = _make_response(
            400,
            text='{"error":{"message":"Invalid parameter"}}'
        )
        mock_get.return_value = error_resp

        result = _throttled_get("https://graph.facebook.com/v21.0/test")

        # Только 1 запрос, без retry
        assert mock_get.call_count == 1
        assert result.status_code == 400

    @patch("agent.fb_common._time.sleep")
    @patch("agent.fb_common.session.get")
    def test_exponential_backoff_delays(self, mock_get, mock_sleep):
        """Backoff увеличивается: 5s → 10s.

        Гнилой тест (A4): при _RATE_LIMIT_MAX_RETRIES=2 всего 3 попытки (1 оригинал
        + 2 retry), значит успех может прийти максимум после 2 rate-limit ответов
        (2 backoff-sleep), а не 3."""
        rate_limit_resp = _make_response(
            400,
            text='{"error":{"message":"too many calls"}}'
        )
        ok_resp = _make_response(200, json_data={"data": []})

        # 2 rate limit → потом ok (ровно _RATE_LIMIT_MAX_RETRIES попыток retry)
        mock_get.side_effect = [rate_limit_resp, rate_limit_resp, ok_resp]

        result = _throttled_get("https://graph.facebook.com/v21.0/test")

        assert result.status_code == 200
        # Проверяем что backoff sleep вызывался с растущими значениями
        backoff_sleeps = [c[0][0] for c in mock_sleep.call_args_list
                          if c[0][0] >= _RATE_LIMIT_BASE_DELAY]
        assert len(backoff_sleeps) == _RATE_LIMIT_MAX_RETRIES
        assert backoff_sleeps[0] < backoff_sleeps[1]

    @patch("agent.fb_common._time.sleep")
    @patch("agent.fb_common.session.get")
    def test_no_retry_on_success(self, mock_get, mock_sleep):
        """Успешный запрос → без retry."""
        ok_resp = _make_response(200, json_data={"data": []})
        mock_get.return_value = ok_resp

        result = _throttled_get("https://graph.facebook.com/v21.0/test")

        assert result.status_code == 200
        assert mock_get.call_count == 1
