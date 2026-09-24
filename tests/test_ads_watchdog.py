"""Регресс-тесты объединённого CDP/status сторожа рекламы."""

import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())
_agent_package = __import__("agent")
_supabase_stub = ModuleType("agent.supabase_client")
_supabase_stub._client = None
_supabase_stub.get_supabase = MagicMock()
sys.modules["agent.supabase_client"] = _supabase_stub
setattr(_agent_package, "supabase_client", _supabase_stub)

_TZ = timezone(timedelta(hours=5))


def _spend_result(
    *,
    alerts_sent: int = 0,
    alerts_skipped: int = 0,
    warnings_sent: int = 0,
    ok: bool = True,
    status: str = "evaluated",
) -> dict:
    """Минимальный результат замоканного CDP-контура."""
    return {
        "target_date": "2026-07-16",
        "alerts_sent": alerts_sent,
        "alerts_skipped": alerts_skipped,
        "warnings_sent": warnings_sent,
        "segments_resolved": [],
        "ok": ok,
        "status": status,
    }


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Не позволяем status-дедупу писать в рабочий data/."""
    import services.ads_watchdog as watchdog

    state_path = tmp_path / "ads_watchdog_state.json"
    monkeypatch.setattr(watchdog, "_STATE_FILE", state_path)
    yield state_path


def test_run_watchdog_delegates_spend_to_cdp_with_same_now():
    """Watchdog передаёт время единому CDP runner без собственных spend-расчётов."""
    from services.ads_watchdog import run_watchdog

    now = datetime(2026, 7, 17, 13, 5, tzinfo=_TZ)
    with patch(
        "services.cdp_spend_alerts.run_cdp_spend_alerts",
        return_value=_spend_result(status="awaiting_confirmation"),
    ) as cdp_runner, patch(
        "services.ads_watchdog._check_problem_statuses", return_value=[]
    ), patch("services.notifications.send_telegram") as telegram:
        result = run_watchdog(now)

    cdp_runner.assert_called_once_with(now)
    telegram.assert_not_called()
    assert result == {
        "alerts_sent": 0,
        "alerts_skipped": 0,
        "warnings_sent": 0,
        "spend_status": "awaiting_confirmation",
        "ok": True,
    }


def test_run_watchdog_combines_exact_cdp_and_status_counters():
    """Счётчики двух независимых контуров складываются без потери warning/status."""
    from services.ads_watchdog import run_watchdog

    with patch(
        "services.cdp_spend_alerts.run_cdp_spend_alerts",
        return_value=_spend_result(
            alerts_sent=2,
            alerts_skipped=3,
            warnings_sent=1,
            ok=False,
            status="degraded",
        ),
    ), patch(
        "services.ads_watchdog._check_problem_statuses",
        return_value=["Обнаружено 3 объявлений со статусом DISAPPROVED"],
    ), patch("services.notifications.send_telegram", return_value=True) as telegram:
        result = run_watchdog(datetime(2026, 7, 17, 13, 5, tzinfo=_TZ))

    assert result == {
        "alerts_sent": 3,
        "alerts_skipped": 3,
        "warnings_sent": 1,
        "spend_status": "degraded",
        "ok": False,
    }
    telegram.assert_called_once()
    assert telegram.call_args.kwargs["channel"] == "health"


def test_problem_status_alert_goes_only_to_health_channel():
    """Watchdog сам отправляет только problem-status в health-бот."""
    from services.ads_watchdog import run_watchdog

    with patch(
        "services.cdp_spend_alerts.run_cdp_spend_alerts",
        return_value=_spend_result(),
    ), patch(
        "services.ads_watchdog._check_problem_statuses",
        return_value=["Обнаружено 2 объявлений со статусом WITH_ISSUES"],
    ), patch("services.notifications.send_telegram", return_value=True) as telegram:
        result = run_watchdog()

    assert result["alerts_sent"] == 1
    assert result["ok"] is False
    telegram.assert_called_once()
    assert telegram.call_args.kwargs["channel"] == "health"
    assert "Сторож рекламы" in telegram.call_args.args[0]


def test_status_dedup_is_preserved(isolate_state):
    """Повтор problem-status за 6 часов не отправляется второй раз."""
    from services.ads_watchdog import run_watchdog

    alert = "Обнаружено 2 объявлений со статусом DISAPPROVED"
    cdp = _spend_result(status="already_resolved")
    now = datetime(2026, 7, 17, 14, 0, tzinfo=_TZ)
    with patch(
        "services.cdp_spend_alerts.run_cdp_spend_alerts", return_value=cdp
    ), patch(
        "services.ads_watchdog._check_problem_statuses", return_value=[alert]
    ), patch("services.notifications.send_telegram", return_value=True) as telegram:
        first = run_watchdog(now)
        second = run_watchdog(now + timedelta(hours=1))

    assert first["alerts_sent"] == 1
    assert second["alerts_sent"] == 0
    assert second["alerts_skipped"] == 1
    assert telegram.call_count == 1
    state = json.loads(isolate_state.read_text(encoding="utf-8"))
    assert set(state["alerts"]) == {alert}


@pytest.mark.parametrize(
    "first_outcome",
    [False, RuntimeError("Telegram недоступен")],
    ids=["false", "exception"],
)
def test_status_telegram_failure_is_retried(isolate_state, first_outcome):
    """Неуспешная отправка status не создаёт дедуп; следующий run повторяет её."""
    from services.ads_watchdog import run_watchdog

    alert = "Обнаружено 1 объявлений со статусом DISAPPROVED"
    side_effect = [first_outcome, True]
    with patch(
        "services.cdp_spend_alerts.run_cdp_spend_alerts",
        return_value=_spend_result(status="already_resolved"),
    ), patch(
        "services.ads_watchdog._check_problem_statuses", return_value=[alert]
    ), patch("services.notifications.send_telegram", side_effect=side_effect) as telegram:
        first = run_watchdog(datetime(2026, 7, 17, 14, 0, tzinfo=_TZ))
        second = run_watchdog(datetime(2026, 7, 17, 15, 0, tzinfo=_TZ))

    assert first["alerts_sent"] == 0
    assert first["alerts_skipped"] == 1
    assert second["alerts_sent"] == 1
    assert telegram.call_count == 2
    assert alert in json.loads(isolate_state.read_text(encoding="utf-8"))["alerts"]


def test_unexpected_cdp_failure_does_not_hide_status_health():
    """Защитный барьер CDP не мешает отправить независимый status alert."""
    from services.ads_watchdog import run_watchdog

    with patch(
        "services.cdp_spend_alerts.run_cdp_spend_alerts",
        side_effect=RuntimeError("unexpected"),
    ), patch(
        "services.ads_watchdog._check_problem_statuses",
        return_value=["Обнаружено 1 объявлений со статусом WITH_ISSUES"],
    ), patch("services.notifications.send_telegram", return_value=True):
        result = run_watchdog()

    assert result == {
        "alerts_sent": 1,
        "alerts_skipped": 0,
        "warnings_sent": 0,
        "spend_status": "degraded",
        "ok": False,
    }


def test_check_ads_health_contains_only_problem_statuses():
    """Публичная health-проверка больше не содержит spend producer."""
    from services.ads_watchdog import check_ads_health

    with patch(
        "services.ads_watchdog._check_problem_statuses",
        return_value=["DISAPPROVED 2"],
    ):
        result = check_ads_health()

    assert result == {"alerts": ["DISAPPROVED 2"], "ok": False}


def test_check_ads_health_ok_when_statuses_are_clean():
    """Пустой список проблемных статусов означает здоровый status-контур."""
    from services.ads_watchdog import check_ads_health

    with patch("services.ads_watchdog._check_problem_statuses", return_value=[]):
        assert check_ads_health() == {"alerts": [], "ok": True}


def test_ads_watchdog_source_has_no_legacy_spend_or_mutation_paths():
    """Статически запрещаем возврат FB/cache spend и рекламных мутаций."""
    source_path = Path(__file__).parent.parent / "services" / "ads_watchdog.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))

    forbidden_text = {
        "_get_hourly_spend_today",
        "_get_avg_daily_spend_7d",
        "_get_account_spend",
        "_check_spend_anomaly",
        "analytics_cache.json",
        "agent.fb_common",
        "fb_token_provider",
    }
    assert forbidden_text.isdisjoint(source.split())
    for marker in forbidden_text:
        assert marker not in source

    forbidden_calls = {"pause_ad", "set_adset_budget", "launch_single"}
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert forbidden_calls.isdisjoint(called)
