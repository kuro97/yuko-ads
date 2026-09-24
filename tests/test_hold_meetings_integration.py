"""
Сквозные (интеграционные) тесты run_autopilot_live с обогащением встречами AMO
(ARCH-hold-meetings, T5, фаза 2).

Проверяем интеграцию _enrich_meetings_for_hold + should_hold внутри
_run_live_inner (Шаг 5.5): ранговый кандидат без потенциала payments/qual, но
с достаточным числом встреч — уходит в held, а не в паузу; при недостатке
встреч/аварии AMO/отключённом hold_enabled — деградация к фазе 1 (payments/qual).

Проект approval-first: автопилот не паузит FB сам, поэтому «кандидат запаузен»
здесь означает «владельцу создан PAUSE-proposal» (result["proposals"], плюс
план в producer_boundary), а не мутацию. Удержание (held) от этого не меняется:
удержанный кандидат не должен получить даже предложения — иначе владелец увидит
кнопку на рекламу, которую система решила подождать.

Паттерн (моки внешних границ) — как в tests/test_autopilot_hold_integration.py:
- services.shadow_report._fetch_ads_from_local_db — локальная БД
- services.decision_policy.score_and_decide — ранговое решение
- services.autopilot._fetch_candidate_fb_info — свежая проверка FB
- services.action_producer_gateway (fetch_pause_inventory/fetch_exact_ad_contexts +
  запись proposal) — producer-граница, FB-мутаторы закрыты Mock'ами
- agent.repositories.decisions_repo.save_decision — SQLite
- services.notifications.send_telegram / send_critical_alert
- services.telegram_bot.send_with_buttons
- integrations.amo.get_leads — источник встреч (мокается на границе AMO, как
  того требует спека, а не _enrich_meetings_for_hold целиком — иначе тест не
  проверял бы реальную интеграцию count_meetings_by_ad).

hold-стейт и дневной стейт паузы изолируются через monkeypatch путей на tmp_path
(isolate_state_files), как в test_autopilot_hold_integration.py.

НЕ трогаем services/autopilot_hold.py (T2b параллельно дорабатывает текст
истечения) — тексты причины истечения здесь НЕ ассертим жёстко.
"""

import json
import sys
from datetime import timedelta, timezone
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
    monkeypatch.setattr(ap, "_LIVE_DAILY_STATE_FILE", daily_state_path)

    yield {"hold": hold_state_path, "daily": daily_state_path}


@pytest.fixture(autouse=True)
def producer_boundary(tmp_path, monkeypatch):
    """Producer-граница: живой discovery/guard, запись плана вместо БД, запрет мутаций.

    Инвентарь producer читает через `adset_pause_guard.fetch_pause_inventory`,
    который каждый тест подменяет своей картиной adset'а (см. _run). Так guard и
    producer видят ОДИН мир: last-active guard проверяется по-настоящему.
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
# Хелперы конфигурации/данных (аналог test_autopilot_hold_integration.py)
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
        "hold_enabled": hold_enabled,
        "hold_romi_target": 200,
        "hold_romi_ratio": 0.8,
        "hold_spend_max": 800,
        "hold_min_qual_pct": 15,
        "hold_min_meetings": 2,
        "hold_days": 7,
    }
    cfg.update(overrides)
    return cfg


def _rank_candidate(ad_id="ad_hold", ad_name="CityD | отзыв клиента", **overrides) -> dict:
    """Ранговый кандидат PAUSE («портфельный аутсайдер»)."""
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
    """Подтверждённый слив — паузится всегда, встречи его не спасают."""
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "action": "PAUSE",
        "score": -5,
        "reasons": ["PAUSE: подтверждённый слив (0 оплат, spend>150)"],
        "is_confirmed_waster": True,
        "is_early_waster": False,
    }


def _local_ad(ad_id, ad_name, spend=640.0, romi=184.0, qual_pct=10.0, payments=0, days_running=30):
    """Кандидат без потенциала по payments/qual (qual<15, payments=0), но romi/spend
    удовлетворяют условиям (а)/(б) should_hold — потенциал должен решаться встречами."""
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "spend": spend,
        "romi": romi,
        "qual_pct": qual_pct,
        "payments": payments,
        "days_running": days_running,
    }


def _lead(status_id, fb_ad_id, lead_id) -> dict:
    """RAW-лид AMO на этапе встречи (см. tests/test_hold_meetings.py::_lead)."""
    return {
        "id": lead_id,
        "status_id": status_id,
        "custom_fields": [{"field_name": "fb_ad_id", "values": [{"value": fb_ad_id}]}],
        "tags": [],
    }


# Реальные status_id этапов встреч (config.py, ARCH-hold-meetings §5)
_MEETING_SCHEDULED = 31239894  # ВСТРЕЧА НАЗНАЧЕНА
_MEETING_HELD = 44446055       # ВСТРЕЧА СОСТОЯЛАСЬ


def _guardrail_filler(ad_id="ad_filler", ad_name="Заполнитель adset (лучший score)") -> dict:
    """Второй не-слив кандидат с заведомо лучшим score в том же adset — забирает
    guardrail-защиту на себя, тестируемый кандидат проходит guardrail в final_list."""
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "action": "PAUSE",
        "score": 100,
        "reasons": ["PAUSE: портфельный аутсайдер (заполнитель для guardrail)"],
        "is_confirmed_waster": False,
        "is_early_waster": False,
    }


def _run(cfg: dict, local_ads: list[dict], decisions: list[dict], fb_info: dict,
         leads: list[dict] | None = None, get_leads_side_effect=None,
         no_replacement_ids: set[str] | None = None):
    """Запускает run_autopilot_live с замоканными внешними границами
    (включая integrations.amo.get_leads — источник встреч для _enrich_meetings_for_hold).

    Созданные предложения собирает фикстура producer_boundary — сюда возвращаются
    только моки уведомлений/БД и сам result.
    """
    from services.autopilot import run_autopilot_live
    get_leads_kwargs = (
        {"side_effect": get_leads_side_effect} if get_leads_side_effect
        else {"return_value": leads if leads is not None else []}
    )
    no_replacement_ids = no_replacement_ids or set()

    candidates_by_adset: dict[str, list[str]] = {}
    for ad_id, info in fb_info.items():
        if info.get("effective_status") == "ACTIVE" and info.get("adset_id"):
            candidates_by_adset.setdefault(info["adset_id"], []).append(ad_id)

    pause_inventories = {}
    for adset_id, ad_ids in candidates_by_adset.items():
        active_ids = set(ad_ids)
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
                    "ad_id": current_id, "adset_id": adset_id, "name": current_id,
                    "configured_status": "ACTIVE", "effective_status": "ACTIVE",
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
         patch("services.telegram_bot.send_with_buttons", return_value=False) as mock_buttons, \
         patch("integrations.amo.get_leads", **get_leads_kwargs) as mock_get_leads:
        result = run_autopilot_live(max_pauses=cfg["max_pauses_per_run"], trigger="cron")

    return {
        "result": result,
        "mock_save": mock_save,
        "mock_telegram": mock_telegram,
        "mock_alert": mock_alert,
        "mock_buttons": mock_buttons,
        "mock_get_leads": mock_get_leads,
    }


# ---------------------------------------------------------------------------
# Сценарий 1: ранговый кандидат без potential по payments/qual, но с 2 встречами
# (1 назначена + 1 состоялась) → held (не пауза). Telegram-моке содержит
# «встреч (назн. 2» (2 назначенных: MEETING_SCHEDULED + MEETING_SCHEDULED_2 из
# спеки не нужен — берём 2 лида на MEETING_SCHEDULED, чтобы точно проверить
# «назн. 2»). get_leads вызван РОВНО один раз за прогон.
# ---------------------------------------------------------------------------

def test_rank_candidate_with_two_meetings_goes_to_held_not_paused(
    isolate_state_files, producer_boundary
):
    cfg = _cfg(hold_enabled=True)
    decision = _rank_candidate(ad_id="ad_hold", ad_name="CityD | отзыв клиента")
    filler = _guardrail_filler()
    local_ads = [
        _local_ad("ad_hold", "CityD | отзыв клиента", spend=640.0, romi=184.0, qual_pct=10.0, payments=0),
        _local_ad("ad_filler", filler["ad_name"], spend=50.0, romi=500.0, qual_pct=0.0, payments=0),
    ]
    fb_info = {
        "ad_hold": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_filler": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
    }
    # 2 встречи назначены на ad_hold (fb_ad_id матчится напрямую по known_ad_ids)
    leads = [
        _lead(_MEETING_SCHEDULED, fb_ad_id="ad_hold", lead_id=1),
        _lead(_MEETING_SCHEDULED, fb_ad_id="ad_hold", lead_id=2),
    ]

    out = _run(cfg, local_ads, [decision, filler], fb_info, leads=leads)
    result = out["result"]

    assert result["ran"] is True, f"Ожидали ran=True, got: {result}"
    # Удержан по встречам: ни паузы, ни предложения владельцу. Предложение здесь
    # было бы ошибкой — система сама решила дать рекламе время.
    assert producer_boundary.plans == [], (
        f"удержанный кандидат не должен попасть владельцу: {producer_boundary.subject_ids}"
    )
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == [], f"Ожидали пустой paused, got: {result['paused']}"

    held_ids = [h["ad_id"] for h in result["held"]]
    assert held_ids == ["ad_hold"], f"Ожидали held=['ad_hold'], got: {held_ids}"
    held_entry = result["held"][0]
    assert held_entry["meetings_scheduled"] == 2
    assert held_entry["meetings_held"] == 0

    # get_leads вызван РОВНО один раз за весь прогон (экономность — ARCH-hold-meetings §9)
    out["mock_get_leads"].assert_called_once()

    # hold-стейт на диске содержит встречи
    hold_state_raw = json.loads(isolate_state_files["hold"].read_text(encoding="utf-8"))
    entry = hold_state_raw["holds"]["ad_hold"]
    assert entry["meetings_scheduled"] == 2
    assert entry["meetings_held"] == 0
    assert entry["meetings_at_hold"] == 2

    # Telegram-текст содержит блок встреч «N встреч (назн. 2 / сост. 0)»
    out["mock_telegram"].assert_called_once()
    msg_text = out["mock_telegram"].call_args.args[0]
    assert "встреч (назн. 2" in msg_text, f"Ожидали текст про 2 назначенных встречи: {msg_text!r}"


# ---------------------------------------------------------------------------
# Сценарий 2: тот же кандидат, но только 1 встреча (< hold_min_meetings=2) →
# пауза как раньше (недостаточно встреч, payments/qual тоже не спасают).
# ---------------------------------------------------------------------------

def test_rank_candidate_with_one_meeting_still_paused(isolate_state_files, producer_boundary):
    """1 встреча < hold_min_meetings=2 → удержания нет, владельцу уходит предложение."""
    cfg = _cfg(hold_enabled=True)
    decision = _rank_candidate(ad_id="ad_hold", ad_name="CityD | отзыв клиента")
    filler = _guardrail_filler()
    local_ads = [
        _local_ad("ad_hold", "CityD | отзыв клиента", spend=640.0, romi=184.0, qual_pct=10.0, payments=0),
        _local_ad("ad_filler", filler["ad_name"], spend=50.0, romi=500.0, qual_pct=0.0, payments=0),
    ]
    fb_info = {
        "ad_hold": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_filler": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
    }
    leads = [_lead(_MEETING_SCHEDULED, fb_ad_id="ad_hold", lead_id=1)]

    out = _run(cfg, local_ads, [decision, filler], fb_info, leads=leads)
    result = out["result"]

    assert result["ran"] is True
    producer_boundary.assert_proposed(
        "ad_hold",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    assert len(result["proposals"]) == 1
    assert result["paused"] == [], "автопилот не паузит сам — только предлагает"
    producer_boundary.assert_no_direct_provider_mutation()
    assert result.get("held", []) == [], f"Ожидали пустой held (1 встреча < hold_min_meetings=2): {result.get('held')}"
    out["mock_get_leads"].assert_called_once()


# ---------------------------------------------------------------------------
# Сценарий 3: AMO get_leads кидает исключение → прогон НЕ падает, кандидат без
# встреч-потенциала (qual<15, payments=0) паузится (деградация до фазы 1),
# лог warning (проверяем через caplog).
# ---------------------------------------------------------------------------

def test_amo_get_leads_exception_degrades_to_phase1_pause(
    isolate_state_files, producer_boundary, caplog
):
    cfg = _cfg(hold_enabled=True)
    decision = _rank_candidate(ad_id="ad_hold", ad_name="CityD | отзыв клиента")
    filler = _guardrail_filler()
    local_ads = [
        _local_ad("ad_hold", "CityD | отзыв клиента", spend=640.0, romi=184.0, qual_pct=10.0, payments=0),
        _local_ad("ad_filler", filler["ad_name"], spend=50.0, romi=500.0, qual_pct=0.0, payments=0),
    ]
    fb_info = {
        "ad_hold": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_filler": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
    }

    with caplog.at_level("WARNING"):
        out = _run(cfg, local_ads, [decision, filler], fb_info,
                    get_leads_side_effect=RuntimeError("AMO недоступен"))
    result = out["result"]

    # Прогон не падает
    assert result["ran"] is True, f"Прогон должен пережить сбой AMO при добыче встреч: {result}"
    # Деградация: встреч нет (0), payments=0, qual=10<15 → потенциала нет →
    # предложение об отключении уходит владельцу, удержания не возникает.
    producer_boundary.assert_proposed(
        "ad_hold",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    assert len(result["proposals"]) == 1
    assert result["paused"] == []
    producer_boundary.assert_no_direct_provider_mutation()
    assert result.get("held", []) == []

    # Warning про сбой добычи встреч залогирован (fail-safe, не критический алерт)
    assert any(
        "_enrich_meetings_for_hold" in rec.message or "не удалось добыть встречи" in rec.message
        for rec in caplog.records
    ), f"Ожидали warning про сбой добычи встреч в логах: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Сценарий 4: hold_enabled=false → get_leads НЕ вызывается вообще (нет лишнего
# AMO-трафика, экономность), кандидат паузится как в фазе 1 без удержания.
# ---------------------------------------------------------------------------

def test_hold_disabled_get_leads_not_called(isolate_state_files, producer_boundary):
    cfg = _cfg(hold_enabled=False)
    decision = _rank_candidate(ad_id="ad_hold", ad_name="CityD | отзыв клиента")
    filler = _guardrail_filler()
    local_ads = [
        _local_ad("ad_hold", "CityD | отзыв клиента", spend=640.0, romi=184.0, qual_pct=10.0, payments=0),
        _local_ad("ad_filler", filler["ad_name"], spend=50.0, romi=500.0, qual_pct=0.0, payments=0),
    ]
    fb_info = {
        "ad_hold": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_filler": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
    }

    out = _run(cfg, local_ads, [decision, filler], fb_info)
    result = out["result"]

    assert result["ran"] is True
    producer_boundary.assert_proposed(
        "ad_hold",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    assert len(result["proposals"]) == 1
    assert result["paused"] == []
    producer_boundary.assert_no_direct_provider_mutation()
    assert result.get("held", []) == []

    # Экономность: hold_enabled=False — не должно быть лишнего запроса к AMO
    out["mock_get_leads"].assert_not_called()


# ---------------------------------------------------------------------------
# Сценарий 5: confirmed_waster с 5 встречами (2 назначено + 3 состоялось) —
# всё равно паузится (встречи слив не спасают, should_hold ветка 1 — ранний
# return до чтения ad_metrics со встречами).
# ---------------------------------------------------------------------------

def test_lone_confirmed_waster_with_five_meetings_still_blocked(
    isolate_state_files, producer_boundary
):
    """Слив в adset без замены: встречи его не спасают, но и гасить нечем.

    Отключить последнее ACTIVE значит обнулить трафик adset'а, поэтому
    предложение НЕ создаётся вовсе — уходит критический алерт владельцу.
    """
    cfg = _cfg(hold_enabled=True)
    waster_decision = _waster_candidate(ad_id="ad_waster", ad_name="Слив | баннер")
    local_ads = [
        _local_ad("ad_waster", "Слив | баннер", spend=300.0, romi=0.0, qual_pct=0.0, payments=0),
    ]
    fb_info = {"ad_waster": {"adset_id": "adset_waster", "effective_status": "ACTIVE"}}
    leads = (
        [_lead(_MEETING_SCHEDULED, fb_ad_id="ad_waster", lead_id=i) for i in range(1, 3)]
        + [_lead(_MEETING_HELD, fb_ad_id="ad_waster", lead_id=i) for i in range(3, 6)]
    )

    out = _run(
        cfg, local_ads, [waster_decision], fb_info, leads=leads,
        no_replacement_ids={"ad_waster"},
    )
    result = out["result"]

    assert result["ran"] is True
    assert producer_boundary.plans == [], "последний ACTIVE не предлагаем владельцу"
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []
    out["mock_alert"].assert_called()
    assert result.get("held", []) == []


# ---------------------------------------------------------------------------
# Сценарий 6: held-запись содержит meetings_scheduled/held; повторный прогон
# при активном hold — без дублей (кандидат пропускается, held пуст в новом
# прогоне, запись в hold-стейте не задваивается).
# ---------------------------------------------------------------------------

def test_held_entry_has_meetings_fields_no_duplicate_on_repeat_run(
    isolate_state_files, producer_boundary
):
    cfg = _cfg(hold_enabled=True)
    decision = _rank_candidate(ad_id="ad_hold", ad_name="CityD | отзыв клиента")
    filler = _guardrail_filler()
    local_ads = [
        _local_ad("ad_hold", "CityD | отзыв клиента", spend=640.0, romi=184.0, qual_pct=10.0, payments=0),
        _local_ad("ad_filler", filler["ad_name"], spend=50.0, romi=500.0, qual_pct=0.0, payments=0),
    ]
    fb_info = {
        "ad_hold": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_filler": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
    }
    leads = [
        _lead(_MEETING_SCHEDULED, fb_ad_id="ad_hold", lead_id=1),
        _lead(_MEETING_HELD, fb_ad_id="ad_hold", lead_id=2),
    ]

    # Первый прогон — кандидат уходит в held с meetings_scheduled=1, meetings_held=1
    out1 = _run(cfg, local_ads, [decision, filler], fb_info, leads=leads)
    result1 = out1["result"]
    assert result1["paused"] == []
    held_ids_1 = [h["ad_id"] for h in result1["held"]]
    assert held_ids_1 == ["ad_hold"]
    held_entry_1 = result1["held"][0]
    assert held_entry_1["meetings_scheduled"] == 1
    assert held_entry_1["meetings_held"] == 1

    hold_state_raw = json.loads(isolate_state_files["hold"].read_text(encoding="utf-8"))
    assert len(hold_state_raw["holds"]) == 1
    entry = hold_state_raw["holds"]["ad_hold"]
    assert entry["meetings_scheduled"] == 1
    assert entry["meetings_held"] == 1
    assert entry["meetings_at_hold"] == 2

    # Повторный прогон в тот же день, тот же кандидат снова возвращается ранговым
    # правилом — hold_until ещё в будущем (только что поставлен на hold_days=7)
    out2 = _run(cfg, local_ads, [decision, filler], fb_info, leads=leads)
    result2 = out2["result"]

    assert result2["ran"] is True
    assert producer_boundary.plans == [], (
        "ни первый, ни повторный прогон не должны предлагать удержанного кандидата"
    )
    producer_boundary.assert_no_direct_provider_mutation()
    assert result2["paused"] == []
    # Не дублируется в held этого (повторного) прогона — уже под активным удержанием
    held_ids_2 = [h["ad_id"] for h in result2["held"]]
    assert held_ids_2 == [], f"Активный hold не должен дублироваться в held повторного прогона: {held_ids_2}"

    # В hold-стейте по-прежнему ровно одна запись (не задвоилась)
    hold_state_raw_2 = json.loads(isolate_state_files["hold"].read_text(encoding="utf-8"))
    assert len(hold_state_raw_2["holds"]) == 1
    entry_2 = hold_state_raw_2["holds"]["ad_hold"]
    assert entry_2["meetings_scheduled"] == 1
    assert entry_2["meetings_held"] == 1
