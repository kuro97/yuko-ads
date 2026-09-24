"""Автономные паузы подтверждённых сливов — единственный автономный класс.

Инварианты, которые эти тесты обязаны держать:
  * выключено настройкой (дефолт) → бот не исполняет НИЧЕГО сам;
  * включено → автономна ровно пауза подтверждённого слива, и никакой другой
    класс действий автономным не стал;
  * все четыре защиты (последняя активная в адсете, живое состояние,
    независимая верификация, удержание) остаются на месте;
  * аномальный всплеск кандидатов = ноль мутаций + критический алерт;
  * решение записано в аудит с явным признаком «автомат».
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from services import autonomous_pause as autonomous
from tests.gateway_test_helpers import proposal_outcome


PROJECT_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Состояние автономных пауз — во временном файле, а не в data/.

    Сверка исполнения (owner_action_attempts) по умолчанию «недоступна»
    (None → сводка по журналу): иначе БД контура, оставшаяся от соседних
    тестов в creative_intelligence.DB_PATH, молча отфильтровывала бы записи.
    Тесты самой сверки патчат _confirmed_pause_ids явно.
    """
    monkeypatch.setattr(
        autonomous, "STATE_FILE", tmp_path / "autonomous_pause_state.json"
    )
    monkeypatch.setattr(autonomous, "_confirmed_pause_ids", lambda since_iso: None)
    return tmp_path


@pytest.fixture(autouse=True)
def patch_autopilot_state(tmp_path, monkeypatch):
    import services.autopilot as ap_module

    monkeypatch.setattr(ap_module, "STATE_FILE", tmp_path / "autopilot_state.json")
    monkeypatch.setattr(ap_module, "_AUTO_ACTIONS_FILE", tmp_path / "auto_actions.json")
    monkeypatch.setattr(
        ap_module, "_CLASSIC_DAILY_STATE_FILE", tmp_path / "classic_daily.json"
    )
    monkeypatch.setattr(
        ap_module, "_LIVE_DAILY_STATE_FILE", tmp_path / "live_daily.json"
    )


def _cfg(*, autonomous_on: bool = True, anomaly_guard: bool = True) -> dict:
    return {
        "enabled": True,
        "kill_switch": False,
        "max_pauses_per_run": 8,
        "min_days_protect": 5,
        "autonomous": {
            "pause_confirmed_wasters": autonomous_on,
            "anomaly_guard": anomaly_guard,
        },
    }


def _waster_ad(ad_id: str = "ad1", **overrides) -> dict:
    """Объявление-слив: сверка прошла, оплат ноль, расход значимый."""
    ad = {
        "ad_id": ad_id,
        "ad_name": f"Слив {ad_id}",
        "spend": 450.0,
        "days_running": 12,
        "cpl": 9.4,
        "ctr": 0.9,
        "leads": 48,
        "qual_pct": 13.0,
        "romi": None,
        "payments": 0,
        "outcomes_matched_at": "2026-08-02T21:00:00+00:00",
        "impressions": 90000,
        "city": "CityA",
        "adset_type": "L2",
        "adset_id": None,
        "effective_status": "ACTIVE",
        "recommendation": "ЖДАТЬ",
        "reason": "",
    }
    ad.update(overrides)
    return ad


def _waster_decision(ad_id: str = "ad1", *, confirmed: bool = True, **overrides) -> dict:
    decision = {
        "ad_id": ad_id,
        "ad_name": f"Слив {ad_id}",
        "adset_id": f"adset-{ad_id}",
        "action": "PAUSE",
        "score": 3,
        "reasons": ["PAUSE: подтверждённый слив (тир A)"],
        "business_reason": "48 лидов, оплат 0 при расходе $450 — деньги уходят, продаж нет",
        "is_confirmed_waster": confirmed,
        "is_zero_leads_after_3d": False,
    }
    decision.update(overrides)
    return decision


def _run_live(local_ads, decisions, cfg, *, approve=None, inventory=None, max_pauses=8):
    """Прогон боевого автопилота с подменённой producer-границей.

    ``propose_pause`` отдаёт готовую квитанцию, самоодобрение записывается
    Mock'ом — так тест видит РОВНО то, что бот решил исполнить сам.
    """
    approve = approve if approve is not None else Mock(return_value=True)
    fb_info = {
        str(d["ad_id"]): {"adset_id": d["adset_id"], "effective_status": "ACTIVE"}
        for d in decisions
    }
    if inventory is None:
        # По умолчанию у каждого кандидата в адсете есть доказанная ACTIVE-замена:
        # иначе last-active guard справедливо заблокировал бы паузу, и тест
        # проверял бы не то, что собирался.
        inventory = {}
        for decision in decisions:
            ad_id = str(decision["ad_id"])
            adset_id = str(decision["adset_id"])
            replacement = f"replacement-{ad_id}"
            context = {
                current: {
                    "ad_id": current,
                    "adset_id": adset_id,
                    "name": current,
                    "configured_status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
                for current in (ad_id, replacement)
            }
            inventory[adset_id] = {
                "adset_id": adset_id,
                "active_ids": {ad_id, replacement},
                "candidate_context": {ad_id: context[ad_id]},
                "inventory_context": context,
                "complete": True,
                "pages_read": 1,
                "error": None,
            }
    stack = [
        patch(
            "services.adset_pause_guard.fetch_pause_inventory",
            return_value=inventory,
        ),
        # Producer импортировал функцию по значению — патчим и его ссылку,
        # иначе guard и producer видели бы разные картины мира.
        patch(
            "services.action_producer_gateway.fetch_pause_inventory",
            return_value=inventory,
        ),
        patch("services.autopilot.get_autopilot_config", return_value=cfg),
        patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads),
        patch("services.decision_policy.score_and_decide", return_value=decisions),
        patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info),
        patch("agent.analyzer.get_fresh_ad_statuses", return_value={}),
        patch("agent.fb_common.build_adset_map", return_value={}),
        patch("agent.repositories.decisions_repo.save_decision"),
        patch("agent.scheduler.load_settings", return_value={"thresholds": {}}),
        patch("services.autopilot.get_active_overrides", return_value={}),
        patch("services.autopilot._write_auto_action"),
        patch("services.notifications.send_telegram"),
        patch("services.notifications.send_critical_alert"),
        patch("services.telegram_bot.send_with_buttons", return_value=True),
        patch("services.autonomous_pause.approve_autonomously", approve),
    ]
    stack.append(
        patch(
            "services.action_producer_gateway.fetch_exact_ad_contexts",
            side_effect=lambda ad_ids, *, require_names=True: (
                {
                    ad_id: {
                        **context,
                        "account_id": "1111",
                    }
                    for item in inventory.values()
                    for ad_id, context in (item.get("candidate_context") or {}).items()
                    if ad_id in set(ad_ids)
                },
                None,
            ),
        )
    )

    # Вся discovery/guard-логика propose_pause остаётся живой; подменяется
    # только запись в БД и биндинг идемпотентности.
    stack.append(
        patch(
            "services.action_producer_gateway.reserve_idempotency",
            side_effect=lambda scope, payload, key=None: (
                "22222222-2222-4222-8222-222222222222"
            ),
        )
    )
    recorded: list[str] = []

    def _fake_persist(plan, *, now=None):
        del now
        recorded.append(plan.targets[0].subject_id)
        return proposal_outcome(f"proposal-{plan.targets[0].subject_id}").receipt

    stack.append(
        patch("services.owner_action_repository.propose_action", _fake_persist)
    )

    from contextlib import ExitStack

    with ExitStack() as ctx:
        for item in stack:
            ctx.enter_context(item)
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live(max_pauses=max_pauses)
    return result, approve


# ---------------------------------------------------------------------------
# Настройка: дефолт выключен
# ---------------------------------------------------------------------------

def test_default_config_keeps_autonomy_off():
    """Дефолт после деплоя — автономия выключена, предохранитель включён."""
    from services.autopilot import AUTOPILOT_DEFAULTS

    assert AUTOPILOT_DEFAULTS["autonomous"]["pause_confirmed_wasters"] is False
    assert AUTOPILOT_DEFAULTS["autonomous"]["anomaly_guard"] is True


@pytest.mark.parametrize(
    "value",
    ["true", 1, "yes", None, {}, [], "1"],
)
def test_master_key_is_fail_closed(value):
    """Неизвестное значение из правленого руками settings.json НЕ включает автономию."""
    assert autonomous.is_autonomous_pause_enabled(
        {"autonomous": {"pause_confirmed_wasters": value}}
    ) is False


@pytest.mark.parametrize("value", ["false", 0, None, "off", {}])
def test_anomaly_guard_is_fail_safe(value):
    """Выключить предохранитель можно ТОЛЬКО настоящим JSON false."""
    assert autonomous.is_anomaly_guard_enabled(
        {"autonomous": {"anomaly_guard": value}}
    ) is True
    assert autonomous.is_anomaly_guard_enabled(
        {"autonomous": {"anomaly_guard": False}}
    ) is False


def test_disabled_by_setting_makes_zero_autonomous_actions():
    """Выключено настройкой (дефолтное состояние) → ни одной автономной мутации."""
    ads = [_waster_ad("ad1"), _waster_ad("ad2")]
    decisions = [_waster_decision("ad1"), _waster_decision("ad2")]

    result, approve = _run_live(ads, decisions, _cfg(autonomous_on=False))

    assert approve.call_count == 0
    assert result["autonomous"] == []
    # Предложения владельцу при этом создаются как раньше — approval-first цел.
    assert len(result["proposals"]) == 2


# ---------------------------------------------------------------------------
# Класс действий: что автономно, а что нет
# ---------------------------------------------------------------------------



def _mock_v2(monkeypatch, is_waster=True):
    """Автономия теперь решает по waster_rules_v2 — в тестах его мокаем."""
    import services.waster_rules_v2 as v2

    monkeypatch.setattr(
        v2, "confirmed_waster_v2",
        lambda ad_id, now=None: v2.WasterVerdict(
            is_waster, "R1_MATURE_ZERO" if is_waster else "NONE", "тест"
        ),
    )


def test_confirmed_waster_is_paused_autonomously(monkeypatch):
    _mock_v2(monkeypatch)
    ads = [_waster_ad("ad1")]
    decisions = [_waster_decision("ad1")]

    result, approve = _run_live(ads, decisions, _cfg())

    assert approve.call_count == 1
    assert result["autonomous"] == ["ad1"]


def test_v2_not_waster_stays_a_proposal(monkeypatch):
    """v2 сказал «не слив» → автономии нет, предложение владельцу остаётся.

    Прежние по-сигнальные случаи (нет сверки AMO, есть оплаты, мал расход)
    переехали внутрь waster_rules_v2 и покрыты его юнитами: здесь важен только
    контракт «не-слив не режется сам».
    """
    _mock_v2(monkeypatch, is_waster=False)
    ads = [_waster_ad("ad1", outcomes_matched_at=None)]
    decisions = [_waster_decision("ad1")]

    result, approve = _run_live(ads, decisions, _cfg())

    assert approve.call_count == 0
    assert result["autonomous"] == []
    assert len(result["proposals"]) == 1  # владельцу предложение всё равно ушло


def test_spend_below_threshold_is_not_autonomous(monkeypatch):
    """Расход ниже порога значимости — не слив, а почти не открутившаяся реклама."""
    _mock_v2(monkeypatch, is_waster=False)
    ads = [_waster_ad("ad1", spend=4.0)]
    decisions = [_waster_decision("ad1")]

    result, approve = _run_live(ads, decisions, _cfg())

    assert approve.call_count == 0
    assert result["autonomous"] == []


def test_decision_without_confirmed_waster_flag_is_not_autonomous():
    """Пауза по другому правилу (нулевые лиды, тренд, портфель) — не автономна."""
    ads = [_waster_ad("ad1")]
    decisions = [_waster_decision("ad1", confirmed=False)]

    result, approve = _run_live(ads, decisions, _cfg())

    assert approve.call_count == 0
    assert result["autonomous"] == []


def test_non_pause_decision_never_becomes_autonomous():
    assert autonomous.is_autonomous_pause_candidate(
        {"action": "SCALE", "is_confirmed_waster": True}, _waster_ad()
    ) is False
    assert autonomous.is_autonomous_pause_candidate(
        {"action": "KEEP", "is_confirmed_waster": True}, _waster_ad()
    ) is False


def test_v2_data_error_fails_closed(monkeypatch):
    """Сбой сбора данных v2 (DATA_ERROR) → не автономно: недоказанное не режется."""
    import services.waster_rules_v2 as v2

    monkeypatch.setattr(
        v2, "confirmed_waster_v2",
        lambda ad_id, now=None: v2.WasterVerdict(False, "DATA_ERROR", "сеть упала"),
    )
    assert autonomous.is_autonomous_pause_candidate(_waster_decision(), None) is False


# ---------------------------------------------------------------------------
# Защиты, которые обязаны сохраниться
# ---------------------------------------------------------------------------

def _lone_active_inventory(ad_id: str, adset_id: str) -> dict:
    """В адсете этот кандидат — единственный ACTIVE."""
    return {
        adset_id: {
            "adset_id": adset_id,
            "active_ids": {ad_id},
            "candidate_context": {
                ad_id: {
                    "ad_id": ad_id,
                    "adset_id": adset_id,
                    "name": ad_id,
                    "configured_status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
            },
            "complete": True,
            "pages_read": 1,
            "error": None,
        }
    }


def test_last_active_in_adset_is_never_paused_autonomously():
    """Последняя активная реклама в адсете не паузится ни при каких условиях."""
    ads = [_waster_ad("ad1")]
    decisions = [_waster_decision("ad1", adset_id="adset-ad1")]

    result, approve = _run_live(
        ads,
        decisions,
        _cfg(),
        inventory=_lone_active_inventory("ad1", "adset-ad1"),
    )

    assert approve.call_count == 0
    assert result["autonomous"] == []
    assert result["proposals"] == []


def test_live_state_change_blocks_autonomous_mutation():
    """Живое состояние FB разошлось с локальным → ни предложения, ни мутации."""
    ads = [_waster_ad("ad1")]
    decisions = [_waster_decision("ad1")]
    approve = Mock(return_value=True)

    with patch("services.autopilot.get_autopilot_config", return_value=_cfg()), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch(
             "services.autopilot._fetch_candidate_fb_info",
             return_value={"ad1": {"adset_id": "adset-ad1", "effective_status": "PAUSED"}},
         ), \
         patch("agent.analyzer.get_fresh_ad_statuses", return_value={}), \
         patch("agent.fb_common.build_adset_map", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True), \
         patch("services.autonomous_pause.approve_autonomously", approve):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live()

    assert approve.call_count == 0
    assert result.get("autonomous", []) == []


def test_hold_is_not_bypassed_by_autonomy():
    """Реклама под удержанием не становится автономной паузой."""
    ads = [_waster_ad("ad1")]
    decisions = [_waster_decision("ad1")]
    held_state = {
        "holds": {
            "ad1": {
                "ad_name": "Слив ad1",
                "held_at": "2026-08-03T09:00:00+00:00",
                "hold_until": "2026-08-09T09:00:00+00:00",
                "payments_at_hold": 0,
                "romi": 90.0,
                "qual_pct": 13.0,
                "spend": 450.0,
                "reason": "ROMI близко к цели",
            }
        }
    }

    cfg = _cfg()
    cfg["hold_enabled"] = True

    with patch("services.autopilot_hold.load_hold_state", return_value=held_state), \
         patch("services.autopilot_hold.save_hold_state"), \
         patch("services.autopilot._enrich_meetings_for_hold"), \
         patch("services.autopilot_hold.check_hold_expired", return_value=("survived", "")):
        result, approve = _run_live(ads, decisions, cfg)

    assert approve.call_count == 0
    assert result["autonomous"] == []


def _hold_entry(hold_until: str) -> dict:
    return {
        "ad_name": "Слив ad1",
        "held_at": (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),
        "hold_until": hold_until,
        "payments_at_hold": 0,
        "romi": 90.0,
        "qual_pct": 13.0,
        "spend": 450.0,
        "reason": "ROMI близко к цели",
    }


def test_hold_closed_this_run_still_blocks_autonomy():
    """Удержание, закрывшееся В ЭТОМ ЖЕ прогоне, не открывает дорогу автономии.

    "survived" означает «оплаты выросли», а автономный класс требует «оплат
    ноль» — оба вердикта в одном прогоне возможны только при расхождении
    источников. Противоречие в данных = не действуем сами, предложение с
    кнопкой уезжает владельцу.
    """
    ads = [_waster_ad("ad1")]
    decisions = [_waster_decision("ad1")]
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    held_state = {"holds": {"ad1": _hold_entry(expired)}}

    cfg = _cfg()
    cfg["hold_enabled"] = True

    with patch("services.autopilot_hold.load_hold_state", return_value=held_state), \
         patch("services.autopilot_hold.save_hold_state"), \
         patch("services.autopilot._enrich_meetings_for_hold"), \
         patch("services.autopilot_hold.check_hold_expired", return_value=("survived", "")):
        result, approve = _run_live(ads, decisions, cfg)

    assert approve.call_count == 0
    assert result["autonomous"] == []
    # Пауза не потеряна — она ушла владельцу предложением, как и раньше.
    assert result["proposals"]


def test_hold_block_failure_disables_autonomy_but_keeps_proposals():
    """Сбой hold-блока = автономии в прогоне нет: неизвестно, кто под удержанием.

    Fail-safe hold-блока возвращает всех кандидатов в паузу (деньги важнее), и
    для предложений владельцу это верно. Но реклама под активным удержанием
    может к этому моменту стать подтверждённым сливом — тогда без этой защиты
    сбой чтения стейта молча выключил бы то, что удержание защищает.
    """
    ads = [_waster_ad("ad1")]
    decisions = [_waster_decision("ad1")]
    active = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    held_state = {"holds": {"ad1": _hold_entry(active)}}

    cfg = _cfg()
    cfg["hold_enabled"] = True

    # Первый вызов — Шаг 3.5 (истечение), второй — Шаг 5.5 (удержание): падает
    # только второй, то есть до автономии доходит именно fail-safe-ветка.
    with patch(
        "services.autopilot_hold.load_hold_state",
        side_effect=[held_state, RuntimeError("hold-стейт недоступен")],
    ), \
         patch("services.autopilot_hold.save_hold_state"), \
         patch("services.autopilot._enrich_meetings_for_hold"):
        result, approve = _run_live(ads, decisions, cfg)

    assert approve.call_count == 0
    assert result["autonomous"] == []
    assert result["proposals"]


def test_execution_path_has_no_branch_for_automatic_decisions():
    """Исполнение не знает, кто одобрил: те же проверки, тот же транспорт.

    Если в исполнителе, живом манифесте или верификаторе появится ветка по
    ``decision_source``/``SYSTEM``, автономное действие пойдёт по облегчённому
    пути — ровно то, чего быть не должно.
    """
    for name in (
        "owner_action_executor.py",
        "owner_action_live_manifest.py",
        "action_verifier.py",
        "action_gateway.py",
        "action_adapter_pause.py",
    ):
        text = (PROJECT_ROOT / "services" / name).read_text(encoding="utf-8")
        assert "decision_source" not in text, name
        assert "automation_rule" not in text, name


def test_verification_still_covers_autonomous_pause():
    """Автономная пауза остаётся обычным PAUSE-предложением для верификатора.

    Верификатор берёт работу по CONFIRMED-попыткам и виду предложения, а не по
    тому, кто его одобрил — значит автономное действие проверяется тем же
    независимым чтением живого состояния.
    """
    from services.action_verifier import VERIFIABLE_KINDS

    assert "PAUSE" in VERIFIABLE_KINDS


# ---------------------------------------------------------------------------
# Предохранитель от аномалии
# ---------------------------------------------------------------------------

def test_anomaly_needs_both_conditions():
    # Втрое больше медианы, но меньше 10 штук — не аномалия.
    assert autonomous.anomaly_verdict(9, [1, 1, 1, 1])[0] is False
    # 10 штук, но медиана высокая — не аномалия.
    assert autonomous.anomaly_verdict(10, [8, 9, 10, 11])[0] is False
    # И втрое больше медианы, и не меньше 10 — аномалия.
    assert autonomous.anomaly_verdict(12, [1, 2, 1, 2])[0] is True


def test_anomaly_blocks_every_mutation_and_alerts(monkeypatch):
    """Кандидатов втрое больше нормы и ≥10 → ноль мутаций + критический алерт."""
    _mock_v2(monkeypatch)
    autonomous.save_state({"runs": [1, 2, 1, 2, 1, 2], "journal": []})
    ads = [_waster_ad(f"ad{i}") for i in range(12)]
    decisions = [_waster_decision(f"ad{i}", adset_id=f"adset{i}") for i in range(12)]
    cfg = _cfg()
    cfg["max_pauses_per_run"] = 20

    alert = Mock()
    with patch("services.autonomous_pause.send_anomaly_alert", alert):
        result, approve = _run_live(ads, decisions, cfg, max_pauses=20)

    assert approve.call_count == 0
    assert result["autonomous"] == []
    assert alert.call_count == 1
    assert "12" in alert.call_args[0][0]


def test_anomalous_run_does_not_poison_the_median():
    """Выброс не попадает в историю: иначе завтра такой же выброс пройдёт."""
    autonomous.save_state({"runs": [1, 1, 1, 1], "journal": []})
    is_anomaly, _ = autonomous.check_anomaly(30, enabled=True)
    assert is_anomaly is True
    assert autonomous.load_state()["runs"] == [1, 1, 1, 1]


def test_anomaly_guard_can_be_switched_off():
    autonomous.save_state({"runs": [1, 1, 1, 1], "journal": []})
    is_anomaly, _ = autonomous.check_anomaly(30, enabled=False)
    assert is_anomaly is False
    assert autonomous.load_state()["runs"][-1] == 30


def test_broken_state_file_degrades_to_stricter_guard(isolated_state):
    autonomous.STATE_FILE.write_text("{битый json", encoding="utf-8")
    assert autonomous.load_state() == {"runs": [], "journal": []}
    assert autonomous.anomaly_verdict(10, [])[0] is True


# ---------------------------------------------------------------------------
# Жёсткая граница автономии
# ---------------------------------------------------------------------------

def test_no_other_action_class_is_autonomous():
    """Страж: автономен ровно один класс, и самоодобрение зовут из одного места.

    Если кто-то сделает автономным запуск, бюджет, паузу по нулевым лидам или
    по тренду, этот тест упадёт — расширение автономии обязано быть отдельным
    решением владельца, а не побочным эффектом рефакторинга.
    """
    assert autonomous.AUTONOMOUS_ACTION_KINDS == frozenset({"PAUSE_AD"})

    call_sites: list[str] = []
    for path in (
        *PROJECT_ROOT.glob("services/*.py"),
        *PROJECT_ROOT.glob("web/*.py"),
        # agent/ обязан сканироваться: самоодобрение запусков живёт в
        # agent/launcher.py, и до 09.2026 этот страж его просто не видел —
        # докстринг обещал падение на автономном запуске и лгал.
        *PROJECT_ROOT.glob("agent/*.py"),
    ):
        text = path.read_text(encoding="utf-8")
        for name in ("approve_by_system(", "approve_autonomously("):
            for match in re.finditer(re.escape(name), text):
                line = text.count("\n", 0, match.start()) + 1
                call_sites.append(f"{path.name}:{line}:{name}")

    modules = {item.split(":")[0] for item in call_sites}
    # owner_action_repository — определение и тонкая обёртка;
    # autonomous_pause — единственный вызывающий;
    # autopilot — единственная точка применения (пауза подтверждённого слива);
    # launcher — самоодобрение LAUNCH по постоянной директиве владельца
    # (OWNER_STANDING_DIRECTIVE_LAUNCH, флаг launch.auto_approve).
    # Любой НОВЫЙ модуль в этом множестве — отдельное решение владельца.
    assert modules == {
        "owner_action_repository.py",
        "autonomous_pause.py",
        "autopilot.py",
        "launcher.py",
    }, call_sites

    launcher_text = (PROJECT_ROOT / "agent" / "launcher.py").read_text(
        encoding="utf-8"
    )
    assert launcher_text.count("approve_by_system(") == 1

    autopilot_text = (PROJECT_ROOT / "services" / "autopilot.py").read_text(
        encoding="utf-8"
    )
    assert autopilot_text.count("approve_autonomously(") == 1


def test_scale_and_launch_producers_stay_proposal_only():
    """Подъём бюджета и запуск по-прежнему только предлагают."""
    from services.action_producer_gateway import ProducerActionOutcome

    assert ProducerActionOutcome(action="PROPOSAL_CREATED").confirmed is False


# ---------------------------------------------------------------------------
# Вечерняя сводка
# ---------------------------------------------------------------------------

def test_daily_summary_is_silent_without_actions():
    sender = Mock(return_value=True)
    with patch("services.telegram_bot.send_with_buttons", sender):
        assert autonomous.send_daily_summary(NOW) is False
    assert sender.call_count == 0


def test_daily_summary_reports_actions_with_undo_buttons():
    autonomous.record_autonomous_pause(
        {
            "ad_id": "120001",
            "id": "120001",
            "name": "Слив CityA",
            "spend": 450.0,
            "leads": 48,
            "cpl": 9.4,
            "qual_pct": 13.0,
            "payments": 0,
            "business_reason": "48 лидов, оплат 0 при расходе $450",
        },
        now=NOW,
    )
    sender = Mock(return_value=True)
    with patch("services.telegram_bot.send_with_buttons", sender):
        assert autonomous.send_daily_summary(NOW) is True

    text, buttons = sender.call_args[0]
    assert "Выключил сам: 1" in text
    assert "$450" in text
    assert buttons == [[("↩️ Вернуть «Слив CityA»", "undo_pause:120001")]]


def _summary_entry(index: int, **overrides) -> dict:
    """Запись журнала автономной паузы — сырьё для сводки."""
    entry = {
        "ad_id": f"12000{index}",
        "id": f"12000{index}",
        "name": f"CityA / Петров / Тема А {index}",
        "spend": 72.03,
        "leads": 8,
        "cpl": 8.25,
        "qual_pct": 0.0,
        "payments": 0,
        "business_reason": "8 лидов, ни одного квала",
    }
    entry.update(overrides)
    return entry


def test_daily_summary_fits_telegram_limit():
    """40 пауз — сводка влезает в лимит и режется целыми блоками.

    Лимит Telegram жёсткий: сообщение длиннее 4096 API отклоняет целиком, и
    владелец не узнаёт НИ ОБ ОДНОЙ паузе. Раньше обрезку держал слот-отчёт
    автопилота; после перехода на «днём тишина» список пауз ходит владельцу
    только этой сводкой — значит и обрезка обязана жить здесь.
    """
    from services.autopilot import _TELEGRAM_MAX_LEN

    text = autonomous.format_daily_summary([_summary_entry(i) for i in range(40)])

    assert len(text) <= _TELEGRAM_MAX_LEN, f"сводка не влезла: {len(text)} символов"
    assert "…и ещё" in text
    # Счётчик в шапке честный: выключено 40, сколько бы блоков ни поместилось
    assert "Выключил сам: 40" in text
    # Футер объясняет кнопки «Вернуть» — он обязан пережить обрезку
    assert "Вернуть" in text
    # Режем целыми блоками, а не по символам: у каждого уцелевшего блока
    # на месте все свои строки — деньги, квал и причина
    assert text.count("💸") == text.count("👥") == text.count("📉")


def test_daily_summary_has_no_score_or_creative_jargon():
    """В сводке нет score и творческого жаргона — она уходит владельцу как есть."""
    entries = [
        _summary_entry(1, score=3, reason="hook/ctr не выше медианы группы; +2: видео"),
        _summary_entry(2),
    ]
    lowered = autonomous.format_daily_summary(entries).lower()

    assert "score" not in lowered
    assert "hook" not in lowered
    assert "ctr" not in lowered


def test_undo_button_goes_through_proposal_path_not_direct_mutation():
    """«Вернуть» — предложение владельцу (propose_unpause), а не автомат."""
    source = (PROJECT_ROOT / "services" / "telegram_bot.py").read_text(encoding="utf-8")
    assert "propose_unpause" in source


def test_journal_keeps_only_recent_days():
    autonomous.save_state(
        {
            "runs": [],
            "journal": [{"ad_id": "old", "at": "2026-07-01T10:00:00+00:00"}],
        }
    )
    autonomous.record_autonomous_pause({"ad_id": "new"}, now=NOW)
    journal = autonomous.load_state()["journal"]
    assert [item["ad_id"] for item in journal] == ["new"]


# ---------------------------------------------------------------------------
# Аудит: самоодобрение в репозитории решений
# ---------------------------------------------------------------------------

def _init_db(tmp_path: Path) -> str:
    from agent.database import init_db

    return init_db(str(tmp_path / "decisions.db"), str(tmp_path / "missing.json"))


def _pause_plan(now: datetime):
    from services.owner_action_models import (
        EvidenceRecord,
        ProposalKind,
        ProposalOrigin,
        ProposedActionPlan,
        ProposedTarget,
        canonical_sha256,
    )

    payload = {"operation": "PAUSE_AD", "ad_id": "120001", "adset_id": "900001"}
    evidence_payload = {"ad_id": "120001", "active_ids": ["120001", "120002"]}
    return ProposedActionPlan(
        proposal_kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        idempotency_key="22222222-2222-4222-8222-222222222222",
        source_ref="autopilot-live:test:120001",
        actor="action_producer_gateway",
        summary="Пауза подтверждённого слива",
        targets=(
            ProposedTarget(
                claim_id="producer-test-claim",
                ordinal=0,
                action_kind="PAUSE_AD",
                account_id="1111",
                adset_id="900001",
                subject_id="120001",
                city="CityA",
                language="L1",
                intended_payload=payload,
                intended_payload_sha256=canonical_sha256(payload),
            ),
        ),
        evidence=(
            EvidenceRecord(
                evidence_kind="PRODUCER_LIVE_INVENTORY",
                source_system="FACEBOOK",
                subject_id="120001",
                observed_at=now,
                complete=True,
                payload=evidence_payload,
                payload_sha256=canonical_sha256(evidence_payload),
            ),
        ),
        config_version_sha256=canonical_sha256({"v": 1}),
        valid_until=now + timedelta(hours=48),
        staged_media_root=None,
    )


def test_system_approval_is_recorded_as_automatic(tmp_path):
    """Решение записано с явным признаком «автомат» и ставит задание в очередь."""
    db_path = _init_db(tmp_path)
    from services.owner_action_repository import OwnerActionRepository

    repository = OwnerActionRepository(db_path)
    receipt = repository.propose_action(_pause_plan(NOW), now=NOW)
    result = repository.approve_by_system(
        proposal_id=receipt.proposal_id,
        automation_rule=autonomous.AUTOMATION_RULE,
        actor=autonomous.SYSTEM_ACTOR,
        evidence={"spend_usd": 450.0, "payments": 0},
        now=NOW,
    )

    assert result.accepted is True
    assert result.reason_code == "SYSTEM_APPROVE"

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    decision = connection.execute(
        "SELECT * FROM owner_action_decisions WHERE proposal_id = ?",
        (receipt.proposal_id,),
    ).fetchone()
    assert decision["decision_source"] == "SYSTEM"
    assert decision["automation_rule"] == autonomous.AUTOMATION_RULE
    assert decision["decision_kind"] == "APPROVE"
    # Телеграм-родословной у автоматического решения нет и быть не может.
    assert decision["owner_user_id"] is None
    assert decision["callback_token_id"] is None
    assert decision["telegram_update_id"] is None

    job = connection.execute(
        "SELECT state FROM owner_execution_jobs WHERE proposal_id = ?",
        (receipt.proposal_id,),
    ).fetchone()
    assert job["state"] == "QUEUED"

    lifecycle = connection.execute(
        "SELECT state, latest_reason_code FROM owner_action_lifecycle WHERE proposal_id = ?",
        (receipt.proposal_id,),
    ).fetchone()
    assert lifecycle["state"] == "APPROVED"
    assert lifecycle["latest_reason_code"] == "SYSTEM_APPROVE"

    event = connection.execute(
        """
        SELECT actor, reason_code, payload_json
        FROM owner_action_events
        WHERE proposal_id = ? AND event_type = 'SYSTEM_APPROVE'
        """,
        (receipt.proposal_id,),
    ).fetchone()
    assert event["actor"] == autonomous.SYSTEM_ACTOR
    assert "AUTONOMOUS_PAUSE_CONFIRMED_WASTER" in event["payload_json"]
    assert "450" in event["payload_json"]
    connection.close()


def test_system_approval_refuses_after_proposal_reached_owner(tmp_path):
    """Предложение уже уехало владельцу → бот его не перехватывает."""
    db_path = _init_db(tmp_path)
    from services.owner_action_repository import (
        OwnerActionLifecycleConflict,
        OwnerActionRepository,
    )

    repository = OwnerActionRepository(db_path)
    receipt = repository.propose_action(_pause_plan(NOW), now=NOW)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        UPDATE owner_action_lifecycle
        SET state = 'PENDING_OWNER', version = version + 1
        WHERE proposal_id = ?
        """,
        (receipt.proposal_id,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(OwnerActionLifecycleConflict, match="PROPOSAL_NOT_SELF_APPROVABLE"):
        repository.approve_by_system(
            proposal_id=receipt.proposal_id,
            automation_rule=autonomous.AUTOMATION_RULE,
            actor=autonomous.SYSTEM_ACTOR,
            now=NOW,
        )


def test_system_approval_is_single_shot(tmp_path):
    db_path = _init_db(tmp_path)
    from services.owner_action_repository import (
        OwnerActionLifecycleConflict,
        OwnerActionRepository,
    )

    repository = OwnerActionRepository(db_path)
    receipt = repository.propose_action(_pause_plan(NOW), now=NOW)
    repository.approve_by_system(
        proposal_id=receipt.proposal_id,
        automation_rule=autonomous.AUTOMATION_RULE,
        actor=autonomous.SYSTEM_ACTOR,
        now=NOW,
    )
    with pytest.raises(OwnerActionLifecycleConflict):
        repository.approve_by_system(
            proposal_id=receipt.proposal_id,
            automation_rule=autonomous.AUTOMATION_RULE,
            actor=autonomous.SYSTEM_ACTOR,
            now=NOW,
        )


def test_system_decision_row_cannot_carry_telegram_lineage(tmp_path):
    """Схема не даёт смешать автоматическое решение с родословной владельца."""
    db_path = _init_db(tmp_path)
    from services.owner_action_repository import OwnerActionRepository

    repository = OwnerActionRepository(db_path)
    receipt = repository.propose_action(_pause_plan(NOW), now=NOW)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO owner_action_decisions (
                decision_id, proposal_id, proposal_sha256, decision_kind,
                owner_user_id, chat_id, message_id, delivery_generation,
                telegram_update_id, callback_query_id, callback_token_id,
                trusted_ingress_sha256, reason_text, recorded_at,
                decision_source, automation_rule
            ) VALUES ('d-1', ?, ?, 'APPROVE', 42, 1, 1, 1, 7, 'cq', 'tok',
                      ?, NULL, ?, 'SYSTEM', 'RULE')
            """,
            (
                receipt.proposal_id,
                receipt.proposal_sha256,
                "a" * 64,
                NOW.isoformat(),
            ),
        )
    connection.close()


def test_owner_decision_still_requires_full_telegram_lineage(tmp_path):
    """Путь владельца не ослаблен: строка OWNER без токена по-прежнему невозможна."""
    db_path = _init_db(tmp_path)
    from services.owner_action_repository import OwnerActionRepository

    repository = OwnerActionRepository(db_path)
    receipt = repository.propose_action(_pause_plan(NOW), now=NOW)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO owner_action_decisions (
                decision_id, proposal_id, proposal_sha256, decision_kind,
                reason_text, recorded_at, decision_source, automation_rule
            ) VALUES ('d-2', ?, ?, 'APPROVE', NULL, ?, 'OWNER', NULL)
            """,
            (receipt.proposal_id, receipt.proposal_sha256, NOW.isoformat()),
        )
    connection.close()


def test_training_export_separates_automatic_decisions(tmp_path):
    """Датасет обучения видит, что решение принял бот, а не владелец."""
    db_path = _init_db(tmp_path)
    from services.owner_action_repository import OwnerActionRepository
    from services.owner_training_export import _load_decisions

    repository = OwnerActionRepository(db_path)
    receipt = repository.propose_action(_pause_plan(NOW), now=NOW)
    repository.approve_by_system(
        proposal_id=receipt.proposal_id,
        automation_rule=autonomous.AUTOMATION_RULE,
        actor=autonomous.SYSTEM_ACTOR,
        now=NOW,
    )
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    documents = _load_decisions(connection, receipt.proposal_id)
    connection.close()

    assert len(documents) == 1
    assert documents[0]["decision_source"] == "SYSTEM"
    assert documents[0]["automation_rule"] == autonomous.AUTOMATION_RULE
    assert documents[0]["owner_user_id"] is None


def test_self_approval_failure_leaves_proposal_for_owner(tmp_path, caplog):
    """Отказ самоодобрения безопасен: предложение остаётся обычным."""
    from services.owner_action_repository import OwnerActionLifecycleConflict

    with patch(
        "services.owner_action_repository.approve_by_system",
        side_effect=OwnerActionLifecycleConflict("PROPOSAL_NOT_SELF_APPROVABLE"),
    ):
        assert autonomous.approve_autonomously("proposal-1") is False


def test_pauses_since_window_covers_evening_slots(tmp_path, monkeypatch):
    """Регрессия: сводка 9:00 берёт окно «с прошлой сводки», а не
    календарный день — иначе паузы вечерних слотов (14–22) не попадали бы
    ни в одну сводку вообще."""
    from datetime import datetime, timezone, timedelta

    from services import autonomous_pause as ap

    state_file = tmp_path / "autonomous_state.json"
    monkeypatch.setattr(ap, "STATE_FILE", state_file)

    now = datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)  # 09:00 по локальному времени
    yesterday_evening = now - timedelta(hours=12)            # вчера 16:00 UTC−?
    old = now - timedelta(days=2)

    ap.record_autonomous_pause({"ad_id": "evening-1", "name": "Вечерний слив"},
                               now=yesterday_evening)
    ap.record_autonomous_pause({"ad_id": "old-1", "name": "Старый"}, now=old)
    ap.record_autonomous_pause({"ad_id": "morning-1", "name": "Утренний"},
                               now=now - timedelta(hours=1))

    since = (now - timedelta(days=1)).isoformat()
    got = {e["ad_id"] for e in ap.autonomous_pauses_since(since)}
    assert got == {"evening-1", "morning-1"}, got


def test_full_autonomy_key_survives_config_merge(monkeypatch):
    """Регрессия: get_autopilot_config фильтрует блок autonomous по
    известным дефолтам — pause_all_candidates без записи в AUTONOMOUS_DEFAULTS
    молча выбрасывался при чтении: файл говорил true, рантайм видел False,
    и три дня владельцу шли карточки вместо итогов."""
    from services import autonomous_pause as ap

    monkeypatch.setattr(
        "agent.scheduler.load_settings",
        lambda: {"autopilot": {"autonomous": {
            "pause_confirmed_wasters": True,
            "pause_all_candidates": True,
        }}},
    )
    from services.autopilot import get_autopilot_config

    cfg = get_autopilot_config()
    assert ap.is_autonomous_pause_enabled(cfg) is True
    assert ap.is_full_pause_autonomy_enabled(cfg) is True


def test_journal_dedupes_by_ad_id(tmp_path, monkeypatch):
    """Регрессия («солянка»): слоты переписывали одну паузу по 2–4 раза,
    пока конвейер довозил исполнение. Повтор заменяет запись, не дописывает."""
    from datetime import datetime, timezone, timedelta

    from services import autonomous_pause as ap

    monkeypatch.setattr(ap, "STATE_FILE", tmp_path / "state.json")
    base = datetime(2026, 8, 25, 4, 0, tzinfo=timezone.utc)
    ap.record_autonomous_pause({"ad_id": "a1", "name": "X", "spend": 100}, now=base)
    ap.record_autonomous_pause(
        {"ad_id": "a1", "name": "X", "spend": 130}, now=base + timedelta(hours=2)
    )
    ap.record_autonomous_pause({"ad_id": "a2", "name": "Y", "spend": 5}, now=base)

    journal = ap.load_state()["journal"]
    assert len(journal) == 2
    a1 = next(item for item in journal if item["ad_id"] == "a1")
    assert a1["spend"] == 130  # свежая запись победила


def test_daily_summary_reports_only_confirmed_pauses():
    """Регрессия: журнал пишется при самоодобрении, а паузу
    конвейер мог не довезти — сводка обязана показывать только подтверждённые."""
    for ad_id, name in (("120001", "Довезённая"), ("120002", "Зависшая")):
        autonomous.record_autonomous_pause(
            {"ad_id": ad_id, "id": ad_id, "name": name, "spend": 100.0, "leads": 10,
             "cpl": 10.0, "qual_pct": 0.0, "payments": 0, "business_reason": "10 лидов, 0 квалов"},
            now=NOW,
        )
    sender = Mock(return_value=True)
    with patch("services.telegram_bot.send_with_buttons", sender), \
         patch.object(autonomous, "_confirmed_pause_ids", return_value={"120001"}):
        assert autonomous.send_daily_summary(NOW) is True

    text, buttons = sender.call_args[0]
    assert "Выключил сам: 1" in text
    assert "Довезённая" in text and "Зависшая" not in text
    assert buttons == [[("↩️ Вернуть «Довезённая»", "undo_pause:120001")]]


def test_daily_summary_is_silent_when_nothing_confirmed():
    autonomous.record_autonomous_pause(
        {"ad_id": "120009", "id": "120009", "name": "Зависшая", "spend": 50.0, "leads": 5,
         "cpl": 10.0, "qual_pct": 0.0, "payments": 0, "business_reason": "5 лидов, 0 квалов"},
        now=NOW,
    )
    sender = Mock(return_value=True)
    with patch("services.telegram_bot.send_with_buttons", sender), \
         patch.object(autonomous, "_confirmed_pause_ids", return_value=set()):
        assert autonomous.send_daily_summary(NOW) is False
    assert sender.call_count == 0
