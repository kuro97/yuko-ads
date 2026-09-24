"""
Смоук-тесты фазы 2 «живые этапы встреч AMO» (ARCH-hold-meetings, T3).

Покрываем (см. docs/specs/ARCH-hold-meetings.md §9):
1. integrations.amo.count_meetings_by_ad — подсчёт встреч по ad_id (scheduled/held,
   матч по fb_ad_id/fb_ad_name, исключение служебных лидов, дедуп).
2. services.autopilot_hold.should_hold — условие (в) расширенное встречами
   (реализовано в T2, здесь — регресс-проверка на границах из спеки).
3. services.autopilot_hold.make_hold_entry — снимок встреч в HoldEntry.
4. services.autopilot_hold.check_hold_expired — семантика роста оплат (встречи
   НЕ продлевают удержание); при meetings_at_hold > 0 текст причины паузы
   обогащается числом встреч (ARCH-hold-meetings §9/AC-10), при 0/отсутствии —
   старый текст фазы 1 (обратная совместимость).
5. integrations.amo.calc_ad_metrics — meetings=None не ломает вызов (дефолт 0).
6. web.settings_validation.validate_settings_update — hold_min_meetings 1..20.

Мокать не нужно (чистые функции), кроме сценария недоступности AMO (monkeypatch).
Существующие тесты (test_autopilot_hold.py, test_settings_validation.py) НЕ трогаем.
"""

import itertools
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.autopilot_hold as ah
from integrations.amo import calc_ad_metrics, count_meetings_by_ad
from web.settings_validation import validate_settings_update

# Пример status_id этапов встреч воронки "Новые продажи" (config.py, §5 спеки)
MEETING_SCHEDULED_1 = 31239894  # ВСТРЕЧА НАЗНАЧЕНА
MEETING_SCHEDULED_2 = 34482950  # Встреча ПОДТВЕРЖДЕНА
MEETING_HELD = 44446055         # ВСТРЕЧА СОСТОЯЛАСЬ

_lead_id_seq = itertools.count(1)


def _lead(status_id=None, fb_ad_id=None, fb_ad_name=None, tags=None, lead_id=None) -> dict:
    """Собирает тестовый RAW-лид AMO с нужными полями (fb_ad_id/fb_ad_name/tags)."""
    custom_fields = []
    if fb_ad_id is not None:
        custom_fields.append({"field_name": "fb_ad_id", "values": [{"value": fb_ad_id}]})
    if fb_ad_name is not None:
        custom_fields.append({"field_name": "fb_ad_name", "values": [{"value": fb_ad_name}]})
    return {
        "id": lead_id if lead_id is not None else next(_lead_id_seq),
        "status_id": status_id,
        "custom_fields": custom_fields,
        "tags": tags or [],
    }


# ---------------------------------------------------------------------------
# count_meetings_by_ad
# ---------------------------------------------------------------------------

def test_count_meetings_by_ad_scheduled_and_held():
    """4 лида на MEETING_SCHEDULED_1 + 1 лид на MEETING_HELD, все с fb_ad_id=X в known_ad_ids."""
    leads = [_lead(status_id=MEETING_SCHEDULED_1, fb_ad_id="X") for _ in range(4)]
    leads.append(_lead(status_id=MEETING_HELD, fb_ad_id="X"))
    result = count_meetings_by_ad(leads, known_ad_ids={"X"})
    assert result == {"X": {"meetings_scheduled": 4, "meetings_held": 1}}


def test_count_meetings_by_ad_confirmed_counts_as_scheduled():
    """Статус 'Встреча ПОДТВЕРЖДЕНА' (34482950) попадает в meetings_scheduled, не held."""
    leads = [_lead(status_id=MEETING_SCHEDULED_2, fb_ad_id="X")]
    result = count_meetings_by_ad(leads, known_ad_ids={"X"})
    assert result["X"]["meetings_scheduled"] == 1
    assert result["X"]["meetings_held"] == 0


def test_count_meetings_by_ad_fallback_by_name():
    """Лид без fb_ad_id, матчится по fb_ad_name через fb_lookup."""
    leads = [_lead(status_id=MEETING_SCHEDULED_1, fb_ad_name="Тест")]
    result = count_meetings_by_ad(leads, fb_lookup={"тест": "X"})
    assert result == {"X": {"meetings_scheduled": 1, "meetings_held": 0}}


def test_count_meetings_by_ad_excludes_avtosdelka():
    """Лид с тегом 'Автосделка' не считается во встречах."""
    leads = [_lead(status_id=MEETING_SCHEDULED_1, fb_ad_id="X", tags=[{"id": 1, "name": "Автосделка"}])]
    result = count_meetings_by_ad(leads, known_ad_ids={"X"})
    assert result == {}


def test_count_meetings_by_ad_excludes_rassylka_waba():
    """Лид с тегом 'Рассылка Waba' не считается во встречах (регистронезависимо)."""
    leads = [_lead(status_id=MEETING_HELD, fb_ad_id="X", tags=[{"id": 1, "name": "рассылка WABA"}])]
    result = count_meetings_by_ad(leads, known_ad_ids={"X"})
    assert result == {}


def test_count_meetings_by_ad_dedup_by_lead_id():
    """Два одинаковых lead_id — считаем один раз."""
    leads = [
        _lead(status_id=MEETING_SCHEDULED_1, fb_ad_id="X", lead_id=42),
        _lead(status_id=MEETING_SCHEDULED_1, fb_ad_id="X", lead_id=42),
    ]
    result = count_meetings_by_ad(leads, known_ad_ids={"X"})
    assert result["X"]["meetings_scheduled"] == 1


def test_count_meetings_by_ad_no_meetings_returns_empty():
    """Лиды в статусе 'новый' (не встреча) — пустой dict."""
    leads = [_lead(status_id=30, fb_ad_id="X")]
    result = count_meetings_by_ad(leads, known_ad_ids={"X"})
    assert result == {}


def test_count_meetings_by_ad_missing_status_id_skipped():
    """Лид без status_id — пропуск, пустой dict."""
    leads = [_lead(status_id=None, fb_ad_id="X")]
    result = count_meetings_by_ad(leads, known_ad_ids={"X"})
    assert result == {}


# ---------------------------------------------------------------------------
# calc_ad_metrics — совместимость meetings=None
# ---------------------------------------------------------------------------

def test_calc_ad_metrics_meetings_none_defaults_to_zero(monkeypatch):
    """meetings не передан (None) — у метрик meetings_scheduled=0, meetings_held=0."""
    monkeypatch.setattr("services.exchange_rate.get_usd_to_lcy", lambda: 100.0)
    matched = {"X": {"total": 1, "quals": 0, "payments": 0, "revenue": 0}}
    result = calc_ad_metrics(matched, ad_spends={"X": 10})
    assert result["X"]["meetings_scheduled"] == 0
    assert result["X"]["meetings_held"] == 0


# ---------------------------------------------------------------------------
# should_hold — условие (в) со встречами (регресс-проверка границ из спеки)
# ---------------------------------------------------------------------------

def _decision(**overrides) -> dict:
    d = {
        "ad_id": "120210000000000001",
        "ad_name": "CityD | отзыв клиента",
        "action": "PAUSE",
        "reasons": ["PAUSE: портфельный аутсайдер (В нижней части ранга, есть лучше)"],
        "is_confirmed_waster": False,
        "is_early_waster": False,
    }
    d.update(overrides)
    return d


def _metrics(**overrides) -> dict:
    m = {"spend": 640.0, "romi": 184.0, "qual_pct": 22.0, "payments": 0}
    m.update(overrides)
    return m


def _cfg(**overrides) -> dict:
    cfg = dict(ah.HOLD_DEFAULTS)
    cfg["hold_enabled"] = True
    cfg.update(overrides)
    return cfg


def test_should_hold_by_meetings_when_qual_and_payments_insufficient():
    """payments=0, qual=10 (<15), meetings_scheduled=2, meetings_held=0, hold_min_meetings=2 → держим."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=10.0, meetings_scheduled=2, meetings_held=0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


def test_should_hold_not_enough_meetings():
    """meetings_scheduled=1 (<2) — нет потенциала оплат."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=10.0, meetings_scheduled=1, meetings_held=0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "нет потенциала оплат"


def test_should_hold_meetings_none_degrades():
    """meetings_*=None → деградация до payments/qual фазы 1."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=10.0, meetings_scheduled=None, meetings_held=None)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "нет потенциала оплат"


def test_should_hold_by_qual_when_meetings_zero_phase1_not_broken():
    """qual=15 (>=15), meetings=0 — держим по qual (фаза 1 не сломана)."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=15.0, meetings_scheduled=0, meetings_held=0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


def test_should_hold_meetings_scheduled_plus_held_sum():
    """scheduled=1 + held=1 == hold_min_meetings=2 (суммируются) → держим."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=0.0, meetings_scheduled=1, meetings_held=1)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


def test_should_hold_meetings_exactly_at_default_threshold():
    """scheduled=2, hold_min_meetings=2 (граница ровно) → держим."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=0.0, meetings_scheduled=2, meetings_held=0)
    ok, reason = ah.should_hold(decision, metrics, _cfg(hold_min_meetings=2))
    assert ok is True
    assert reason == ""


# ---------------------------------------------------------------------------
# make_hold_entry — снимок встреч
# ---------------------------------------------------------------------------

def test_make_hold_entry_meetings_saved():
    """metrics с meetings_scheduled=4, held=1 → entry содержит их и сумму meetings_at_hold=5."""
    decision = _decision()
    metrics = _metrics(meetings_scheduled=4, meetings_held=1)
    entry = ah.make_hold_entry(decision, metrics, _cfg())
    assert entry["meetings_scheduled"] == 4
    assert entry["meetings_held"] == 1
    assert entry["meetings_at_hold"] == 5


# ---------------------------------------------------------------------------
# check_hold_expired — семантика роста оплат (встречи не продлевают hold),
# но текст причины паузы обогащается числом встреч (ARCH-hold-meetings §9/AC-10).
# ---------------------------------------------------------------------------

def test_check_hold_expired_reason_enriched_with_meetings_at_hold():
    """entry с meetings_at_hold=5, оплат нет → pause с текстом, обогащённым числом встреч.
    РЕШЕНИЕ не меняется (по-прежнему pause, т.к. оплаты не выросли) — меняется только текст."""
    state = {"holds": {"ad1": {"payments_at_hold": 0, "meetings_at_hold": 5}}}
    status, reason = ah.check_hold_expired("ad1", {"payments": 0}, state)
    assert status == "pause"
    assert reason == "удержание истекло: было 5 встреч, оплат нет (было 0, стало 0)"


def test_check_hold_expired_meetings_at_hold_zero_old_text():
    """entry с meetings_at_hold=0 (был передан явно, но встреч не было) — старый текст
    фазы 1 без упоминания встреч (обратная совместимость)."""
    state = {"holds": {"ad1": {"payments_at_hold": 0, "meetings_at_hold": 0}}}
    status, reason = ah.check_hold_expired("ad1", {"payments": 0}, state)
    assert status == "pause"
    assert reason == "удержание истекло, оплат нет (было 0, стало 0)"


def test_check_hold_expired_without_meetings_old_text():
    """entry без meetings_at_hold вовсе (старая запись до фазы 2) — старый текст фазы 1 (регресс)."""
    state = {"holds": {"ad1": {"payments_at_hold": 0}}}
    status, reason = ah.check_hold_expired("ad1", {"payments": 0}, state)
    assert status == "pause"
    assert reason == "удержание истекло, оплат нет (было 0, стало 0)"


def test_check_hold_expired_payments_grew_survives_regardless_of_meetings():
    """current payments > payments_at_hold → survived, встречи не важны (решение не меняется)."""
    state = {"holds": {"ad1": {"payments_at_hold": 0, "meetings_at_hold": 5}}}
    status, reason = ah.check_hold_expired("ad1", {"payments": 2}, state)
    assert status == "survived"
    assert reason == ""


# ---------------------------------------------------------------------------
# Валидация hold_min_meetings (web/settings_validation.py)
# ---------------------------------------------------------------------------

def test_validate_hold_min_meetings_happy_path():
    """hold_min_meetings=3 — валидно, сохраняется в autopilot."""
    result = validate_settings_update({"autopilot": {"hold_min_meetings": 3}}, {})
    assert result["autopilot"]["hold_min_meetings"] == 3


def test_validate_hold_min_meetings_lower_boundary_ok():
    """hold_min_meetings=1 (нижняя граница, включительно) — валидно."""
    result = validate_settings_update({"autopilot": {"hold_min_meetings": 1}}, {})
    assert result["autopilot"]["hold_min_meetings"] == 1


def test_validate_hold_min_meetings_upper_boundary_ok():
    """hold_min_meetings=20 (верхняя граница, включительно) — валидно."""
    result = validate_settings_update({"autopilot": {"hold_min_meetings": 20}}, {})
    assert result["autopilot"]["hold_min_meetings"] == 20


def test_validate_hold_min_meetings_zero_raises_400():
    """hold_min_meetings=0 (ниже границы) — 400."""
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"hold_min_meetings": 0}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "hold_min_meetings должен быть от 1 до 20"


def test_validate_hold_min_meetings_21_raises_400():
    """hold_min_meetings=21 (выше границы) — 400."""
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"hold_min_meetings": 21}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "hold_min_meetings должен быть от 1 до 20"


def test_validate_hold_min_meetings_wrong_type_raises_400():
    """hold_min_meetings='abc' (не число) — 400 с текстом про int."""
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"hold_min_meetings": "abc"}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "hold_min_meetings должен быть int"


def test_validate_hold_min_meetings_merges_over_existing():
    """hold_min_meetings мержится поверх текущего autopilot-блока, другие ключи не теряются."""
    current = {"autopilot": {"hold_days": 7, "hold_enabled": True}}
    result = validate_settings_update({"autopilot": {"hold_min_meetings": 5}}, current)
    assert result["autopilot"]["hold_min_meetings"] == 5
    assert result["autopilot"]["hold_days"] == 7
    assert result["autopilot"]["hold_enabled"] is True
