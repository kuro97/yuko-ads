"""Адаптивный троттлинг FB API: пауза между запросами зависит от реальной загрузки лимита.

Мотив: фиксированные 5 секунд при загрузке лимита в единицы процентов превращали одну паузу объявления
(десятки чтений FB) в минуты ожидания, очередь одобренных пауз расходилась сутками.
"""

import json
from unittest.mock import MagicMock

import pytest

from agent import fb_common


@pytest.fixture(autouse=True)
def _clean_usage():
    fb_common._fb_usage.clear()
    yield
    fb_common._fb_usage.clear()


def _resp(headers):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = headers
    return resp


def _buc(account="1", **fields):
    block = {"type": "ads_management", "call_count": 1, "total_cputime": 1, "total_time": 1,
             "estimated_time_to_regain_access": 0, **fields}
    return {"x-business-use-case-usage": json.dumps({account: [block]})}


def test_unknown_usage_uses_moderate_interval():
    assert fb_common._throttle_interval() == fb_common._FB_INTERVAL_UNKNOWN


@pytest.mark.parametrize("fields,expected", [
    ({"call_count": 1}, 0.3),                                   # низкая загрузка: 1% лимита
    ({"total_cputime": 49}, 0.3),
    ({"total_time": 60}, 2.0),
    ({"call_count": 80}, 5.0),
    ({"call_count": 95}, 15.0),
    ({"estimated_time_to_regain_access": 7}, 15.0),             # FB уже просит подождать
])
def test_interval_follows_observed_usage(fields, expected):
    fb_common._observe_usage(_resp(_buc(**fields)))
    assert fb_common._throttle_interval() == expected


def test_worst_account_and_header_wins():
    fb_common._observe_usage(_resp(_buc(account="cab_b", call_count=2)))
    fb_common._observe_usage(_resp({"x-fb-ads-insights-throttle": json.dumps({"app_id_util_pct": 3, "acc_id_util_pct": 77})}))
    assert fb_common._throttle_interval() == 5.0


def test_stale_observation_expires(monkeypatch):
    fb_common._observe_usage(_resp(_buc(call_count=95)))
    assert fb_common._throttle_interval() == 15.0
    real = fb_common._time.time()
    monkeypatch.setattr(fb_common._time, "time", lambda: real + fb_common._FB_USAGE_TTL_SECONDS + 1)
    assert fb_common._throttle_interval() == fb_common._FB_INTERVAL_UNKNOWN


def test_garbage_headers_never_break_request():
    for headers in (None, MagicMock(), {"x-app-usage": "не json"}, {"x-app-usage": json.dumps([1, 2])},
                    {"x-business-use-case-usage": json.dumps({"1": "x"})}, {"x-app-usage": json.dumps({"call_count": True})}):
        fb_common._observe_usage(_resp(headers))
    assert fb_common._fb_usage == {}


def test_zero_min_interval_disables_throttle(monkeypatch):
    monkeypatch.setattr(fb_common, "_FB_MIN_INTERVAL", 0.0)
    fb_common._observe_usage(_resp(_buc(call_count=95)))
    assert fb_common._throttle_interval() == 0.0


def test_request_observes_usage_and_sleeps_by_it(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(fb_common._time, "sleep", lambda s: sleeps.append(round(s, 1)))
    monkeypatch.setattr(fb_common, "_fb_last_call", fb_common._time.time())
    low = _resp(_buc(call_count=1))
    fb_common._do_throttled_request(lambda url, **kw: low, "https://graph.facebook.com/x")
    fb_common._do_throttled_request(lambda url, **kw: low, "https://graph.facebook.com/x")
    assert sleeps and max(sleeps) <= 1.0, f"при загрузке 1% пауза не должна быть пятисекундной: {sleeps}"
