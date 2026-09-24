"""
Юнит-тесты «Правила удержания» автопилота (services/autopilot_hold.py).

Покрываем:
1. should_hold — все правила с ранними return (0-7), включая границы (0.8, 800).
   Включая багфикс (решение владельца): ROMI-гейт применим ТОЛЬКО
   при payments>0 — без оплат ROMI всегда 0 и неинформативен.
2. check_hold_expired — рост оплат → survived, отсутствие роста/None → pause.
3. load_hold_state / save_hold_state — round-trip, атомарность, ролловер, битый JSON.
4. is_held — границы (будущее/прошлое).
5. make_hold_entry — состав полей.

Без sleep. Файловый стейт — через monkeypatch HOLD_STATE_FILE на tmp_path.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.autopilot_hold as ah

_TZ = timezone(timedelta(hours=5))


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_hold_state(tmp_path, monkeypatch):
    """Перенаправляет HOLD_STATE_FILE на tmp_path для изоляции тестов от реального стейта."""
    state_path = tmp_path / "autopilot_hold_state.json"
    monkeypatch.setattr(ah, "HOLD_STATE_FILE", state_path)
    yield state_path


def _cfg(**overrides) -> dict:
    """Конфиг автопилота с дефолтами hold, для should_hold."""
    cfg = dict(ah.HOLD_DEFAULTS)
    cfg["hold_enabled"] = True  # для большинства тестов включаем явно
    cfg.update(overrides)
    return cfg


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


# ---------------------------------------------------------------------------
# should_hold — happy path и границы
# ---------------------------------------------------------------------------

def test_should_hold_all_conditions_met_holds():
    """Кейс владельца: romi=184/target=200/spend=512(<=800)/payments=1 → держим."""
    decision = _decision()
    metrics = _metrics(spend=512.0, romi=184.0, payments=1, qual_pct=None)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


def test_should_hold_romi_far_from_target_does_not_hold():
    """payments=1 (ROMI-гейт применим), romi=88/target=200 → 0.44 < 0.8 → не держим.

    РЕШЕНИЕ ВЛАДЕЛЬЦА: раньше этот тест ставил payments=0 (дефолт
    _metrics) и всё равно ждал отказ по ROMI — это и есть починенный баг
    (ROMI-гейт неприменим без оплат). Переписано на payments=1, чтобы тест
    по-прежнему проверял ROMI-гейт там, где он реально применяется.
    """
    decision = _decision()
    metrics = _metrics(romi=88.0, payments=1)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "ROMI далёк от цели"


def test_should_hold_romi_exactly_on_ratio_boundary_holds():
    """romi=160/target=200 → 0.8 >= 0.8 (включительно) → держим."""
    decision = _decision()
    metrics = _metrics(romi=160.0, spend=100.0, payments=1)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


def test_should_hold_spend_over_max_does_not_hold():
    """spend=801 (>800) → не держим."""
    decision = _decision()
    metrics = _metrics(spend=801.0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "расход большой"


def test_should_hold_spend_exactly_on_boundary_holds():
    """spend=800 (<=800, включительно) → держим (при прочих условиях ок)."""
    decision = _decision()
    metrics = _metrics(spend=800.0, romi=184.0, payments=1)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


def test_should_hold_no_potential_does_not_hold():
    """payments=0, qual=10 (<15) → нет потенциала оплат."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=10.0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "нет потенциала оплат"


def test_should_hold_potential_via_payments_holds():
    """payments=1, qual=None → держим (условие в выполнено через payments)."""
    decision = _decision()
    metrics = _metrics(payments=1, qual_pct=None)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


def test_should_hold_potential_via_qual_holds():
    """payments=0, qual=15 (>=15, включительно) → держим (условие в выполнено через qual)."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=15.0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


# ---------------------------------------------------------------------------
# should_hold — БАГФИКС (решение владельца): ROMI-гейт неприменим
# при payments==0, т.к. ROMI без единой оплаты всегда 0 и неинформативен.
# Пример: spend $50, несколько лидов, qual 30%, payments=0, romi=0 —
# ранговое правило запаузило рекламу, hold не сработал «ROMI далёк от цели».
# ---------------------------------------------------------------------------

def test_should_hold_cityd_case_payments_zero_qual_good_holds():
    """Пример: payments=0, qual_pct=30%, spend=50, romi=0
    → ROMI-гейт пропускается (payments=0), spend<=800 (б) и qual>=15 (в)
    выполнены → держим. До фикса это же сочетание давало отказ «ROMI далёк от
    цели», хотя ROMI без оплат в принципе не может быть близок к цели."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=30.0, spend=50.0, romi=0.0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


def test_should_hold_payments_zero_no_qual_no_meetings_no_potential():
    """payments=0, qual_pct=0, встреч=0 → нет потенциала оплат (ROMI-гейт
    пропущен, но условие (в) всё равно не выполнено)."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=0.0, spend=50.0, romi=0.0,
                        meetings_scheduled=0, meetings_held=0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "нет потенциала оплат"


def test_should_hold_payments_zero_qual_ok_spend_over_max_does_not_hold():
    """payments=0, qual_pct=20% (>=15), но spend=900 (>800) → отказ по расходу.
    ROMI-гейт пропущен (payments=0), но условие (б) режет раньше, чем (в)."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=20.0, spend=900.0, romi=0.0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "расход большой"


def test_should_hold_payments_zero_meetings_potential_holds():
    """payments=0, встреч=2 (>=hold_min_meetings), qual_pct=0 → держим по
    потенциалу встреч (ROMI-гейт пропущен, т.к. payments=0)."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=0.0, spend=50.0, romi=0.0,
                        meetings_scheduled=2, meetings_held=0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


def test_should_hold_payments_nonzero_romi_far_from_target_still_rejected():
    """payments=1 (оплата уже есть), romi=50 (далёк от цели 200*0.8=160) →
    ROMI-гейт по-прежнему обязателен и режет — старое поведение при payments>0
    НЕ затронуто фиксом."""
    decision = _decision()
    metrics = _metrics(payments=1, romi=50.0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "ROMI далёк от цели"


def test_should_hold_payments_nonzero_happy_path_still_holds():
    """payments=1, romi=170 (170/200=0.85>=0.8), qual_pct=20, spend=500 (<=800)
    → держим — старый happy path при payments>0 жив (регресс-защита)."""
    decision = _decision()
    metrics = _metrics(payments=1, romi=170.0, qual_pct=20.0, spend=500.0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


# ---------------------------------------------------------------------------
# should_hold — ранние return (владелец: слив/ранний сигнал ДО ранговой проверки)
# ---------------------------------------------------------------------------

def test_should_hold_confirmed_waster_does_not_hold():
    """is_confirmed_waster=True (остальное ок) → слив, не держим."""
    decision = _decision(is_confirmed_waster=True)
    metrics = _metrics()
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "слив — не держим"


def test_should_hold_confirmed_waster_checked_before_rank_reason():
    """Слив проверяется ДО ранговой проверки: даже если reasons не ранговые, причина — слив."""
    decision = _decision(is_confirmed_waster=True, reasons=["PAUSE: wasted_no_crm"])
    metrics = _metrics()
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "слив — не держим"


def test_should_hold_early_waster_does_not_hold():
    """is_early_waster=True (день 1-3) → не держим."""
    decision = _decision(is_early_waster=True)
    metrics = _metrics()
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "ранний сигнал — не держим"


def test_should_hold_not_rank_reason_does_not_hold():
    """reasons без 'портфельный аутсайдер' (например qual_pct=0) → не ранговое правило."""
    decision = _decision(reasons=["PAUSE: есть лиды, но qual_pct=0"])
    metrics = _metrics()
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "не ранговое правило"


def test_should_hold_no_romi_does_not_hold():
    """payments=1 (ROMI-гейт применим), romi=None → нет ROMI, не держим.

    РЕШЕНИЕ ВЛАДЕЛЬЦА: раньше payments=0 (дефолт _metrics) + romi=None
    тоже давали отказ «нет ROMI» — это удалённое поведение (без оплат ROMI-гейт
    неприменим вовсе, romi=None в таком случае не мешает держать). Переписано на
    payments=1, чтобы тест проверял «нет ROMI» там, где гейт реально работает.
    """
    decision = _decision()
    metrics = _metrics(romi=None, payments=1)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "нет ROMI"


def test_should_hold_disabled_does_not_hold():
    """hold_enabled=False → удержание выключено, не держим (это правило проверяется первым)."""
    decision = _decision()
    metrics = _metrics()
    ok, reason = ah.should_hold(decision, metrics, _cfg(hold_enabled=False))
    assert ok is False
    assert reason == "удержание выключено"


def test_should_hold_disabled_checked_before_waster():
    """hold_enabled=False проверяется раньше слива/раннего сигнала (правило 0 первое)."""
    decision = _decision(is_confirmed_waster=True)
    metrics = _metrics()
    ok, reason = ah.should_hold(decision, metrics, _cfg(hold_enabled=False))
    assert ok is False
    assert reason == "удержание выключено"


def test_should_hold_zero_romi_target_does_not_hold():
    """payments=1 (ROMI-гейт применим), hold_romi_target=0 (защита от деления
    на 0) → нет цели ROMI.

    РЕШЕНИЕ ВЛАДЕЛЬЦА: раньше payments=0 (дефолт _metrics) тоже
    падал на этой проверке — удалённое поведение (без оплат условие (а), включая
    защиту от деления на 0, вообще не проверяется). Переписано на payments=1.
    """
    decision = _decision()
    metrics = _metrics(payments=1)
    ok, reason = ah.should_hold(decision, metrics, _cfg(hold_romi_target=0))
    assert ok is False
    assert reason == "нет цели ROMI"


# ---------------------------------------------------------------------------
# check_hold_expired
# ---------------------------------------------------------------------------

def test_check_hold_expired_no_growth_pauses():
    """payments_at_hold=0, current payments=0 → pause с точным текстом (было 0, стало 0)."""
    state = {"holds": {"ad1": {"payments_at_hold": 0}}}
    status, reason = ah.check_hold_expired("ad1", {"payments": 0}, state)
    assert status == "pause"
    assert reason == "удержание истекло, оплат нет (было 0, стало 0)"


def test_check_hold_expired_payments_grew_survives():
    """payments_at_hold=0, current payments=2 → survived, сброс."""
    state = {"holds": {"ad1": {"payments_at_hold": 0}}}
    status, reason = ah.check_hold_expired("ad1", {"payments": 2}, state)
    assert status == "survived"
    assert reason == ""


def test_check_hold_expired_payments_none_pauses():
    """current payments=None → трактуем как 0 → pause (безопаснее закрыть удержание паузой)."""
    state = {"holds": {"ad1": {"payments_at_hold": 0}}}
    status, reason = ah.check_hold_expired("ad1", {"payments": None}, state)
    assert status == "pause"
    assert "было 0, стало 0" in reason


def test_check_hold_expired_missing_payments_at_hold_defaults_to_zero():
    """payments_at_hold отсутствует в записи → трактуем как 0."""
    state = {"holds": {"ad1": {}}}
    status, reason = ah.check_hold_expired("ad1", {"payments": 1}, state)
    assert status == "survived"


def test_check_hold_expired_does_not_mutate_state():
    """check_hold_expired не удаляет запись из state (ответственность вызывающего)."""
    state = {"holds": {"ad1": {"payments_at_hold": 0}}}
    ah.check_hold_expired("ad1", {"payments": 5}, state)
    assert "ad1" in state["holds"]


# ---------------------------------------------------------------------------
# is_held — границы
# ---------------------------------------------------------------------------

def test_is_held_active_hold_returns_true():
    """hold_until в будущем → is_held True."""
    future = (datetime.now(_TZ) + timedelta(days=3)).isoformat()
    state = {"holds": {"ad1": {"hold_until": future}}}
    assert ah.is_held("ad1", state) is True


def test_is_held_expired_hold_returns_false():
    """hold_until в прошлом → is_held False."""
    past = (datetime.now(_TZ) - timedelta(days=1)).isoformat()
    state = {"holds": {"ad1": {"hold_until": past}}}
    assert ah.is_held("ad1", state) is False


def test_is_held_missing_ad_id_returns_false():
    """ad_id отсутствует в state — is_held False."""
    state = {"holds": {}}
    assert ah.is_held("ad_missing", state) is False


# ---------------------------------------------------------------------------
# load_hold_state / save_hold_state — round-trip, атомарность, ролловер
# ---------------------------------------------------------------------------

def test_load_hold_state_missing_file_returns_empty():
    """Файла нет → {'holds': {}}."""
    state = ah.load_hold_state()
    assert state == {"holds": {}}


def test_save_and_load_round_trip(isolate_hold_state):
    """save_hold_state -> load_hold_state: запись на месте, поля совпадают."""
    entry = {
        "ad_name": "CityD | отзыв клиента",
        "held_at": "2026-07-03T10:00:00+05:00",
        "hold_until": "2026-07-10T10:00:00+05:00",
        "payments_at_hold": 0,
        "romi": 184.0,
        "qual_pct": 22.0,
        "spend": 640.0,
        "reason": "PAUSE: портфельный аутсайдер",
    }
    ah.save_hold_state({"holds": {"120210000000000001": entry}})

    loaded = ah.load_hold_state()
    assert "120210000000000001" in loaded["holds"]
    assert loaded["holds"]["120210000000000001"] == entry


def test_save_hold_state_is_atomic_no_corrupted_file(isolate_hold_state):
    """save_hold_state пишет через tmp+replace: итоговый файл не битый и не остаётся tmp-мусора."""
    ah.save_hold_state({"holds": {"ad1": {"hold_until": "2026-07-10T10:00:00+05:00"}}})

    assert isolate_hold_state.exists()
    # tmp-файл не должен остаться после replace
    tmp_path = isolate_hold_state.with_suffix(".json.tmp")
    assert not tmp_path.exists()
    # файл валиден и парсится
    data = json.loads(isolate_hold_state.read_text(encoding="utf-8"))
    assert data["holds"]["ad1"]["hold_until"] == "2026-07-10T10:00:00+05:00"


def test_load_hold_state_rollover_drops_entry_without_hold_until(isolate_hold_state):
    """Запись без hold_until в файле — load_hold_state выкидывает её при ролловере."""
    isolate_hold_state.parent.mkdir(parents=True, exist_ok=True)
    isolate_hold_state.write_text(
        json.dumps(
            {
                "holds": {
                    "ad_bad": {"ad_name": "без hold_until"},
                    "ad_good": {"hold_until": "2026-07-10T10:00:00+05:00"},
                }
            }
        ),
        encoding="utf-8",
    )
    state = ah.load_hold_state()
    assert "ad_bad" not in state["holds"]
    assert "ad_good" in state["holds"]


def test_load_hold_state_rollover_drops_entry_with_unparseable_hold_until(isolate_hold_state):
    """Запись с непарсящимся hold_until — тоже выкидывается при ролловере."""
    isolate_hold_state.parent.mkdir(parents=True, exist_ok=True)
    isolate_hold_state.write_text(
        json.dumps({"holds": {"ad_bad": {"hold_until": "не-дата"}}}),
        encoding="utf-8",
    )
    state = ah.load_hold_state()
    assert "ad_bad" not in state["holds"]


def test_load_hold_state_corrupted_json_returns_empty_without_crash(isolate_hold_state):
    """Битый JSON → лог warning + пустой стейт, без падения."""
    isolate_hold_state.parent.mkdir(parents=True, exist_ok=True)
    isolate_hold_state.write_text("{не валидный json...", encoding="utf-8")

    state = ah.load_hold_state()
    assert state == {"holds": {}}


# ---------------------------------------------------------------------------
# make_hold_entry
# ---------------------------------------------------------------------------

def test_make_hold_entry_fields():
    """hold_until = held_at + hold_days; payments_at_hold корректен; поля совпадают со спекой."""
    decision = _decision()
    metrics = _metrics(spend=640.0, romi=184.0, qual_pct=22.0, payments=0)
    cfg = _cfg(hold_days=7)

    before = datetime.now(_TZ)
    entry = ah.make_hold_entry(decision, metrics, cfg)
    after = datetime.now(_TZ)

    assert entry["ad_name"] == decision["ad_name"]
    assert entry["payments_at_hold"] == 0
    assert entry["romi"] == 184.0
    assert entry["qual_pct"] == 22.0
    assert entry["spend"] == 640.0
    assert entry["reason"] == "; ".join(decision["reasons"])

    held_at = datetime.fromisoformat(entry["held_at"])
    hold_until = datetime.fromisoformat(entry["hold_until"])
    assert before <= held_at <= after
    assert hold_until - held_at == timedelta(days=7)


def test_make_hold_entry_payments_none_defaults_to_zero():
    """payments=None в метриках -> payments_at_hold=0 (int(None or 0))."""
    decision = _decision()
    metrics = _metrics(payments=None)
    entry = ah.make_hold_entry(decision, metrics, _cfg())
    assert entry["payments_at_hold"] == 0


# ---------------------------------------------------------------------------
# should_hold — условие (в) расширено встречами (ARCH-hold-meetings, фаза 2)
# ---------------------------------------------------------------------------

def test_should_hold_potential_via_meetings_exactly_at_threshold_holds():
    """payments=0, qual=10 (<15), meetings_scheduled=2 (== hold_min_meetings=2) → держим."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=10.0, meetings_scheduled=2, meetings_held=0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


def test_should_hold_meetings_below_threshold_does_not_hold():
    """meetings_scheduled=1 (< hold_min_meetings=2), payments=0, qual=10 (<15) → нет потенциала."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=10.0, meetings_scheduled=1, meetings_held=0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "нет потенциала оплат"


def test_should_hold_meetings_none_degrades_to_phase1():
    """meetings_scheduled/meetings_held=None (гейт «FB-лидов 0») → трактуются как 0,
    условие деградирует до payments/qual фазы 1 — не держим при payments=0, qual<15."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=10.0, meetings_scheduled=None, meetings_held=None)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "нет потенциала оплат"


def test_should_hold_meetings_missing_fields_degrades_to_phase1():
    """meetings_scheduled/meetings_held отсутствуют в ad_metrics вовсе (старый вызов до фазы 2)
    → трактуются как 0, поведение идентично фазе 1."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=15.0)  # без ключей meetings_*
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True  # держим по qual — фаза 1 не сломана
    assert reason == ""


def test_should_hold_qual_potential_still_holds_when_meetings_zero():
    """qual=15 (>=15), meetings=0 → держим по qual, как в фазе 1 (регресс-защита)."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=15.0, meetings_scheduled=0, meetings_held=0)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


def test_should_hold_meetings_scheduled_and_held_sum():
    """scheduled=1 + held=1 == hold_min_meetings=2 (суммируются) → держим."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=0.0, meetings_scheduled=1, meetings_held=1)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is True
    assert reason == ""


def test_should_hold_meetings_but_romi_far_still_pauses():
    """payments=1 (ROMI-гейт применим), встречи есть в достатке, но ROMI далёк от
    цели — условие (а) проверяется раньше условия (в) и режет независимо от
    встреч (слив по ROMI не спасают встречи).

    РЕШЕНИЕ ВЛАДЕЛЬЦА: раньше тест держал payments=0 и всё равно ждал
    отказ по ROMI — удалённое поведение (без оплат ROMI-гейт неприменим, при
    payments=0 и достаточных встречах этот кейс теперь HOLD, см.
    test_should_hold_cityd_case_payments_zero_qual_good_holds). Переписано на
    payments=1, чтобы тест по-прежнему проверял «ROMI режет независимо от
    встреч» именно там, где гейт применяется.
    """
    decision = _decision()
    metrics = _metrics(romi=10.0, payments=1, qual_pct=0.0, meetings_scheduled=5, meetings_held=5)
    ok, reason = ah.should_hold(decision, metrics, _cfg())
    assert ok is False
    assert reason == "ROMI далёк от цели"


def test_should_hold_custom_hold_min_meetings_threshold():
    """hold_min_meetings из cfg переопределён (5) — встреч=4 (<5) не хватает, держать нельзя."""
    decision = _decision()
    metrics = _metrics(payments=0, qual_pct=0.0, meetings_scheduled=4, meetings_held=0)
    ok, reason = ah.should_hold(decision, metrics, _cfg(hold_min_meetings=5))
    assert ok is False
    assert reason == "нет потенциала оплат"


# ---------------------------------------------------------------------------
# make_hold_entry — снимок встреч (ARCH-hold-meetings, фаза 2)
# ---------------------------------------------------------------------------

def test_make_hold_entry_saves_meetings_snapshot():
    """metrics с meetings_scheduled=4, meetings_held=1 → entry содержит их и сумму meetings_at_hold=5."""
    decision = _decision()
    metrics = _metrics(meetings_scheduled=4, meetings_held=1)
    entry = ah.make_hold_entry(decision, metrics, _cfg())
    assert entry["meetings_scheduled"] == 4
    assert entry["meetings_held"] == 1
    assert entry["meetings_at_hold"] == 5


def test_make_hold_entry_meetings_missing_defaults_to_zero():
    """metrics без ключей meetings_* → entry содержит нули (совместимость со старыми вызовами)."""
    decision = _decision()
    metrics = _metrics()  # без meetings_*
    entry = ah.make_hold_entry(decision, metrics, _cfg())
    assert entry["meetings_scheduled"] == 0
    assert entry["meetings_held"] == 0
    assert entry["meetings_at_hold"] == 0
