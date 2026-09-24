"""
Сквозные (интеграционные) тесты run_autopilot_live с «Правилом удержания»
(ARCH-rank-pause-hold, T5).

Проверяем поведение _run_live_inner (Шаг 5.5) целиком: разбиение кандидатов
на to_pause/to_hold, запись/чтение hold-стейта, истечение удержания, fail-safe
при ошибке hold-блока, дневной счётчик пауз и Telegram-секцию «Держу».

Проект approval-first: автопилот не мутирует Facebook, а создаёт владельцу
PAUSE-proposal. Поэтому «кандидат запаузен» в этих тестах значит «на него создано
предложение» (result["proposals"] + план в producer_boundary), а `result["paused"]`
всегда пуст. Логика удержания от этого не меняется: удержанный кандидат не должен
получить даже предложения — иначе владелец увидит кнопку «отключить» на рекламу,
которой система сама дала отсрочку.

Внешние границы мокаются (как в tests/test_autopilot_daily_cap.py):
- services.shadow_report._fetch_ads_from_local_db — локальная БД
- services.decision_policy.score_and_decide — ранговое решение
- services.autopilot._fetch_candidate_fb_info — свежая проверка FB
- services.action_producer_gateway (inventory/exact-контексты + запись proposal) —
  producer-граница; реальные FB-мутаторы закрыты Mock'ами
- agent.repositories.decisions_repo.save_decision — SQLite
- services.notifications.send_telegram / send_critical_alert
- services.telegram_bot.send_with_buttons

hold-стейт и дневной стейт паузы изолируются через monkeypatch путей на tmp_path,
как в isolate_hold_state (test_autopilot_hold.py) и isolate_live_daily_state
(test_autopilot_daily_cap.py).
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import install_proposal_recorder

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта (как в test_autopilot_daily_cap.py)
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())

_TZ = timezone(timedelta(hours=5))


# ---------------------------------------------------------------------------
# Фикстуры: изоляция файловых стейтов (hold + дневной лимит live)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_state_files(tmp_path, monkeypatch):
    """Перенаправляет HOLD_STATE_FILE (autopilot_hold.py) и
    _LIVE_DAILY_STATE_FILE (autopilot.py) на tmp_path — тесты не трогают
    реальные data/*.json и не зависят друг от друга."""
    import services.autopilot as ap
    import services.autopilot_hold as ah

    hold_state_path = tmp_path / "autopilot_hold_state.json"
    daily_state_path = tmp_path / "autopilot_live_daily.json"

    monkeypatch.setattr(ah, "HOLD_STATE_FILE", hold_state_path)
    # autopilot.py импортирует функции из autopilot_hold лениво (внутри функции),
    # поэтому патчим модуль-источник — импорт внутри _run_live_inner увидит патч.
    monkeypatch.setattr(ap, "_LIVE_DAILY_STATE_FILE", daily_state_path)

    yield {"hold": hold_state_path, "daily": daily_state_path}


@pytest.fixture(autouse=True)
def producer_boundary(tmp_path, monkeypatch):
    """Producer-граница: живой guard/discovery, запись плана вместо БД, запрет мутаций.

    Инвентарь producer читает через `adset_pause_guard.fetch_pause_inventory`,
    который каждый тест подменяет своей картиной adset'а. Так guard и producer
    видят один мир — last-active guard проверяется по-настоящему, а не вхолостую.
    """
    from services import adset_pause_guard

    def current_inventory(ad_ids):
        return adset_pause_guard.fetch_pause_inventory(list(ad_ids))

    def fake_exact_contexts(ad_ids, *, require_names=True):
        del require_names
        wanted = {str(ad_id) for ad_id in ad_ids}
        contexts = {
            ad_id: context
            for inventory in current_inventory(ad_ids).values()
            for ad_id, context in (inventory.get("candidate_context") or {}).items()
            if ad_id in wanted
        }
        return contexts, None

    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory", current_inventory
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts", fake_exact_contexts
    )
    return install_proposal_recorder(monkeypatch, tmp_path)


# ---------------------------------------------------------------------------
# Хелперы конфигурации/данных
# ---------------------------------------------------------------------------

def _cfg(hold_enabled: bool, **overrides) -> dict:
    """Полный конфиг автопилота (guardian + hold_* пороги) для _run_live_inner."""
    cfg = {
        "enabled": True,
        "kill_switch": False,
        "mode": "active",
        "max_pauses_per_run": 8,
        "max_pauses_per_day": 6,
        "min_days_protect": 0,
        "guardian": {},
        # hold-пороги (дефолты из спеки)
        "hold_enabled": hold_enabled,
        "hold_romi_target": 200,
        "hold_romi_ratio": 0.8,
        "hold_spend_max": 800,
        "hold_min_qual_pct": 15,
        "hold_days": 7,
    }
    cfg.update(overrides)
    return cfg


def _rank_candidate(ad_id="ad_hold", ad_name="CityD | отзыв клиента", **overrides) -> dict:
    """Ранговый кандидат PAUSE («портфельный аутсайдер»), удовлетворяющий hold-условиям
    из кейса владельца: romi=184/target=200, spend=512, payments=1."""
    d = {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "action": "PAUSE",
        "score": -1,
        "reasons": ["PAUSE: портфельный аутсайдер (В нижней части ранга, есть лучше)"],
        "is_confirmed_waster": False,
        "is_early_waster": False,
    }
    d.update(overrides)
    return d


def _waster_candidate(ad_id="ad_waster", ad_name="Слив | баннер") -> dict:
    """Подтверждённый слив — паузится всегда, удержание его не спасает."""
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "action": "PAUSE",
        "score": -5,
        "reasons": ["PAUSE: подтверждённый слив (0 оплат, spend>150)"],
        "is_confirmed_waster": True,
        "is_early_waster": False,
    }


def _local_ad(ad_id, ad_name, spend=512.0, romi=184.0, qual_pct=None, payments=1, days_running=30):
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "spend": spend,
        "romi": romi,
        "qual_pct": qual_pct,
        "payments": payments,
        "days_running": days_running,
    }


def _run(cfg: dict, local_ads: list[dict], decisions: list[dict], fb_info: dict,
          no_replacement_ids: set[str] | None = None):
    """Запускает run_autopilot_live с замоканными внешними границами.

    Созданные предложения собирает фикстура producer_boundary — здесь только
    result и моки уведомлений/БД.
    """
    from services.autopilot import run_autopilot_live
    no_replacement_ids = no_replacement_ids or set()

    candidates_by_adset: dict[str, list[str]] = {}
    for ad_id, info in fb_info.items():
        if info.get("effective_status") == "ACTIVE" and info.get("adset_id"):
            candidates_by_adset.setdefault(info["adset_id"], []).append(ad_id)

    pause_inventories = {}
    for adset_id, ad_ids in candidates_by_adset.items():
        active_ids = set(ad_ids)
        # Legacy hold-сценарии проверяют не guard, поэтому явно моделируем
        # существующую ACTIVE-замену. Lone-waster кейсы отключают её параметром.
        if len(ad_ids) == 1 and ad_ids[0] not in no_replacement_ids:
            active_ids.add(f"replacement-{ad_ids[0]}")
        pause_inventories[adset_id] = {
            "adset_id": adset_id,
            "active_ids": active_ids,
            "candidate_context": {
                ad_id: {
                    "ad_id": ad_id,
                    "adset_id": adset_id,
                    "configured_status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
                for ad_id in ad_ids
            },
            "inventory_context": {
                current_id: {
                    "ad_id": current_id,
                    "adset_id": adset_id,
                    "name": current_id,
                    "configured_status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
                for current_id in active_ids
            },
            "complete": True,
            "pages_read": 1,
            "error": None,
        }

    with patch("services.autopilot.get_autopilot_config", return_value=cfg), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("services.adset_pause_guard.fetch_pause_inventory", return_value=pause_inventories), \
         patch("agent.repositories.decisions_repo.save_decision") as mock_save, \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram") as mock_telegram, \
         patch("services.notifications.send_critical_alert") as mock_alert, \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.telegram_bot.send_with_buttons", return_value=False) as mock_buttons:
        result = run_autopilot_live(max_pauses=cfg["max_pauses_per_run"], trigger="cron")

    return {
        "result": result,
        "mock_save": mock_save,
        "mock_telegram": mock_telegram,
        "mock_alert": mock_alert,
        "mock_buttons": mock_buttons,
    }


def _fb_active(*ad_ids, adset_id="adset_shared") -> dict:
    """Каждому ad_id — отдельный adset, чтобы guardrail никого не защищал
    (кандидат единственный в своём adset → защищать нечего, кроме случая
    когда явно нужно несколько кандидатов в одном adset)."""
    return {ad_id: {"adset_id": f"{adset_id}_{ad_id}", "effective_status": "ACTIVE"} for ad_id in ad_ids}


def _guardrail_filler(ad_id="ad_filler", ad_name="Заполнитель adset (лучший score)") -> dict:
    """Второй не-слив кандидат с заведомо лучшим score в том же adset, что и
    тестируемый кандидат.

    Guardrail (Шаг 5 в _run_live_inner) защищает ЛУЧШЕГО не-слив-кандидата
    в каждом adset — если тестируемый кандидат один в своём adset, он сам
    становится guardrail-защищённым и вообще не доходит до hold-логики.
    Добавляя «заполнителя» с более высоким score в ТОТ ЖЕ adset, мы отдаём
    guardrail-защиту ему, а тестируемый кандидат проходит guardrail и
    попадает в final_list (что и требуется для проверки hold-логики)."""
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "action": "PAUSE",
        "score": 100,  # заведомо лучший score — guardrail выберет именно его
        "reasons": ["PAUSE: портфельный аутсайдер (заполнитель для guardrail)"],
        "is_confirmed_waster": False,
        "is_early_waster": False,
    }


# ---------------------------------------------------------------------------
# Сценарий 1: hold_enabled=True, ранговый кандидат (romi 184/200, spend 512,
# payments 1) → удержание, pause_ad НЕ вызван, held в результате, запись в
# hold-стейте, дневной счётчик пауз не растёт.
# ---------------------------------------------------------------------------

def test_hold_enabled_rank_candidate_goes_to_held_not_paused(isolate_state_files, producer_boundary):
    cfg = _cfg(hold_enabled=True)
    decision = _rank_candidate()
    filler = _guardrail_filler()
    local_ads = [
        _local_ad("ad_hold", "CityD | отзыв клиента", spend=512.0, romi=184.0, payments=1),
        _local_ad("ad_filler", filler["ad_name"], spend=50.0, romi=500.0, payments=0),
    ]
    # Тот же adset — guardrail защитит filler (лучший score), ad_hold пройдёт дальше
    fb_info = {
        "ad_hold": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_filler": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
    }

    out = _run(cfg, local_ads, [decision, filler], fb_info)
    result = out["result"]

    assert result["ran"] is True, f"Ожидали ran=True, got: {result}"
    # Удержанный кандидат не должен получить даже предложения владельцу:
    # система решила дать ему отсрочку, кнопка «отключить» противоречила бы этому.
    assert producer_boundary.plans == [], (
        f"удержанный кандидат не должен попасть владельцу: {producer_boundary.subject_ids}"
    )
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == [], f"Ожидали пустой paused, got: {result['paused']}"

    # held содержит кандидата
    assert "held" in result, "Результат должен содержать ключ 'held'"
    held_ids = [h["ad_id"] for h in result["held"]]
    assert held_ids == ["ad_hold"], f"Ожидали held=['ad_hold'], got: {held_ids}"
    held_entry = result["held"][0]
    assert held_entry["ad_name"] == "CityD | отзыв клиента"
    assert held_entry["romi"] == 184.0
    assert held_entry["spend"] == 512.0

    # Запись реально попала в hold-стейт на диске
    hold_state_raw = json.loads(isolate_state_files["hold"].read_text(encoding="utf-8"))
    assert "ad_hold" in hold_state_raw["holds"], "Ожидали запись ad_hold в hold-стейте"
    entry = hold_state_raw["holds"]["ad_hold"]
    assert entry["payments_at_hold"] == 1
    assert entry["romi"] == 184.0
    assert entry["spend"] == 512.0

    # Дневной счётчик пауз НЕ вырос (удержание — не пауза)
    if isolate_state_files["daily"].exists():
        daily = json.loads(isolate_state_files["daily"].read_text(encoding="utf-8"))
        assert daily.get("pauses_today", 0) == 0, \
            f"Дневной счётчик пауз не должен расти от удержаний: {daily}"


# ---------------------------------------------------------------------------
# Сценарий 2: второй кандидат — confirmed_waster — паузится как раньше,
# удержание слив не спасает (в том же прогоне, что и hold-кандидат).
# ---------------------------------------------------------------------------

def test_hold_enabled_lone_confirmed_waster_blocked(isolate_state_files, producer_boundary):
    cfg = _cfg(hold_enabled=True)
    hold_decision = _rank_candidate(ad_id="ad_hold", ad_name="CityD | отзыв клиента")
    waster_decision = _waster_candidate(ad_id="ad_waster", ad_name="Слив | баннер")
    filler = _guardrail_filler()

    local_ads = [
        _local_ad("ad_hold", "CityD | отзыв клиента", spend=512.0, romi=184.0, payments=1),
        _local_ad("ad_waster", "Слив | баннер", spend=300.0, romi=0.0, payments=0),
        _local_ad("ad_filler", filler["ad_name"], spend=50.0, romi=500.0, payments=0),
    ]
    fb_info = {
        # ad_hold делит adset с filler'ом — guardrail защитит filler, не ad_hold
        "ad_hold": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_filler": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        # waster один в своём adset — ACTIVE-замены нет, guard обязан блокировать
        "ad_waster": {"adset_id": "adset_waster", "effective_status": "ACTIVE"},
    }

    out = _run(
        cfg, local_ads, [hold_decision, waster_decision, filler], fb_info,
        no_replacement_ids={"ad_waster"},
    )
    result = out["result"]

    assert result["ran"] is True
    # Слив один в своём adset: отключить его нечем заменить → предложение не
    # создаётся вовсе (guard fail-closed), владелец получает критический алерт.
    assert producer_boundary.plans == [], (
        f"ни удержанный, ни последний ACTIVE не предлагаются: {producer_boundary.subject_ids}"
    )
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []
    out["mock_alert"].assert_called()

    # Ранговый кандидат — в held, не в paused
    held_ids = [h["ad_id"] for h in result["held"]]
    assert held_ids == ["ad_hold"]

    # Заблокированная PAUSE не расходует дневной лимит.
    if isolate_state_files["daily"].exists():
        daily = json.loads(isolate_state_files["daily"].read_text(encoding="utf-8"))
        assert daily.get("pauses_today", 0) == 0


# ---------------------------------------------------------------------------
# Сценарий 3: hold_enabled=False → всё как раньше, pause_ad вызван, held пуст.
# ---------------------------------------------------------------------------

def test_hold_disabled_pauses_as_before(isolate_state_files, producer_boundary):
    cfg = _cfg(hold_enabled=False)
    decision = _rank_candidate()
    filler = _guardrail_filler()
    local_ads = [
        _local_ad("ad_hold", "CityD | отзыв клиента", spend=512.0, romi=184.0, payments=1),
        _local_ad("ad_filler", filler["ad_name"], spend=50.0, romi=500.0, payments=0),
    ]
    fb_info = {
        "ad_hold": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_filler": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
    }

    out = _run(cfg, local_ads, [decision, filler], fb_info)
    result = out["result"]

    assert result["ran"] is True
    # hold выключен → кандидат идёт обычным путём: предложение владельцу.
    producer_boundary.assert_proposed(
        "ad_hold",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    assert len(result["proposals"]) == 1
    assert result["paused"] == [], "автопилот не паузит сам — только предлагает"
    producer_boundary.assert_no_direct_provider_mutation()
    assert result.get("held", []) == [], f"Ожидали пустой held, got: {result.get('held')}"

    # hold-стейт не должен появиться на диске (удержание выключено — блок не трогали)
    assert not isolate_state_files["hold"].exists() or \
        json.loads(isolate_state_files["hold"].read_text(encoding="utf-8")).get("holds") == {}


# ---------------------------------------------------------------------------
# Сценарий 4а: истечение — payments НЕ выросли → пауза с причиной
# «удержание истекло», запись удаляется.
# ---------------------------------------------------------------------------

def test_expired_hold_without_payment_growth_pauses_with_reason(isolate_state_files, producer_boundary):
    cfg = _cfg(hold_enabled=True)

    # Предзаполняем hold-стейт: запись с hold_until в прошлом, payments_at_hold=0
    past = (datetime.now(_TZ) - timedelta(days=1)).isoformat()
    held_at = (datetime.now(_TZ) - timedelta(days=8)).isoformat()
    isolate_state_files["hold"].write_text(json.dumps({
        "holds": {
            "ad_expired": {
                "ad_name": "Экспирированная реклама",
                "held_at": held_at,
                "hold_until": past,
                "payments_at_hold": 0,
                "romi": 184.0,
                "qual_pct": None,
                "spend": 512.0,
                "reason": "PAUSE: портфельный аутсайдер",
            }
        }
    }), encoding="utf-8")

    # Кандидат всё ещё ранговый аутсайдер в этом прогоне (payments не выросли,
    # поэтому ранг не улучшился) — должен присутствовать в decisions, иначе
    # не попадёт в still_active и истечение не будет обработано.
    decision = _rank_candidate(ad_id="ad_expired", ad_name="Экспирированная реклама")
    filler = _guardrail_filler()
    local_ads = [
        _local_ad("ad_expired", "Экспирированная реклама", spend=512.0, romi=184.0, payments=0),
        _local_ad("ad_filler", filler["ad_name"], spend=50.0, romi=500.0, payments=0),
    ]
    fb_info = {
        "ad_expired": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_filler": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
    }

    out = _run(cfg, local_ads, [decision, filler], fb_info)
    result = out["result"]

    assert result["ran"] is True
    producer_boundary.assert_proposed(
        "ad_expired",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    assert len(result["proposals"]) == 1
    assert result["paused"] == []
    producer_boundary.assert_no_direct_provider_mutation()

    # Запись удалена из hold-стейта
    hold_state_raw = json.loads(isolate_state_files["hold"].read_text(encoding="utf-8"))
    assert "ad_expired" not in hold_state_raw["holds"], "Истёкшая запись должна быть удалена"

    # Причина истечения обязана дойти до владельца: он должен понимать, что
    # предложение — следствие закончившейся отсрочки, а не нового вердикта.
    # Проверка переезжала дважды вслед за тем, что реально видит владелец:
    # save_decision (producer решений не пишет) → текст слот-отчёта → summary
    # предложения. Слот-отчёт замолчал («днём тишина»), и
    # владельцу причину теперь несёт карточка предложения — её и проверяем.
    out["mock_save"].assert_not_called()
    plan = producer_boundary.assert_proposed(
        "ad_expired", kind=ProposalKind.PAUSE, origin=ProposalOrigin.AUTOPILOT
    )
    assert "удержание истекло" in plan.summary, (
        f"Ожидали причину истечения в карточке, got: {plan.summary!r}"
    )
    assert "было 0, стало 0" in plan.summary


# ---------------------------------------------------------------------------
# Сценарий 4б: истечение — payments выросли → survived, запись удалена,
# паузы нет.
# ---------------------------------------------------------------------------

def test_expired_hold_with_payment_growth_survives_no_pause(isolate_state_files, producer_boundary):
    cfg = _cfg(hold_enabled=True)

    past = (datetime.now(_TZ) - timedelta(days=1)).isoformat()
    held_at = (datetime.now(_TZ) - timedelta(days=8)).isoformat()
    isolate_state_files["hold"].write_text(json.dumps({
        "holds": {
            "ad_expired": {
                "ad_name": "Пережившая удержание",
                "held_at": held_at,
                "hold_until": past,
                "payments_at_hold": 0,
                "romi": 184.0,
                "qual_pct": None,
                "spend": 512.0,
                "reason": "PAUSE: портфельный аутсайдер",
            }
        }
    }), encoding="utf-8")

    # payments выросли с 0 до 2 — реклама пережила удержание
    local_ads = [_local_ad("ad_expired", "Пережившая удержание", spend=512.0, romi=184.0, payments=2)]
    fb_info = _fb_active("ad_expired")

    out = _run(cfg, local_ads, [], fb_info)
    result = out["result"]

    assert result["ran"] is True
    assert producer_boundary.plans == [], "пережившую удержание рекламу не предлагаем гасить"
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == [], f"Ожидали пустой paused (пережило удержание), got: {result['paused']}"
    assert result.get("held", []) == [], "survived не попадает в held — просто сбрасывается"

    hold_state_raw = json.loads(isolate_state_files["hold"].read_text(encoding="utf-8"))
    assert "ad_expired" not in hold_state_raw["holds"], "Запись должна быть удалена после сброса"

    daily_path = isolate_state_files["daily"]
    if daily_path.exists():
        daily = json.loads(daily_path.read_text(encoding="utf-8"))
        assert daily.get("pauses_today", 0) == 0


# ---------------------------------------------------------------------------
# Сценарий 5: повторный прогон при активном hold → кандидат пропускается
# (не дублируется ни в паузы, ни в held).
# ---------------------------------------------------------------------------

def test_active_hold_candidate_skipped_on_repeat_run(isolate_state_files, producer_boundary):
    cfg = _cfg(hold_enabled=True)

    # Уже держим ad_hold, hold_until в будущем
    future = (datetime.now(_TZ) + timedelta(days=5)).isoformat()
    held_at = (datetime.now(_TZ) - timedelta(days=2)).isoformat()
    isolate_state_files["hold"].write_text(json.dumps({
        "holds": {
            "ad_hold": {
                "ad_name": "CityD | отзыв клиента",
                "held_at": held_at,
                "hold_until": future,
                "payments_at_hold": 1,
                "romi": 184.0,
                "qual_pct": None,
                "spend": 512.0,
                "reason": "PAUSE: портфельный аутсайдер",
            }
        }
    }), encoding="utf-8")

    # Тот же кандидат снова возвращается ранговым правилом в этом прогоне
    decision = _rank_candidate()
    filler = _guardrail_filler()
    local_ads = [
        _local_ad("ad_hold", "CityD | отзыв клиента", spend=512.0, romi=184.0, payments=1),
        _local_ad("ad_filler", filler["ad_name"], spend=50.0, romi=500.0, payments=0),
    ]
    fb_info = {
        "ad_hold": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_filler": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
    }

    out = _run(cfg, local_ads, [decision, filler], fb_info)
    result = out["result"]

    assert result["ran"] is True
    # Активное удержание не превращается в предложение при следующем прогоне.
    assert producer_boundary.plans == [], (
        f"кандидат под активным удержанием не предлагается: {producer_boundary.subject_ids}"
    )
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []
    # Не дублируется в held этого прогона (уже под удержанием — held пуст в ЭТОМ прогоне)
    held_ids = [h["ad_id"] for h in result["held"]]
    assert held_ids == [], f"Активный hold не должен дублироваться в held текущего прогона: {held_ids}"

    # В hold-стейте запись осталась ровно одна (не задвоилась)
    hold_state_raw = json.loads(isolate_state_files["hold"].read_text(encoding="utf-8"))
    assert len(hold_state_raw["holds"]) == 1
    assert hold_state_raw["holds"]["ad_hold"]["hold_until"] == future, \
        "hold_until не должен пересоздаваться при повторном прогоне"


# ---------------------------------------------------------------------------
# Сценарий 6: fail-safe — should_hold кидает исключение → все кандидаты
# запаузены (старое поведение), прогон не падает.
# ---------------------------------------------------------------------------

def test_hold_block_exception_fail_safe_pauses_all(isolate_state_files, producer_boundary):
    """Ошибка hold-блока не должна ронять прогон: кандидаты идут обычным путём.

    Fail-safe остался тем же по смыслу, но «обычный путь» теперь — предложение
    владельцу, а не мутация: при сбое удержания система не должна ни молча
    пропустить аутсайдера, ни отключить его без одобрения.
    """
    cfg = _cfg(hold_enabled=True)
    decision = _rank_candidate()
    filler = _guardrail_filler()
    local_ads = [
        _local_ad("ad_hold", "CityD | отзыв клиента", spend=512.0, romi=184.0, payments=1),
        _local_ad("ad_filler", filler["ad_name"], spend=50.0, romi=500.0, payments=0),
    ]
    fb_info = {
        "ad_hold": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_filler": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
    }

    from services.autopilot import run_autopilot_live
    pause_inventories = {
        "adset_shared": {
            "adset_id": "adset_shared",
            "active_ids": {"ad_hold", "ad_filler"},
            "candidate_context": {
                ad_id: {
                    "ad_id": ad_id,
                    "adset_id": "adset_shared",
                    "configured_status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
                for ad_id in ("ad_hold", "ad_filler")
            },
            "inventory_context": {
                ad_id: {
                    "ad_id": ad_id,
                    "adset_id": "adset_shared",
                    "name": ad_id,
                    "configured_status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
                for ad_id in ("ad_hold", "ad_filler")
            },
            "complete": True,
            "pages_read": 1,
            "error": None,
        }
    }

    with patch("services.autopilot.get_autopilot_config", return_value=cfg), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=[decision, filler]), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("services.adset_pause_guard.fetch_pause_inventory", return_value=pause_inventories), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert") as mock_alert, \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.telegram_bot.send_with_buttons", return_value=False), \
         patch("services.autopilot_hold.should_hold", side_effect=RuntimeError("boom")):
        result = run_autopilot_live(max_pauses=cfg["max_pauses_per_run"], trigger="cron")

    # Прогон не упал — вернул нормальный результат
    assert result["ran"] is True, f"Прогон должен пережить исключение в hold-блоке: {result}"
    # Fail-safe: кандидат обработан как раньше (to_pause = final_list) — предложен владельцу
    producer_boundary.assert_proposed(
        "ad_hold",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    assert len(result["proposals"]) == 1
    assert result["paused"] == []
    producer_boundary.assert_no_direct_provider_mutation()
    assert result.get("held", []) == [], "При fail-safe held должен быть пуст"
    # Критический алерт про ошибку удержания отправлен
    mock_alert.assert_called()


# ---------------------------------------------------------------------------
# Сценарий 7а: телеметрия/telegram — held непуст → в send_telegram-моке
# секция «Держу».
# ---------------------------------------------------------------------------

def test_telegram_report_contains_held_section_when_held_nonempty(isolate_state_files, producer_boundary):
    cfg = _cfg(hold_enabled=True)
    hold_decision = _rank_candidate(ad_id="ad_hold", ad_name="CityD | отзыв клиента")
    waster_decision = _waster_candidate(ad_id="ad_waster", ad_name="Слив | баннер")
    filler = _guardrail_filler()

    local_ads = [
        _local_ad("ad_hold", "CityD | отзыв клиента", spend=512.0, romi=184.0, payments=1),
        _local_ad("ad_waster", "Слив | баннер", spend=300.0, romi=0.0, payments=0),
        _local_ad("ad_filler", filler["ad_name"], spend=50.0, romi=500.0, payments=0),
    ]
    fb_info = {
        "ad_hold": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_filler": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_waster": {"adset_id": "adset_waster", "effective_status": "ACTIVE"},
    }

    out = _run(cfg, local_ads, [hold_decision, waster_decision, filler], fb_info)

    # Отчёт с кнопками уходит, когда есть предложения (слив предложен владельцу),
    # и секция удержания обязана быть в том же сообщении — иначе владелец увидит
    # только «гасим» и не поймёт, что часть кандидатов система держит.
    producer_boundary.assert_proposed(
        "ad_waster",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    producer_boundary.assert_no_direct_provider_mutation()
    out["mock_buttons"].assert_not_called()
    msg_text = out["mock_telegram"].call_args.args[0]
    assert "⏸→🕐" in msg_text, f"Ожидали секцию удержания в тексте отчёта: {msg_text!r}"
    assert "Держу" in msg_text
    assert "CityD" in msg_text


# ---------------------------------------------------------------------------
# Сценарий 7б: пауз нет, но held есть → сообщение всё равно уходит
# (через send_telegram, т.к. paused_details пуст → нет кнопок).
# ---------------------------------------------------------------------------

def test_telegram_report_sent_when_no_pauses_but_held_present(isolate_state_files, producer_boundary):
    cfg = _cfg(hold_enabled=True)
    decision = _rank_candidate()
    filler = _guardrail_filler()
    local_ads = [
        _local_ad("ad_hold", "CityD | отзыв клиента", spend=512.0, romi=184.0, payments=1),
        _local_ad("ad_filler", filler["ad_name"], spend=50.0, romi=500.0, payments=0),
    ]
    fb_info = {
        "ad_hold": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_filler": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
    }

    out = _run(cfg, local_ads, [decision, filler], fb_info)
    result = out["result"]

    assert result["paused"] == []
    held_ids = [h["ad_id"] for h in result["held"]]
    assert held_ids == ["ad_hold"]

    # Сообщение отправлено через send_telegram (не через send_with_buttons —
    # кнопки только для реальных пауз)
    out["mock_telegram"].assert_called_once()
    msg_text = out["mock_telegram"].call_args.args[0]
    assert "Держу" in msg_text, f"Ожидали секцию удержания даже без пауз: {msg_text!r}"
    out["mock_buttons"].assert_not_called()
