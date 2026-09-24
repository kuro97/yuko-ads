"""Тесты CDP-only алертов расхода за завершённый день.

Проверяем строгую валидацию daily-report, независимость сегментов ``total`` и
``online``, двухснимочное подтверждение и безопасную доставку в основной бот.
CDP, Telegram и файловый state во всех тестах изолированы моками.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import services.cdp_spend_alerts as spend_alerts
from services.cdp_client import CdpError


TARGET = date(2026, 7, 16)
LOCAL_TZ = timezone(timedelta(hours=5))
FIRST_CHECK = datetime(2026, 7, 17, 12, 5, tzinfo=LOCAL_TZ)
SECOND_CHECK = datetime(2026, 7, 17, 13, 5, tzinfo=LOCAL_TZ)


def _dates() -> list[date]:
    """Возвращает семь baseline-дней и target-day."""
    return [TARGET - timedelta(days=offset) for offset in range(7, -1, -1)]


def _items(
    *,
    online_baseline: float = 100.0,
    online_target: float = 100.0,
    offline_baseline: float = 900.0,
    offline_target: float = 900.0,
    iso_dates: bool = True,
) -> list[dict]:
    """Строит полный 8-дневный CDP-снимок с CityA и точным Онлайн."""
    rows: list[dict] = []
    for day in _dates():
        raw_day: str | date = day.isoformat() if iso_dates else day
        rows.extend(
            [
                {
                    "report_date": raw_day,
                    "city": "CityA",
                    "ad_spend": offline_target if day == TARGET else offline_baseline,
                    "ignored_extra": "ok",
                },
                {
                    "report_date": raw_day,
                    "city": "Онлайн",
                    "ad_spend": online_target if day == TARGET else online_baseline,
                },
            ]
        )
    return rows


def _replace_row(
    items: list[dict],
    day: date,
    existing_city: str,
    **changes: object,
) -> list[dict]:
    """Возвращает копию строк с точечным изменением одной пары date/city."""
    changed = deepcopy(items)
    for row in changed:
        raw_date = row.get("report_date")
        parsed = raw_date if isinstance(raw_date, date) else date.fromisoformat(raw_date)
        if parsed == day and row.get("city") == existing_city:
            row.update(changes)
            return changed
    raise AssertionError(f"Не найдена строка {day}/{existing_city}")


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch) -> dict:
    """Хранит state в памяти и не допускает чтения/записи production JSON."""
    storage: dict = {}

    def load_state(_path):
        return deepcopy(storage)

    def save_state(_path, new_state):
        storage.clear()
        storage.update(deepcopy(new_state))

    monkeypatch.setattr(spend_alerts.state_store, "load_json_state", load_state)
    monkeypatch.setattr(spend_alerts.state_store, "save_json_state", save_state)
    return storage


def _mock_boundaries(
    monkeypatch,
    *,
    items: list[dict] | None = None,
    cdp_error: Exception | None = None,
    telegram_result: bool | list[bool | Exception] = True,
) -> tuple[MagicMock, MagicMock]:
    """Подставляет внешние границы CDP GET и Telegram send."""
    cdp = MagicMock()
    if cdp_error is not None:
        cdp.side_effect = cdp_error
    else:
        cdp.return_value = _items() if items is None else items

    telegram = MagicMock()
    if isinstance(telegram_result, list):
        telegram.side_effect = telegram_result
    else:
        telegram.return_value = telegram_result

    monkeypatch.setattr(spend_alerts.cdp_client, "get_daily_report", cdp)
    monkeypatch.setattr(spend_alerts.notifications, "send_telegram", telegram)
    return cdp, telegram


# ---------------------------------------------------------------------------
# Канонизация и строгая runtime-валидация
# ---------------------------------------------------------------------------


def test_total_includes_exact_online_once_and_uses_all_eight_dates():
    """Итого складывает CityA+Онлайн, не теряя target и семь baseline-дней."""
    result = spend_alerts._canonical_segment_snapshot(_items(), "total", TARGET)

    assert result.reason_code is None
    assert result.snapshot is not None
    assert result.snapshot.daily_usd == tuple((day, 1000.0) for day in _dates())


def test_online_uses_only_exact_city_and_ignores_invalid_other_city():
    """Online/онлайн и битая чужая строка не загрязняют точный сегмент Онлайн."""
    items = _items()
    for day in _dates():
        items.append(
            {"report_date": day.isoformat(), "city": "Online", "ad_spend": 9999.0}
        )
    items.append({"report_date": TARGET.isoformat(), "city": "CityA", "ad_spend": "0"})

    result = spend_alerts._canonical_segment_snapshot(items, "online", TARGET)

    assert result.reason_code is None
    assert result.snapshot is not None
    assert result.snapshot.daily_usd == tuple((day, 100.0) for day in _dates())


@pytest.mark.parametrize(
    "invalid_spend",
    [None, float("nan"), float("inf"), float("-inf"), -1, "0", "12.5", True],
    ids=["none", "nan", "inf", "minus-inf", "negative", "string-zero", "string", "bool"],
)
def test_runtime_validation_rejects_invalid_spend_without_coercion(invalid_spend):
    """Missing/NaN/Inf/negative/string/bool не становятся честным числом расхода."""
    items = _replace_row(_items(), TARGET, "Онлайн", ad_spend=invalid_spend)

    result = spend_alerts._canonical_segment_snapshot(items, "online", TARGET)

    assert result.snapshot is None
    assert result.reason_code == "invalid_row"


@pytest.mark.parametrize("invalid_city", ["", " ", " CityA", "CityA ", "_total"])
def test_total_rejects_invalid_city_labels(invalid_city):
    """Пустые, пробельные и служебные city-метки делают total недоступным."""
    items = _replace_row(_items(), TARGET, "CityA", city=invalid_city)

    result = spend_alerts._canonical_segment_snapshot(items, "total", TARGET)

    assert result.snapshot is None
    assert result.reason_code == "invalid_row"


def test_iso_report_date_is_accepted_and_parsed_to_date():
    """Штатная ISO-строка даты принимается и превращается в date."""
    result = spend_alerts._canonical_segment_snapshot(_items(iso_dates=True), "online", TARGET)

    assert result.snapshot is not None
    assert result.snapshot.daily_usd[0][0] == TARGET - timedelta(days=7)
    assert type(result.snapshot.daily_usd[0][0]) is date


def test_today_row_is_ignored_and_does_not_change_snapshot():
    """Сегодняшний незавершённый ноль не влияет на target=yesterday."""
    base = _items()
    with_today = deepcopy(base)
    with_today.extend(
        [
            {"report_date": "2026-07-17", "city": "CityA", "ad_spend": 0.0},
            {"report_date": "2026-07-17", "city": "Онлайн", "ad_spend": 0.0},
        ]
    )

    base_snapshot = spend_alerts._canonical_segment_snapshot(base, "total", TARGET).snapshot
    today_snapshot = spend_alerts._canonical_segment_snapshot(
        with_today, "total", TARGET
    ).snapshot

    assert base_snapshot is not None
    assert today_snapshot == base_snapshot


def test_missing_target_date_returns_typed_gap():
    """Отсутствующий завершённый target-day не подменяется нулём."""
    items = [
        row
        for row in _items()
        if row["report_date"] != TARGET.isoformat()
    ]

    total = spend_alerts._canonical_segment_snapshot(items, "total", TARGET)
    online = spend_alerts._canonical_segment_snapshot(items, "online", TARGET)

    assert total.reason_code == "missing_date"
    assert online.reason_code == "missing_online"


def test_missing_one_online_date_is_not_zero_spend():
    """7/8 строк Онлайн дают missing_online, а не no_spend."""
    items = [
        row
        for row in _items()
        if not (
            row["report_date"] == TARGET.isoformat() and row["city"] == "Онлайн"
        )
    ]

    result = spend_alerts._canonical_segment_snapshot(items, "online", TARGET)

    assert result.snapshot is None
    assert result.reason_code == "missing_online"


def test_total_rejects_city_coverage_drift():
    """Неполный город на одной дате не досуммируется молча."""
    items = [
        row
        for row in _items()
        if not (
            row["report_date"] == TARGET.isoformat() and row["city"] == "CityA"
        )
    ]

    result = spend_alerts._canonical_segment_snapshot(items, "total", TARGET)

    assert result.snapshot is None
    assert result.reason_code == "city_coverage_mismatch"


def test_duplicate_date_city_pair_is_rejected():
    """Дубликат CDP-строки не суммируется повторно."""
    items = _items()
    items.append(deepcopy(items[-1]))

    result = spend_alerts._canonical_segment_snapshot(items, "online", TARGET)

    assert result.snapshot is None
    assert result.reason_code == "duplicate_row"


def test_total_fingerprint_changes_when_city_spends_compensate():
    """Перераспределение +10/-10 меняет fingerprint при прежней общей сумме."""
    original = _items()
    changed = _replace_row(original, TARGET, "CityA", ad_spend=910.0)
    changed = _replace_row(changed, TARGET, "Онлайн", ad_spend=90.0)

    first = spend_alerts._canonical_segment_snapshot(original, "total", TARGET).snapshot
    second = spend_alerts._canonical_segment_snapshot(changed, "total", TARGET).snapshot

    assert first is not None and second is not None
    assert first.daily_usd == second.daily_usd
    assert first.fingerprint != second.fingerprint


# ---------------------------------------------------------------------------
# Оценка порогов
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target,status,ratio,reason",
    [
        (19.0, "low", 0.19, "below_low_threshold"),
        (20.0, "normal", 0.2, None),
        (150.0, "normal", 1.5, None),
        (151.0, "high", 1.51, "above_high_threshold"),
        (0.0, "no_spend", 0.0, "confirmed_zero"),
    ],
)
def test_assessment_thresholds_are_strict(target, status, ratio, reason):
    """0.2 и 1.5 нормальны; low/high только строго за границами; zero отдельно."""
    snapshot = spend_alerts._canonical_segment_snapshot(
        _items(
            online_baseline=100.0,
            online_target=target,
            offline_baseline=0.0,
            offline_target=0.0,
        ),
        "online",
        TARGET,
    ).snapshot

    assert snapshot is not None
    assessment = spend_alerts.build_assessment(snapshot)
    assert assessment.status == status
    assert assessment.ratio == pytest.approx(ratio)
    assert assessment.reason_code == reason


@pytest.mark.parametrize("target", [0.0, 100.0])
def test_zero_baseline_is_unavailable_for_any_target(target):
    """Без нормы нельзя объявить ни no-spend, ни бесконечный high."""
    snapshot = spend_alerts._canonical_segment_snapshot(
        _items(
            online_baseline=0.0,
            online_target=target,
            offline_baseline=0.0,
            offline_target=target,
        ),
        "online",
        TARGET,
    ).snapshot

    assert snapshot is not None
    assessment = spend_alerts.build_assessment(snapshot)
    assert assessment.status == "unavailable"
    assert assessment.ratio is None
    assert assessment.reason_code == "no_baseline"


# ---------------------------------------------------------------------------
# Оркестрация, confirmation, state и Telegram
# ---------------------------------------------------------------------------


def test_before_noon_does_not_read_cdp_or_send(monkeypatch):
    """До 12:00 по локальному времени внешний CDP вообще не вызывается."""
    cdp, telegram = _mock_boundaries(monkeypatch)

    result = spend_alerts.run_cdp_spend_alerts(
        datetime(2026, 7, 17, 11, 59, tzinfo=LOCAL_TZ)
    )

    assert result["status"] == "before_window"
    assert result["target_date"] == TARGET.isoformat()
    cdp.assert_not_called()
    telegram.assert_not_called()


def test_naive_now_is_utc_before_conversion(monkeypatch):
    """Naive 06:59 UTC становится 11:59 CityA и остаётся до окна."""
    cdp, _ = _mock_boundaries(monkeypatch)

    result = spend_alerts.run_cdp_spend_alerts(datetime(2026, 7, 17, 6, 59))

    assert result["status"] == "before_window"
    cdp.assert_not_called()


def test_first_non_high_snapshot_creates_two_candidates_without_telegram(
    monkeypatch, isolated_state
):
    """Первый normal-снимок сохраняется отдельно по двум сегментам."""
    cdp, telegram = _mock_boundaries(monkeypatch)

    result = spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)

    assert result["status"] == "awaiting_confirmation"
    assert result["ok"] is True
    assert set(isolated_state["candidate"]) == {"total", "online"}
    assert isolated_state["candidate"]["total"]["observed_at"] == FIRST_CHECK.isoformat()
    cdp.assert_called_once_with(TARGET - timedelta(days=7), TARGET)
    telegram.assert_not_called()


def test_same_snapshot_before_60_minutes_still_waits(monkeypatch, isolated_state):
    """Идентичный снимок через 59 минут не считается подтверждением."""
    _, telegram = _mock_boundaries(monkeypatch)
    spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)

    result = spend_alerts.run_cdp_spend_alerts(FIRST_CHECK + timedelta(minutes=59))

    assert result["status"] == "awaiting_confirmation"
    assert result["segments_resolved"] == []
    assert set(isolated_state["candidate"]) == {"total", "online"}
    telegram.assert_not_called()


def test_stable_normal_snapshot_resolves_both_at_60_minutes(
    monkeypatch, isolated_state
):
    """Идентичный normal-снимок через ровно 60 минут закрывает оба сегмента."""
    cdp, telegram = _mock_boundaries(monkeypatch)
    spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)

    result = spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)

    assert result["status"] == "evaluated"
    assert result["ok"] is True
    assert result["segments_resolved"] == ["total", "online"]
    assert isolated_state["candidate"] == {}
    assert cdp.call_count == 2
    telegram.assert_not_called()


def test_low_total_and_normal_online_send_one_ads_alert_after_confirmation(
    monkeypatch,
):
    """Total 0.19× алертит, а Online 1.0× молча становится normal-resolved."""
    items = _items(
        online_baseline=100.0,
        online_target=100.0,
        offline_baseline=900.0,
        offline_target=90.0,
    )
    _, telegram = _mock_boundaries(monkeypatch, items=items)
    spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)

    result = spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)

    assert result["alerts_sent"] == 1
    assert result["segments_resolved"] == ["total", "online"]
    telegram.assert_called_once()
    text = telegram.call_args.args[0]
    assert "Итого (включая Онлайн)" in text
    assert "0.19×" in text
    assert "CDP · FB+Google" in text
    assert telegram.call_args.kwargs == {"channel": "ads"}


def test_high_online_is_sent_immediately_without_candidate(monkeypatch, isolated_state):
    """Online 1.51× отправляется с первого полного снимка, total остаётся candidate."""
    items = _items(online_target=151.0)
    _, telegram = _mock_boundaries(monkeypatch, items=items)

    result = spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)

    assert result["status"] == "evaluated"
    assert result["alerts_sent"] == 1
    assert result["segments_resolved"] == ["online"]
    assert set(isolated_state["candidate"]) == {"total"}
    assert "Онлайн" in telegram.call_args.args[0]
    assert "1.51×" in telegram.call_args.args[0]
    assert telegram.call_args.kwargs["channel"] == "ads"


def test_confirmed_zero_sends_no_spend_alert_and_resolves(monkeypatch):
    """Два одинаковых target=$0 превращаются в явный сигнал отсутствия открутки."""
    items = _items(online_target=0.0)
    _, telegram = _mock_boundaries(monkeypatch, items=items)
    spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)

    result = spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)

    assert result["alerts_sent"] == 1
    assert "online" in result["segments_resolved"]
    assert "нет открутки" in telegram.call_args.args[0]
    assert "дважды зафиксировал $0" in telegram.call_args.args[0]
    assert "завершённый день" in telegram.call_args.args[0]
    assert telegram.call_args.kwargs["channel"] == "ads"


def test_unconfirmed_zero_makes_no_financial_claim(monkeypatch, isolated_state):
    """Первый zero только создаёт candidate и ничего не сообщает пользователю."""
    _, telegram = _mock_boundaries(monkeypatch, items=_items(online_target=0.0))

    result = spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)

    assert result["status"] == "awaiting_confirmation"
    assert "online" in isolated_state["candidate"]
    telegram.assert_not_called()


def test_changed_total_fingerprint_restarts_only_total_candidate(
    monkeypatch, isolated_state
):
    """Компенсирующие city-изменения не мешают Online подтвердиться отдельно."""
    original = _items()
    changed = _replace_row(original, TARGET, "CityA", ad_spend=910.0)
    changed = _replace_row(changed, TARGET, "Онлайн", ad_spend=90.0)
    # Возвращаем Online к прежнему значению, а компенсацию делаем вторым offline-городом.
    for day in _dates():
        original.append(
            {"report_date": day.isoformat(), "city": "CityB", "ad_spend": 100.0}
        )
    changed = deepcopy(original)
    changed = _replace_row(changed, TARGET, "CityA", ad_spend=910.0)
    changed = _replace_row(changed, TARGET, "CityB", ad_spend=90.0)

    cdp, telegram = _mock_boundaries(monkeypatch, items=original)
    spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)
    old_total = isolated_state["candidate"]["total"]["fingerprint"]
    online_fingerprint = isolated_state["candidate"]["online"]["fingerprint"]
    cdp.return_value = changed

    result = spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)

    assert result["segments_resolved"] == ["online"]
    assert isolated_state["candidate"]["total"]["fingerprint"] != old_total
    assert isolated_state["candidate"]["total"]["observed_at"] == SECOND_CHECK.isoformat()
    assert online_fingerprint not in {
        candidate["fingerprint"] for candidate in isolated_state["candidate"].values()
    }
    telegram.assert_not_called()


def test_validation_gap_resets_candidate_and_valid_snapshot_starts_again(
    monkeypatch, isolated_state
):
    """valid→missing→valid не даёт ложного confirmation после разрыва."""
    valid = _items()
    missing = [
        row
        for row in valid
        if not (
            row["report_date"] == TARGET.isoformat() and row["city"] == "Онлайн"
        )
    ]
    cdp, _ = _mock_boundaries(monkeypatch, items=valid)
    spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)
    assert "online" in isolated_state["candidate"]

    cdp.return_value = missing
    spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)
    assert "online" not in isolated_state["candidate"]

    cdp.return_value = valid
    result = spend_alerts.run_cdp_spend_alerts(FIRST_CHECK + timedelta(hours=2))

    assert "online" not in result["segments_resolved"]
    assert isolated_state["candidate"]["online"]["observed_at"] == (
        FIRST_CHECK + timedelta(hours=2)
    ).isoformat()


def test_invalid_total_resets_only_total_candidate_and_online_confirms(
    monkeypatch, isolated_state
):
    """Битая чужая city-строка сбрасывает total, но не валидный Online candidate."""
    valid = _items()
    invalid_total = _replace_row(valid, TARGET, "CityA", ad_spend="0")
    cdp, telegram = _mock_boundaries(monkeypatch, items=valid)
    spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)
    online_fingerprint = isolated_state["candidate"]["online"]["fingerprint"]
    cdp.return_value = invalid_total

    result = spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)

    assert result["status"] == "degraded"
    assert result["segments_resolved"] == ["online"]
    assert "total" not in isolated_state["candidate"]
    assert online_fingerprint not in {
        candidate["fingerprint"] for candidate in isolated_state["candidate"].values()
    }
    assert telegram.call_count == 1
    assert "Итого" in telegram.call_args.args[0]
    assert "данные неполные" in telegram.call_args.args[0]


def test_invalid_total_does_not_block_immediate_online_high(monkeypatch):
    """Total data-gap и валидный Online high обрабатываются в одном прогоне."""
    items = _replace_row(_items(online_target=151.0), TARGET, "CityA", ad_spend="0")
    _, telegram = _mock_boundaries(monkeypatch, items=items)

    result = spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)

    assert result["status"] == "degraded"
    assert result["warnings_sent"] == 1
    assert result["alerts_sent"] == 1
    assert result["segments_resolved"] == ["online"]
    assert telegram.call_count == 2
    assert all(call.kwargs["channel"] == "ads" for call in telegram.call_args_list)


def test_baseline_zero_sends_data_warning_not_signal(monkeypatch):
    """Нулевая 7-дневная норма остаётся data-gap и не превращается в high/no-spend."""
    items = _items(
        online_baseline=0.0,
        online_target=100.0,
        offline_baseline=0.0,
        offline_target=0.0,
    )
    _, telegram = _mock_boundaries(monkeypatch, items=items)

    result = spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)

    assert result["status"] == "degraded"
    assert result["alerts_sent"] == 0
    assert result["warnings_sent"] == 2
    assert result["segments_resolved"] == []
    assert all(call.kwargs["channel"] == "ads" for call in telegram.call_args_list)
    assert all("средний расход за 7 baseline-дней равен $0" in call.args[0] for call in telegram.call_args_list)


def test_transport_failure_preserves_candidates_and_sends_one_combined_warning(
    monkeypatch, isolated_state
):
    """CDP transport-error не считается новым снимком и не сбрасывает candidates."""
    cdp, telegram = _mock_boundaries(monkeypatch)
    spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)
    candidates_before = deepcopy(isolated_state["candidate"])
    cdp.side_effect = CdpError("секретный raw transport detail")

    result = spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)

    assert result["status"] == "degraded"
    assert result["warnings_sent"] == 1
    assert isolated_state["candidate"] == candidates_before
    telegram.assert_called_once()
    warning = telegram.call_args.args[0]
    assert "все сегменты" in warning
    assert "CDP daily-report сейчас недоступен" in warning
    assert "секретный raw transport detail" not in warning
    assert telegram.call_args.kwargs == {"channel": "ads"}


def test_empty_response_resets_both_candidates(monkeypatch, isolated_state):
    """Пустой наблюдённый snapshot сбрасывает оба candidates, в отличие от transport-error."""
    cdp, _ = _mock_boundaries(monkeypatch)
    spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)
    assert set(isolated_state["candidate"]) == {"total", "online"}
    cdp.return_value = []

    result = spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)

    assert result["status"] == "degraded"
    assert isolated_state["candidate"] == {}
    assert result["warnings_sent"] == 2


def test_data_warning_is_throttled_for_six_hours_but_cdp_rechecks(
    monkeypatch,
):
    """Gap проверяется каждый час, но один segment warning приходит раз в 6 часов."""
    invalid = _replace_row(_items(), TARGET, "CityA", ad_spend="0")
    cdp, telegram = _mock_boundaries(monkeypatch, items=invalid)

    first = spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)
    second = spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)
    after_six_hours = spend_alerts.run_cdp_spend_alerts(
        FIRST_CHECK + timedelta(hours=6)
    )

    assert first["warnings_sent"] == 1
    assert second["warnings_sent"] == 0
    assert second["alerts_skipped"] == 1
    assert after_six_hours["warnings_sent"] == 1
    assert cdp.call_count == 3
    assert telegram.call_count == 2


def test_failed_signal_is_retried_and_only_success_resolves(monkeypatch, isolated_state):
    """Telegram False не пишет signal dedup и следующий hourly run повторяет high."""
    items = _items(online_target=151.0)
    _, telegram = _mock_boundaries(
        monkeypatch,
        items=items,
        telegram_result=[False, True],
    )

    first = spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)

    assert first["alerts_sent"] == 0
    assert first["alerts_skipped"] == 1
    assert "online" not in first["segments_resolved"]
    assert not any(key.startswith("spend_signal:online") for key in isolated_state["sent"])

    second = spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)

    assert second["alerts_sent"] == 1
    assert "online" in second["segments_resolved"]
    assert telegram.call_count == 2


def test_telegram_exception_is_retried_without_escaping(monkeypatch):
    """Исключение основного бота гасится и не мешает следующей успешной отправке."""
    _, telegram = _mock_boundaries(
        monkeypatch,
        items=_items(online_target=151.0),
        telegram_result=[RuntimeError("telegram down"), True],
    )

    first = spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)
    second = spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)

    assert first["ok"] is False
    assert first["alerts_skipped"] == 1
    assert second["alerts_sent"] == 1
    assert telegram.call_count == 2


def test_existing_signal_dedup_resolves_without_second_send(
    monkeypatch, isolated_state
):
    """Постоянный ключ segment/date подавляет повтор и восстанавливает resolved."""
    isolated_state.update(
        {
            "candidate": {},
            "resolved": {},
            "sent": {
                f"spend_signal:online:{TARGET.isoformat()}": FIRST_CHECK.isoformat()
            },
        }
    )
    _, telegram = _mock_boundaries(monkeypatch, items=_items(online_target=151.0))

    result = spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)

    assert result["alerts_sent"] == 0
    assert result["alerts_skipped"] == 1
    assert result["segments_resolved"] == ["online"]
    telegram.assert_not_called()


def test_two_segments_same_date_have_independent_signal_keys(monkeypatch, isolated_state):
    """Low total и high Online за одну дату создают два независимых сигнала."""
    items = _items(
        online_baseline=10.0,
        online_target=15.1,
        offline_baseline=990.0,
        offline_target=0.0,
    )
    _, telegram = _mock_boundaries(monkeypatch, items=items)
    first = spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)
    second = spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)

    assert first["alerts_sent"] == 1
    assert first["segments_resolved"] == ["online"]
    assert second["alerts_sent"] == 1
    assert second["segments_resolved"] == ["total", "online"]
    assert {
        key
        for key in isolated_state["sent"]
        if key.startswith("spend_signal:")
    } == {
        f"spend_signal:online:{TARGET.isoformat()}",
        f"spend_signal:total:{TARGET.isoformat()}",
    }
    assert telegram.call_count == 2


def test_both_resolved_skips_cdp_for_target_date(monkeypatch, isolated_state):
    """После закрытия total+online повторный крон не читает CDP."""
    isolated_state.update(
        {
            "candidate": {},
            "resolved": {"total": TARGET.isoformat(), "online": TARGET.isoformat()},
            "sent": {},
        }
    )
    cdp, telegram = _mock_boundaries(monkeypatch)

    result = spend_alerts.run_cdp_spend_alerts(SECOND_CHECK)

    assert result["status"] == "already_resolved"
    assert result["segments_resolved"] == ["total", "online"]
    cdp.assert_not_called()
    telegram.assert_not_called()


def test_malformed_state_is_recovered_safely(monkeypatch, isolated_state):
    """Legacy/битые секции state восстанавливаются и не роняют контур."""
    isolated_state.update({"candidate": [], "resolved": "bad", "sent": None})
    _, telegram = _mock_boundaries(monkeypatch)

    result = spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)

    assert result["status"] == "awaiting_confirmation"
    assert set(isolated_state["candidate"]) == {"total", "online"}
    assert isolated_state["resolved"] == {}
    assert isolated_state["sent"] == {}
    telegram.assert_not_called()


def test_only_cdp_daily_report_and_telegram_boundaries_are_used(monkeypatch):
    """Новый контур выполняет один read-only CDP GET и не обращается к FB fallback."""
    cdp, telegram = _mock_boundaries(monkeypatch, items=_items(online_target=151.0))

    result = spend_alerts.run_cdp_spend_alerts(FIRST_CHECK)

    assert result["alerts_sent"] == 1
    cdp.assert_called_once_with(TARGET - timedelta(days=7), TARGET)
    telegram.assert_called_once()
    assert not hasattr(spend_alerts, "facebook")
    assert "budget" not in " ".join(spend_alerts.__dict__).lower()
