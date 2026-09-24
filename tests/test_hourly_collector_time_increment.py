"""Сборщик почасовых метрик обязан передавать time_increment=1.

Без него FB складывает весь time_range в 24 строки «час суток», и
datetime_hour перестаёт быть часом жизни объявления.
"""

from unittest.mock import MagicMock, patch

from services.hourly_collector import _fetch_hourly_for_ad


def test_fetch_hourly_passes_time_increment_one():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"data": []}
    captured: dict = {}

    def _get(url, params=None, **kwargs):
        captured.update(params or {})
        return resp

    with patch("services.hourly_collector._throttled_get", side_effect=_get), \
         patch("services.hourly_collector.get_fb_token", return_value="tok"):
        _fetch_hourly_for_ad("123", "2026-09-06")

    assert captured["time_increment"] == 1
    assert captured["breakdowns"] == "hourly_stats_aggregated_by_advertiser_time_zone"
