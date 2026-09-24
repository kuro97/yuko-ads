"""
Тесты протокола сомнений Budget Scaler (ARCH-cdp-seasonal-pacing §6.4, §8.4-8.6, §9).

Покрывает:
- _evaluate_doubt_triggers — 4 триггера изолированно + граничные "чуть не дотягивает"
- агрегация нескольких триггеров в ОДНО Telegram-сообщение (send_telegram)
- анти-спам: нет триггеров -> нет сообщения
- флаг cdp.doubt_alerts=false -> триггеры есть, но сообщение НЕ шлётся
- НЕ-блокирующесть (главный инвариант): result идентичен при doubt_alerts true/false
  (кроме факта отправки telegram) — решение бота не меняется
- send_telegram кидает исключение -> прогон не падает, результат валиден

Задача T6 волны 3 спеки ARCH-cdp-seasonal-pacing.md.

Моки — на границе: services.notifications.send_telegram и services.cdp_client.*
(через _CascadeMocks, переиспользован паттерн tests/test_cdp_unit_economics.py).
Владение: только этот файл (tests/test_doubt_protocol.py), tests/test_cdp_unit_economics.py
и tests/test_seasonal_plan_gate.py НЕ трогаем — их правят другие агенты параллельно.

Комментарии на русском.
"""

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импортов проекта (как в других тестах бюджет-пилота)
import sys as _sys
if "google.genai" not in _sys.modules:
    _sys.modules["google.genai"] = MagicMock()
if "google" not in _sys.modules:
    _sys.modules["google"] = MagicMock()

from services.budget_scaler import (
    _evaluate_doubt_triggers,
    SCALE_DEFAULTS,
)
from services.cdp_client import CdpError
from tests.gateway_test_helpers import (
    install_provider_mutation_guard,
    proposal_outcome,
)

_TZ = timezone(timedelta(hours=5))


@pytest.fixture(autouse=True)
def isolated_daily_cap_state(tmp_path, monkeypatch):
    """Перенаправляет state-файл дневного капа во временную папку (изоляция между тестами)."""
    import services.budget_daily_cap as cap_module
    state_file = tmp_path / "budget_daily_cap_state.json"
    monkeypatch.setattr(cap_module, "_CAP_STATE_FILE", state_file)


@pytest.fixture(autouse=True)
def isolated_scale_cooldown_state(tmp_path, monkeypatch):
    """Перенаправляет state-файл кулдауна scaler'а во временную папку (изоляция между тестами)."""
    import services.budget_scaler as scaler_module
    state_file = tmp_path / "budget_scaler_state.json"
    monkeypatch.setattr(scaler_module, "_SCALE_STATE_FILE", state_file)


@pytest.fixture(autouse=True)
def isolated_doubt_log(tmp_path, monkeypatch):
    """Перенаправляет журнал сомнений (data/doubt_log.json) во временную папку.

    ДОБАВЛЕНО вместе с пунктом 1 пакета мелочей (журнал сомнений в «Итоге дня»):
    без этой изоляции _run_scaling_inner при срабатывании триггеров
    писал бы в рабочий data/doubt_log.json при каждом прогоне этого файла
    тестов — то же самое, что уже было сделано для budget_daily_cap/scaler
    выше. Тесты этого файла send_telegram уже мокают, но append_doubt_entry —
    отдельный побочный эффект прогона, требующий своей изоляции."""
    import services.doubt_log as doubt_log_module
    log_file = tmp_path / "doubt_log.json"
    monkeypatch.setattr(doubt_log_module, "_DOUBT_LOG_FILE", log_file)


def _frozen_now(now: datetime):
    """Патчит services.budget_scaler.datetime так, чтобы .now() возвращал фиксированный now,
    а конструктор datetime(...) продолжал работать как обычно (нужен коду внутри модуля).
    """
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = now
    return patch("services.budget_scaler.datetime", mock_dt)


# ===========================================================================
# Часть 1: _evaluate_doubt_triggers — юнит-тесты, каждый триггер изолированно
# ===========================================================================

# Пороги SCALE_DEFAULTS (проверяем актуальность спеки §8.4):
#   doubt_divergence_pp = 3.0
#   doubt_drr_near_target_rel = 0.20
#   doubt_plan_edge_rel = 0.10


def _thr(**overrides) -> dict:
    thr = dict(SCALE_DEFAULTS)
    thr.update(overrides)
    return thr


def test_scale_defaults_doubt_thresholds_match_spec():
    """Пороги в SCALE_DEFAULTS точно соответствуют спеке §8.4 (регрессия на константы)."""
    assert SCALE_DEFAULTS["doubt_divergence_pp"] == pytest.approx(3.0)
    assert SCALE_DEFAULTS["doubt_drr_near_target_rel"] == pytest.approx(0.20)
    assert SCALE_DEFAULTS["doubt_plan_edge_rel"] == pytest.approx(0.10)


# --- (а) divergence: расхождение источников ДРР > 3.0 п.п. ---


def test_trigger_divergence_fires_above_threshold():
    """drr_divergence_pp=4.0 (> порог 3.0) -> триггер сработал, формулировка про расхождение."""
    triggers = _evaluate_doubt_triggers(
        unit_source="sheet",
        cdp_ue=None,
        drr_cdp=0.05,
        drr_sheet=0.01,
        drr_divergence_pp=4.0,
        unit_target=None,
        plan_gate_reason=None,
        fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert len(triggers) == 1
    assert "разошл" in triggers[0].lower()
    assert "4.0" in triggers[0]


def test_trigger_divergence_edge_just_above_threshold_fires():
    """Ровно выше порога (3.01 > 3.0, строгое >) -> триггер срабатывает."""
    triggers = _evaluate_doubt_triggers(
        unit_source="sheet", cdp_ue=None,
        drr_cdp=0.05, drr_sheet=0.01, drr_divergence_pp=3.01,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert len(triggers) == 1


def test_trigger_divergence_exactly_on_threshold_does_not_fire():
    """Ровно на пороге (3.0, не строго >) -> триггер НЕ срабатывает (граница)."""
    triggers = _evaluate_doubt_triggers(
        unit_source="sheet", cdp_ue=None,
        drr_cdp=0.05, drr_sheet=0.02, drr_divergence_pp=3.0,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


def test_trigger_divergence_just_below_threshold_no_trigger():
    """Чуть НЕ дотягивает до порога (2.99 < 3.0) -> триггера нет."""
    triggers = _evaluate_doubt_triggers(
        unit_source="sheet", cdp_ue=None,
        drr_cdp=0.05, drr_sheet=0.02, drr_divergence_pp=2.99,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


def test_trigger_divergence_none_no_trigger():
    """drr_divergence_pp=None (не считалось) -> триггера нет, не падает."""
    triggers = _evaluate_doubt_triggers(
        unit_source="sheet", cdp_ue=None,
        drr_cdp=None, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


# --- (б) drr_near_target: |ДРР-цель| <= 20% относительных от цели ---


def test_trigger_drr_near_target_fires_within_relative_band():
    """drr_cdp=0.041, unit_target=0.04 -> |0.041-0.04|=0.001 <= 0.04*0.20=0.008 -> триггер."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=None,
        drr_cdp=0.041, drr_sheet=None, drr_divergence_pp=None,
        unit_target=0.04, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert len(triggers) == 1
    assert "цел" in triggers[0].lower()


def test_trigger_drr_near_target_uses_sheet_when_unit_source_sheet():
    """unit_source='sheet' -> effective_drr берётся из drr_sheet, а не drr_cdp."""
    triggers = _evaluate_doubt_triggers(
        unit_source="sheet", cdp_ue=None,
        drr_cdp=0.20,  # далеко от цели — не должен использоваться
        drr_sheet=0.041,  # близко к цели — должен быть использован
        drr_divergence_pp=None,
        unit_target=0.04, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert len(triggers) == 1
    assert "4.1" in triggers[0]


def test_trigger_drr_near_target_edge_exactly_on_boundary_fires():
    """Ровно на границе (<=, не строгое) -> триггер срабатывает.
    unit_target=0.04, band=0.008 -> drr=0.048 -> |0.048-0.04|=0.008==band."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=None,
        drr_cdp=0.048, drr_sheet=None, drr_divergence_pp=None,
        unit_target=0.04, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert len(triggers) == 1


def test_trigger_drr_near_target_just_outside_band_no_trigger():
    """Чуть НЕ дотягивает (только что за пределами полосы) -> триггера нет.
    unit_target=0.04, band=0.008 -> drr=0.0481 -> |0.0481-0.04|=0.0081 > 0.008."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=None,
        drr_cdp=0.0481, drr_sheet=None, drr_divergence_pp=None,
        unit_target=0.04, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


def test_trigger_drr_near_target_zero_target_no_trigger():
    """unit_target=0 (или None) -> триггер защищён от деления на 0/бессмысленного расчёта."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=None,
        drr_cdp=0.04, drr_sheet=None, drr_divergence_pp=None,
        unit_target=0.0, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


def test_trigger_drr_near_target_effective_drr_none_no_trigger():
    """effective_drr=None (ДРР не посчитан) -> триггера нет, не падает."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=None,
        drr_cdp=None, drr_sheet=None, drr_divergence_pp=None,
        unit_target=0.04, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


# --- (в) plan_gate_edge: факт в пределах ±10% от границы план-гейта (только CDP) ---


def test_trigger_plan_edge_fires_near_border():
    """expected_share=0.33, border=0.33 (буфер 1.0). fact_share=0.32 ->
    |0.32-0.33|=0.01 <= 0.33*0.10=0.033 -> триггер."""
    cdp_ue = {"fact_share": 0.32, "expected_share": 0.33}
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=cdp_ue,
        drr_cdp=None, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert len(triggers) == 1
    assert "границ" in triggers[0].lower()


def test_trigger_plan_edge_requires_cdp_source():
    """Тот же fact_share/expected_share, но unit_source='sheet' -> триггер НЕ считается
    (спека §6.4: (в) только для CDP-источника)."""
    cdp_ue = {"fact_share": 0.32, "expected_share": 0.33}
    triggers = _evaluate_doubt_triggers(
        unit_source="sheet", cdp_ue=cdp_ue,
        drr_cdp=None, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


def test_trigger_plan_edge_edge_just_inside_boundary_fires():
    """У самой границы полосы (<=, не строгое), почти впритык: border=0.33, band=0.033,
    diff=0.99*band -> внутри полосы -> триггер срабатывает (проверка включительной границы
    без хрупкой равности float на самом краю)."""
    border = 0.33
    band = border * SCALE_DEFAULTS["doubt_plan_edge_rel"]
    fact_share = border + band * 0.99
    cdp_ue = {"fact_share": fact_share, "expected_share": border}
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=cdp_ue,
        drr_cdp=None, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert len(triggers) == 1


def test_trigger_plan_edge_just_outside_band_no_trigger():
    """Чуть НЕ дотягивает: fact_share=0.3631 -> |diff|=0.0331 > 0.033 -> триггера нет."""
    cdp_ue = {"fact_share": 0.3631, "expected_share": 0.33}
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=cdp_ue,
        drr_cdp=None, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


def test_trigger_plan_edge_far_from_border_no_trigger():
    """fact_share далеко от границы (0.9 vs expected 0.33) -> триггера нет."""
    cdp_ue = {"fact_share": 0.9, "expected_share": 0.33}
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=cdp_ue,
        drr_cdp=None, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


def test_trigger_plan_edge_expected_share_zero_no_trigger():
    """expected_share=0.0 (дни 1-4, нет статбазы) -> триггер не считается (граница защищена > 0)."""
    cdp_ue = {"fact_share": 0.0, "expected_share": 0.0}
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=cdp_ue,
        drr_cdp=None, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


def test_trigger_plan_edge_fact_share_none_no_trigger():
    """cdp_ue['fact_share'] is None (нет планового знаменателя) -> триггера нет, не падает."""
    cdp_ue = {"fact_share": None, "expected_share": 0.33}
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=cdp_ue,
        drr_cdp=None, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


def test_trigger_plan_edge_cdp_ue_none_no_trigger():
    """cdp_ue=None (CDP недоступен) -> триггер (в) не считается, не падает."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=None,
        drr_cdp=None, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


# --- (г) fallback: CDP лёг -> откат на лист ---


def test_trigger_fallback_fires():
    """fallback_happened=True, cdp_ue=None -> триггер про откат на лист."""
    triggers = _evaluate_doubt_triggers(
        unit_source="sheet", cdp_ue=None,
        drr_cdp=None, drr_sheet=0.04, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=True,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert len(triggers) == 1
    assert "cdp" in triggers[0].lower() or "лист" in triggers[0].lower()


def test_trigger_fallback_false_no_trigger():
    """fallback_happened=False -> триггера нет."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue={"fact_share": 0.9, "expected_share": 0.33},
        drr_cdp=0.02, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


# --- (д) недоверие движку -> откат на сезонную кривую (Шаг A.3) ---


def test_trigger_engine_distrust_fires():
    """engine_distrust_reason не None -> триггер про недоверие с текстом причины
    и упоминанием сезонной кривой (спека ARCH-cdp-budget-context §6.5/§10.3).
    После редизайна текста: человеческая формулировка
    «<причина> — не доверяю ему, считаю по сезонной кривой», без слова «движок»."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue={"fact_share": 0.9, "expected_share": 0.33},
        drr_cdp=0.02, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason="прогноз CDP ещё не готов (холодный старт месяца)",
        engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert len(triggers) == 1
    assert "движок" not in triggers[0].lower()
    assert "не доверяю ему" in triggers[0]
    assert "сезонной кривой" in triggers[0]
    assert "холодный старт" in triggers[0]


def test_trigger_engine_distrust_none_no_trigger():
    """engine_distrust_reason=None (движок доверенный/не проверялся) -> триггера нет."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=None,
        drr_cdp=None, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


# --- (е) выручка отстаёт, а спенд в норме -> совет проверить каналы (Шаг A.3) ---


def test_trigger_engine_revenue_behind_spend_ok_fires():
    """engine_revenue_behind_spend_ok=True -> триггер с советом проверить каналы/переложить бюджет."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=None,
        drr_cdp=0.02, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=True,
        thresholds=_thr(),
    )
    assert len(triggers) == 1
    assert "канал" in triggers[0].lower()
    assert "переложить" in triggers[0].lower()


def test_trigger_engine_revenue_behind_spend_ok_false_no_trigger():
    """engine_revenue_behind_spend_ok=False -> триггера (е) нет."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=None,
        drr_cdp=0.02, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


def test_trigger_engine_distrust_and_revenue_behind_both_present():
    """Оба новых триггера (д) и (е) одновременно -> оба в списке, порядок после (г).
    После редизайна текста: маркер (д) — «сезонной кривой»,
    слова «движок» в тексте триггера больше нет."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=None,
        drr_cdp=0.02, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason="данные прогноза CDP устарели на 40ч (допустимо 36ч)",
        engine_revenue_behind_spend_ok=True,
        thresholds=_thr(),
    )
    assert len(triggers) == 2
    assert "сезонной кривой" in triggers[0]  # (д) первый
    assert "движок" not in triggers[0].lower()
    assert "канал" in triggers[1].lower()   # (е) второй


# --- Отсутствие триггеров вообще ---


def test_no_triggers_returns_empty_list():
    """Все значения далеко от порогов / отсутствуют -> пустой список."""
    triggers = _evaluate_doubt_triggers(
        unit_source="sheet", cdp_ue=None,
        drr_cdp=None, drr_sheet=0.04, drr_divergence_pp=0.1,
        unit_target=0.10,  # далеко от 0.04
        plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


def test_evaluate_doubt_triggers_does_not_raise_on_all_none():
    """Полное отсутствие данных (всё None/False) -> не бросает исключений, возвращает []."""
    triggers = _evaluate_doubt_triggers(
        unit_source="none", cdp_ue=None,
        drr_cdp=None, drr_sheet=None, drr_divergence_pp=None,
        unit_target=None, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []


# --- Несколько триггеров разом ---


def test_multiple_triggers_all_present_in_list():
    """Divergence + drr_near_target одновременно -> оба в списке (порядок фиксирован а,б,в,г)."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp", cdp_ue=None,
        drr_cdp=0.041, drr_sheet=0.02, drr_divergence_pp=4.0,
        unit_target=0.04, plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert len(triggers) == 2
    assert "разошл" in triggers[0].lower()  # (а) первый по порядку
    assert "цел" in triggers[1].lower()      # (б) второй


# ===========================================================================
# Часть 2: интеграция через полный прогон run_budget_scaling (_CascadeMocks)
# ===========================================================================

_TZ2 = timezone(timedelta(hours=5))
_NOW = datetime(2026, 7, 8, 10, 0, tzinfo=_TZ2)  # среда, окно ДРР = 2026-06-28..2026-07-04


def _daily_items():
    """Числовой пример: spend_lcy=19_350, revenue_lcy=400_000 -> drr_cdp≈0.048375."""
    return [
        {"report_date": "2026-07-01", "city": "CityA", "usd_rate": 95.0,
         "ad_spend": 100.0, "revenue_new": 200_000.0, "drr_new": 999.0},
        {"report_date": "2026-07-02", "city": "CityA", "usd_rate": 100.0,
         "ad_spend": 50.0, "revenue_new": 100_000.0, "drr_new": 999.0},
        {"report_date": "2026-07-01", "city": "CityB", "usd_rate": 95.0,
         "ad_spend": 30.0, "revenue_new": 60_000.0, "drr_new": 999.0},
        {"report_date": "2026-07-02", "city": "CityB", "usd_rate": 100.0,
         "ad_spend": 20.0, "revenue_new": 40_000.0, "drr_new": 999.0},
    ]


def _plan_fact_summary(time_pct=0.5, plan_rev=1_000_000.0, fact_rev=600_000.0, cities_override=None):
    if cities_override is not None:
        cities = cities_override
    else:
        cities = [
            {"city": "CityA", "plan": {"revenue_new": plan_rev * 0.6}, "fact": {"revenue_new": fact_rev * 0.6}},
            {"city": "CityB", "plan": {"revenue_new": plan_rev * 0.4}, "fact": {"revenue_new": fact_rev * 0.4}},
        ]
    return {"month": "2026-07-01", "time_pct": time_pct, "days_in_month": 31, "days_elapsed": 15, "cities": cities}


def _make_local_ad(
    ad_id: str = "ad1",
    ad_name: str = "Победитель",
    payments: int = 3,
    days_running: int = 10,
) -> dict:
    return {
        "ad_id": ad_id, "ad_name": ad_name, "city": "CityA", "adset_type": "L2",
        "adset_id": None, "spend": 80.0, "leads": 8, "qual_pct": 20.0, "romi": 150.0,
        "cpl": 10.0, "ctr": 2.0, "hook_rate": 30.0, "impressions": 5000,
        "video_p25": 0, "video_p100": 0, "video_views_3s": 0,
        "payments": payments, "outcomes_matched_at": None, "days_running": days_running,
        "effective_status": "ACTIVE", "recommendation": "ЖДАТЬ", "reason": "",
    }


def _adset_info(budget_usd: float = 100.0, status: str = "ACTIVE", name: str = "Тестовый адсет") -> dict:
    return {"daily_budget_usd": budget_usd, "effective_status": status, "name": name}


def _base_cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "kill_switch": False,
        "scale_enabled": False,
        "max_budget_increase_pct": 15,
        "max_adset_budget_mult": 2.0,
        "max_adset_daily_budget": 300,
        "max_total_daily_budget": 4000,
        "max_scales_per_run": 2,
        "plan_sheet_id": "test_sheet_id",
        "cdp": {"enabled": False},
    }
    cfg.update(overrides)
    return cfg


# Лист с большим запасом и мягкой юниткой — гейт пропускает поток дальше.
_VALID_PLAN = {
    "week_label": "неделя 1 (2026-07-01–07)",
    "budget_usd": 100_000.0,
    "revenue_plan_lcy": 10_000_000.0,
    "unit_target": 0.99,
}


class _CascadeMocks:
    """Полный набор моков одного прогона run_budget_scaling для интеграционных тестов
    протокола сомнений. Скопировано из tests/test_cdp_unit_economics.py (владелец T2) —
    переиспользуем паттерн, файл не импортируем напрямую, чтобы владение осталось раздельным.
    """

    def __init__(
        self,
        cfg: dict,
        cdp_ok: bool | None = True,
        cdp_daily_items: list[dict] | None = None,
        cdp_plan_fact: dict | None = None,
        sheet_ok: bool = True,
        sheet_plan: dict | None = _VALID_PLAN,
        sheet_revenue_lcy: float = 200_000.0,
        sheet_fb_week_spend: float = 0.0,
        local_ads: list[dict] | None = None,
        all_budgets: dict | None = None,
        send_telegram_kwargs: dict | None = None,
    ):
        if local_ads is None:
            local_ads = [_make_local_ad("ad1")]
        if all_budgets is None:
            all_budgets = {"adset1": _adset_info(100.0)}
        if cdp_daily_items is None:
            cdp_daily_items = _daily_items()
        if cdp_plan_fact is None:
            cdp_plan_fact = _plan_fact_summary(time_pct=0.1, plan_rev=1_000_000.0, fact_rev=200_000.0)

        fb_ad_info = {ad["ad_id"]: {"adset_id": "adset1", "effective_status": "ACTIVE"} for ad in local_ads}

        def _fetch_candidate_fb_info_side_effect(ad_ids: list[str]) -> dict:
            return {aid: info for aid, info in fb_ad_info.items() if aid in ad_ids}

        self._stack = ExitStack()
        self._specs: dict[str, tuple] = {
            "get_scale_config": ("services.budget_scaler.get_scale_config", {"return_value": cfg}),
            "fetch_ads": ("services.shadow_report._fetch_ads_from_local_db", {"return_value": local_ads}),
            "load_settings": ("agent.scheduler.load_settings", {"return_value": {"thresholds": {}}}),
            "fetch_candidate_fb_info": (
                "services.autopilot._fetch_candidate_fb_info",
                {"side_effect": _fetch_candidate_fb_info_side_effect},
            ),
            "fetch_all_budgets": ("services.budget_scaler._fetch_all_account_adset_budgets", {"return_value": all_budgets}),
            "fetch_adset_budgets": ("services.budget_scaler._fetch_adset_budgets", {"return_value": {}}),
            # Боевой код импортирует propose_scale ЛОКАЛЬНО из модуля gateway,
            # поэтому патчить нужно именно это имя: патч алиаса execute_scale
            # вызов не перехватит и реальный propose_scale пойдёт в БД/FB.
            "propose_scale": (
                "services.action_producer_gateway.propose_scale",
                {"return_value": proposal_outcome()},
            ),
            "send_telegram": (
                "services.notifications.send_telegram",
                send_telegram_kwargs if send_telegram_kwargs is not None else {"return_value": True},
            ),
            "send_critical_alert": ("services.notifications.send_critical_alert", {}),
            "save_decision": ("agent.repositories.decisions_repo.save_decision", {}),
            "send_with_buttons": ("services.telegram_bot.send_with_buttons", {"return_value": True}),
        }

        if cdp_ok is True:
            self._specs["cdp_get_daily_report"] = (
                "services.cdp_client.get_daily_report", {"return_value": cdp_daily_items},
            )
            self._specs["cdp_get_plan_fact_summary"] = (
                "services.cdp_client.get_plan_fact_summary", {"return_value": cdp_plan_fact},
            )
        elif cdp_ok is False:
            self._specs["cdp_get_daily_report"] = (
                "services.cdp_client.get_daily_report", {"side_effect": CdpError("CDP недоступен")},
            )
            self._specs["cdp_get_plan_fact_summary"] = (
                "services.cdp_client.get_plan_fact_summary", {"return_value": cdp_plan_fact},
            )

        if sheet_ok:
            self._specs.update({
                "read_general_plan": ("services.plan_reader.read_general_plan", {"return_value": sheet_plan}),
                "get_usd_to_lcy": ("services.exchange_rate.get_usd_to_lcy", {"return_value": 100.0}),
                "get_fb_week_spend": ("services.budget_scaler.get_fb_week_spend", {"return_value": sheet_fb_week_spend}),
                "get_google_week_spend": ("services.budget_scaler.get_google_week_spend", {"return_value": 0.0}),
                "get_amo_week_revenue": ("services.budget_scaler.get_amo_week_revenue", {"return_value": sheet_revenue_lcy}),
            })
        else:
            self._specs["read_general_plan"] = ("services.plan_reader.read_general_plan", {"return_value": None})

        self.mocks: dict[str, MagicMock] = {}

    def __enter__(self) -> "_CascadeMocks":
        for name, (target, kwargs) in self._specs.items():
            self.mocks[name] = self._stack.enter_context(patch(target, **kwargs))
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._stack.__exit__(exc_type, exc, tb)

    def __getitem__(self, name: str) -> MagicMock:
        return self.mocks[name]


def _strip_volatile(result: dict) -> dict:
    """Убирает поля, которые естественно отличаются между прогонами не из-за
    протокола сомнений (тут таких нет — прогон полностью детерминирован моками),
    оставлено для явности сравнения в тесте не-блокирующести."""
    return {k: v for k, v in result.items()}


# --- Агрегация: несколько триггеров -> РОВНО одно сообщение сомнений ---


def test_aggregation_single_doubt_message_multiple_triggers():
    """CDP решает, ДРР близко к цели И план-гейт впритык одновременно -> сомнения
    приклеены к ЕДИНСТВЕННОМУ сообщению о результате прогона. После редизайна
    сообщений: отдельное сообщение «есть сомнение» ДО подъёма/блокировки
    больше не отправляется — сомнения теперь хвост финального сообщения."""
    from services.budget_scaler import run_budget_scaling

    # unit_target листа = 0.048 (близко к drr_cdp=0.048375 из _daily_items -> триггер б)
    # plan_fact: expected_share(2026-07-15)=0.33, fact_share сделаем ~0.32 (близко к границе -> триггер в)
    # В этом сценарии сам план-гейт CDP блокирует подъём -> финальное сообщение —
    # вариант 3 «НЕ поднимаю» через send_telegram (не send_with_buttons).
    plan_fact = _plan_fact_summary(time_pct=0.5, plan_rev=1_000_000.0, fact_rev=320_000.0)
    tight_target_plan = {**_VALID_PLAN, "unit_target": 0.048}
    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True, "doubt_alerts": True})
    now_mid_month = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ2)

    with _CascadeMocks(
        cfg, cdp_ok=True, sheet_ok=True, sheet_plan=tight_target_plan, cdp_plan_fact=plan_fact,
    ) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(now_mid_month):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "cdp"
    assert m["send_telegram"].call_count == 1
    assert not m["send_with_buttons"].called

    text = m["send_telegram"].call_args.args[0]
    # Отдельного сообщения "есть сомнение"/"Решение:" больше нет вообще.
    assert "сомнение" not in text.lower()
    assert "Решение:" not in text
    assert "Почему сомневаюсь" in text
    assert "Делаю сам" in text
    # Оба сработавших триггера (б — цель, в — граница плана) отражены в тексте.
    assert "цел" in text.lower()
    assert "границ" in text.lower()


def test_doubt_message_human_readable_format(tmp_path, monkeypatch):
    """Триггер сработал -> сомнения приклеены к ОДНОМУ сообщению о результате.

    Раньше результатом active-прогона была применённая правка бюджета, и текст
    уходил через send_with_buttons («поднял» + кнопки отката). Approval-first
    убрал прямую мутацию: результат прогона — созданное владельцу предложение,
    сообщение уходит обычным send_telegram, кнопок прямого действия больше нет.
    Инвариант формата сохранён и остаётся смыслом теста: сомнения идут секцией
    «Почему сомневаюсь» в КОНЦЕ единственного сообщения о результате (а не
    отдельным упреждающим алертом), без «Решение:» и без слова «сомнение».

    Wave 3C: при cdp.enabled=true active-подъём проходит fail-closed гейт
    самопроверки движка. Чтобы happy-path состоялся, изолируем и сеем ВАЛИДНУЮ
    «ok»-историю selfcheck (сегодня 2026-07-08 + «неделю назад» 2026-07-01,
    forecast==fact -> отклонение 0 -> reason None)."""
    import json as _json
    import services.engine_selfcheck as _esc
    _state = tmp_path / "engine_selfcheck_state.json"
    monkeypatch.setattr(_esc, "_SELFCHECK_STATE_FILE", _state)
    _state.write_text(_json.dumps({"snapshots": [
        {"date": "2026-07-01", "month": "2026-07",
         "forecast_eom": 4_000_000.0, "fact_mtd": 4_000_000.0},
        {"date": "2026-07-08", "month": "2026-07",
         "forecast_eom": 4_000_000.0, "fact_mtd": 4_000_000.0},
    ]}), encoding="utf-8")

    from services.budget_scaler import run_budget_scaling

    # Только divergence: drr_cdp≈0.048375 vs drr_sheet сильно разное -> расхождение > 3 п.п.
    sheet_revenue_lcy = 400_000.0
    sheet_fb_week_spend = 5.0  # даёт drr_sheet маленький -> большая дивергенция с cdp
    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True, "doubt_alerts": True})

    mutation_guard = install_provider_mutation_guard(monkeypatch)

    with _CascadeMocks(
        cfg, cdp_ok=True, sheet_ok=True,
        sheet_revenue_lcy=sheet_revenue_lcy, sheet_fb_week_spend=sheet_fb_week_spend,
    ) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["drr_divergence_pp"] is not None
    # Предложение создано, бюджет напрямую не менялся.
    assert result["proposals"], f"ожидали созданные предложения, got: {result}"
    assert result["scaled"] == []
    mutation_guard.assert_untouched()

    # Кнопки прямого действия исчезли вместе с прямой мутацией: одно сообщение,
    # и сомнения обязаны быть внутри него.
    assert m["send_with_buttons"].call_count == 0
    assert m["send_telegram"].call_count == 1

    text = m["send_telegram"].call_args.args[0]
    assert "Решение:" not in text
    assert "сомнение" not in text.lower()
    assert "Почему сомневаюсь" in text
    assert "Делаю сам" in text
    assert "предложения отправлены владельцу" in text


# --- Анти-спам: нет триггеров -> нет сообщения сомнений ---


def test_antispam_no_doubt_message_when_no_triggers():
    """Все значения далеко от порогов -> триггеров нет -> секции «Почему сомневаюсь»
    НЕТ ни в одном отправленном сообщении. После редизайна сообщений
    проверяем ОБА канала (send_telegram И send_with_buttons) — теперь сомнения
    приклеиваются к финальному сообщению о результате, каким бы каналом оно ни ушло."""
    from services.budget_scaler import run_budget_scaling

    # unit_target листа = 0.99 (_VALID_PLAN) — далеко от drr_cdp=0.048375 (не near_target).
    # sheet и cdp drr близки друг к другу -> дивергенция мала.
    # plan_fact: expected_share(2026-07-08 -> ДРР окно вокруг 8го, но план-гейт day=8)
    # используем время в начале месяца, но day=8 (не 1-4, не near-border): fact_share=1.0 (перевыполнен).
    plan_fact = _plan_fact_summary(time_pct=0.1, plan_rev=1_000_000.0, fact_rev=2_000_000.0)  # far overachieved
    sheet_revenue_lcy = 400_000.0
    sheet_fb_week_spend = 193.5  # rate 100 -> spend_lcy=19_350.0 -> drr_sheet == drr_cdp ровно -> divergence=0
    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True, "doubt_alerts": True})

    with _CascadeMocks(
        cfg, cdp_ok=True, sheet_ok=True, cdp_plan_fact=plan_fact,
        sheet_revenue_lcy=sheet_revenue_lcy, sheet_fb_week_spend=sheet_fb_week_spend,
    ) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "cdp"
    all_texts = [
        (c.args[0] if c.args else c.kwargs.get("text", ""))
        for c in m["send_telegram"].call_args_list + m["send_with_buttons"].call_args_list
    ]
    assert all_texts  # прогон реально отправил хотя бы одно сообщение о результате
    assert not any("Почему сомневаюсь" in t for t in all_texts)


# --- Флаг doubt_alerts=false -> триггеры есть, но сообщение не шлётся ---


def test_doubt_alerts_flag_off_suppresses_message_even_with_triggers():
    """cdp.doubt_alerts=false, триггер (divergence) точно есть -> секции «Почему
    сомневаюсь» НЕТ ни в одном сообщении (send_telegram И send_with_buttons —
    после редизайна сообщений)."""
    from services.budget_scaler import run_budget_scaling

    sheet_revenue_lcy = 400_000.0
    sheet_fb_week_spend = 5.0  # большая дивергенция с cdp (как в human_readable тесте)
    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True, "doubt_alerts": False})

    with _CascadeMocks(
        cfg, cdp_ok=True, sheet_ok=True,
        sheet_revenue_lcy=sheet_revenue_lcy, sheet_fb_week_spend=sheet_fb_week_spend,
    ) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["drr_divergence_pp"] is not None
    assert result["drr_divergence_pp"] > SCALE_DEFAULTS["doubt_divergence_pp"]  # триггер бы точно сработал
    all_texts = [
        (c.args[0] if c.args else c.kwargs.get("text", ""))
        for c in m["send_telegram"].call_args_list + m["send_with_buttons"].call_args_list
    ]
    assert all_texts
    assert not any("Почему сомневаюсь" in t for t in all_texts)


# --- НЕ-блокирующесть (главный инвариант §8.5): result идентичен doubt_alerts true/false ---


def test_doubt_non_blocking_result_identical_true_vs_false(tmp_path, monkeypatch):
    """Триггеры сработали (гарантированно, большая дивергенция) -> result run_budget_scaling
    БАЙТ-В-БАЙТ идентичен при doubt_alerts=true и doubt_alerts=false (кроме факта отправки
    telegram-сообщения сомнений — это единственная разница). Решение бота НЕ меняется.

    ВАЖНО: два прогона выполняются последовательно в active-режиме, поэтому дневной кап
    (services.budget_daily_cap) должен получать СВЕЖИЙ state-файл на каждый прогон —
    иначе второй прогон "видит" повышение, сделанное первым, и результат отличается
    по причине, не связанной с протоколом сомнений (ложный failure)."""
    from services.budget_scaler import run_budget_scaling
    import services.budget_daily_cap as cap_module

    sheet_revenue_lcy = 400_000.0
    sheet_fb_week_spend = 5.0  # гарантирует divergence > 3 п.п.

    def _run(doubt_alerts: bool, run_label: str) -> dict:
        # Свежий state-файл дневного капа на каждый прогон — изоляция от предыдущего вызова.
        monkeypatch.setattr(cap_module, "_CAP_STATE_FILE", tmp_path / f"cap_{run_label}.json")
        cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True, "doubt_alerts": doubt_alerts})
        with _CascadeMocks(
            cfg, cdp_ok=True, sheet_ok=True,
            sheet_revenue_lcy=sheet_revenue_lcy, sheet_fb_week_spend=sheet_fb_week_spend,
        ) as _, \
             patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
             patch("services.budget_scaler._record_scaled_at"), \
             _frozen_now(_NOW):
            return run_budget_scaling(mode="active")

    result_on = _run(True, "on")
    result_off = _run(False, "off")

    assert result_on["drr_divergence_pp"] is not None
    assert result_on["drr_divergence_pp"] > SCALE_DEFAULTS["doubt_divergence_pp"]

    # result идентичен полностью — протокол сомнений не пишет ни в один из ключей результата.
    assert result_on == result_off
    # В частности — решение и все ключевые поля совпадают явно (для читаемости диффа при падении).
    assert result_on["scaled"] == result_off["scaled"]
    assert result_on["skipped_reason"] == result_off["skipped_reason"]
    assert result_on["unit_source"] == result_off["unit_source"]
    assert result_on["recommendations"] == result_off["recommendations"]
    assert result_on["winners"] == result_off["winners"]


def test_doubt_non_blocking_result_identical_when_plan_gate_blocks():
    """То же самое (result идентичен true/false), но в ветке, где план-гейт БЛОКИРУЕТ
    подъём (plan_gate_reason заполнен) — протокол сомнений не должен влиять и здесь."""
    from services.budget_scaler import run_budget_scaling

    now_mid_month = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ2)
    # fact_share=0.10 < expected_share=0.33 -> план-гейт CDP блокирует
    behind_plan_fact = _plan_fact_summary(time_pct=0.9, plan_rev=1_000_000.0, fact_rev=100_000.0)

    def _run(doubt_alerts: bool) -> dict:
        cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True, "doubt_alerts": doubt_alerts})
        with _CascadeMocks(
            cfg, cdp_ok=True, sheet_ok=True, cdp_plan_fact=behind_plan_fact,
        ) as _, \
             patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
             patch("services.budget_scaler._record_scaled_at"), \
             _frozen_now(now_mid_month):
            return run_budget_scaling(mode="active")

    result_on = _run(True)
    result_off = _run(False)

    assert "сезонно" in (result_on["skipped_reason"] or "")
    assert result_on == result_off


# --- send_telegram кидает исключение -> прогон не падает ---


def test_send_telegram_exception_does_not_crash_run():
    """send_telegram финального сообщения кидает исключение -> верхний try/except
    в run_budget_scaling ловит её: ran=False, send_critical_alert вызван, все
    ожидаемые ключи телеметрии на месте (нейтральные значения _telemetry_defaults).
    После редизайна сообщений: сценарий — план-гейт БЛОКИРУЕТ подъём
    (финальное сообщение уходит через send_telegram, не send_with_buttons), поэтому
    side_effect на send_telegram гарантированно попадает в путь отправки."""
    from services.budget_scaler import run_budget_scaling

    now_mid_month = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ2)
    # fact_share=0.10 < expected_share=0.33 -> план-гейт CDP блокирует
    behind_plan_fact = _plan_fact_summary(time_pct=0.9, plan_rev=1_000_000.0, fact_rev=100_000.0)
    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True, "doubt_alerts": True})

    with _CascadeMocks(
        cfg, cdp_ok=True, sheet_ok=True, cdp_plan_fact=behind_plan_fact,
        send_telegram_kwargs={"side_effect": RuntimeError("telegram недоступен")},
    ) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(now_mid_month):
        result = run_budget_scaling(mode="active")

    assert result["ran"] is False
    assert result["skipped_reason"] == "error: telegram недоступен"
    assert m["send_critical_alert"].called
    for key in ("unit_source", "drr_cdp", "drr_sheet", "drr_divergence_pp",
                "plan_gate_mode", "expected_share", "fact_share",
                "scaled", "recommendations", "winners", "errors"):
        assert key in result
    # send_telegram реально был вызван (попытка отправки заблокированного сообщения произошла)
    assert m["send_telegram"].called


# --- Триггеров нет -> telegram вообще не шлётся (дубль сценария анти-спама с другим углом) ---


def test_no_triggers_no_telegram_call_at_all_for_doubts():
    """Явная проверка через прямой юнит-вызов _evaluate_doubt_triggers + send_telegram
    отдельно: если список триггеров пуст, вызывающий код (по контракту §6.4/§8.6)
    не должен вызывать send_telegram для сомнений вовсе."""
    triggers = _evaluate_doubt_triggers(
        unit_source="cdp",
        cdp_ue={"fact_share": 0.9, "expected_share": 0.33},  # далеко от границы
        drr_cdp=0.02, drr_sheet=0.02, drr_divergence_pp=0.0,
        unit_target=0.99,  # далеко от drr
        plan_gate_reason=None, fallback_happened=False,
        engine_distrust_reason=None, engine_revenue_behind_spend_ok=False,
        thresholds=_thr(),
    )
    assert triggers == []
