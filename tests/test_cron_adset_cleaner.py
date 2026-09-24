"""Cron запускает только durable read-only proactive cleaner."""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent))

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from web.app import (  # noqa: E402
    _ADSET_CLEANER_HOUR,
    _TZ_LOCAL,
    _cron_adset_cleaner,
    _should_run_adset_cleaner_cron,
)


def _now(hour: int = 5) -> datetime:
    return datetime(2026, 7, 21, hour, 7, tzinfo=_TZ_LOCAL)


def _result(*, ran=True, phase="COMPLETED", errors=None, warnings=None):
    errors = list(errors or [])
    warnings = list(warnings or [])
    return {
        "run_id": "proactive-run-1",
        "ran": ran,
        "requested_mode": "dry_run",
        "effective_mode": "dry_run",
        "phase": phase,
        "would_delete": [],
        "skipped": [],
        "warnings": warnings,
        "errors": errors,
        "counters": {
            "discovered": 0,
            "eligible": 0,
            "would_delete": 0,
            "deleted": 0,
            "skipped": 0,
            "warnings": len(warnings),
            "errors": len(errors),
        },
    }


def test_cron_hour_gate_has_no_json_daily_state():
    assert _ADSET_CLEANER_HOUR == 5
    assert _should_run_adset_cleaner_cron(_now(5)) is True
    assert _should_run_adset_cleaner_cron(_now(4)) is False
    assert _should_run_adset_cleaner_cron(_now(6)) is False


def test_cron_calls_only_proactive_cleaner_with_scheduled_date():
    with patch("web.app.datetime") as clock, patch(
        "services.proactive_adset_cleaner.run_proactive_cleaner",
        return_value=_result(),
    ) as proactive, patch("services.adset_cleaner.run_cleaner") as legacy, patch(
        "web.app.report_cron_success"
    ) as success, patch(
        "integrations.facebook_ads_mutation_transport.create_ad"
    ) as fb_create, patch(
        "integrations.facebook_ads_mutation_transport.set_ad_status"
    ) as fb_status, patch("services.action_gateway.execute_action") as gateway, patch(
        "integrations.trello.session.put"
    ) as trello_put:
        clock.now.return_value = _now()

        _cron_adset_cleaner()

    proactive.assert_called_once_with(scheduled_date=_now().date())
    legacy.assert_not_called()
    # Прямых FB-мутаторов у integrations.facebook больше нет: закрываем текущие
    # точки (execution-транспорт и сам gateway) — крон не должен их касаться.
    fb_create.assert_not_called()
    fb_status.assert_not_called()
    gateway.assert_not_called()
    trello_put.assert_not_called()
    success.assert_called_once_with("_cron_adset_cleaner")


def test_repeated_scheduler_tick_delegates_idempotency_to_durable_service():
    already_completed = _result(
        ran=False,
        phase="COMPLETED",
        warnings=["already_completed"],
    )
    with patch("web.app.datetime") as clock, patch(
        "services.proactive_adset_cleaner.run_proactive_cleaner",
        return_value=already_completed,
    ) as proactive, patch("web.app.report_cron_success") as success:
        clock.now.return_value = _now()

        _cron_adset_cleaner()
        _cron_adset_cleaner()

    assert proactive.call_count == 2
    assert all(
        call.kwargs == {"scheduled_date": _now().date()}
        for call in proactive.call_args_list
    )
    assert success.call_count == 2


def test_hour_outside_window_does_not_call_cleaner():
    with patch("web.app.datetime") as clock, patch(
        "services.proactive_adset_cleaner.run_proactive_cleaner"
    ) as proactive:
        clock.now.return_value = _now(6)

        _cron_adset_cleaner()

    proactive.assert_not_called()


def test_proactive_errors_are_reported_without_raising():
    failed = _result(phase="FAILED", errors=["fb_unavailable"])
    with patch("web.app.datetime") as clock, patch(
        "services.proactive_adset_cleaner.run_proactive_cleaner",
        return_value=failed,
    ), patch("web.app.report_cron_failure") as failure:
        clock.now.return_value = _now()

        _cron_adset_cleaner()

    failure.assert_called_once_with("_cron_adset_cleaner", "cleaner run incomplete")


def test_proactive_exception_is_contained_and_reported():
    error = RuntimeError("test error")
    with patch("web.app.datetime") as clock, patch(
        "services.proactive_adset_cleaner.run_proactive_cleaner",
        side_effect=error,
    ), patch("web.app.report_cron_failure") as failure:
        clock.now.return_value = _now()

        _cron_adset_cleaner()

    failure.assert_called_once_with("_cron_adset_cleaner", error)
