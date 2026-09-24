"""
Тесты ядра автопилота (services/autopilot.py).

Автопилот — approval-first producer: он НЕ мутирует Facebook. Максимум, что он
делает при кандидате на отключение — создаёт PAUSE-proposal владельцу
(services.action_producer_gateway.propose_pause). Исполнение — только после
одобрения в Telegram через execution boundary. Поэтому тесты проверяют:
  (а) прямой FB-мутации не произошло (см. фикстуру `producer_boundary`);
  (б) создан корректный proposal (kind/target/origin) ЛИБО действие честно
      заблокировано (last-active guard, inventory_incomplete, RAIL и т.п.).

`result["paused"]` у автопилота теперь всегда пуст — реально применённые паузы
появляются только после одобрения; предложения лежат в `result["proposals"]`.

Мокаем все внешние зависимости:
- get_ads_with_metrics — FB API (light=True)
- sync_amo_data — AMO интеграция
- send_telegram, send_critical_alert — уведомления
- decisions_repo.save_decision — SQLite

STATE_FILE переопределяется через monkeypatch на tmp_path.
"""

import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import (
    blocked_outcome,
    install_proposal_recorder,
)

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта (чужой незакоммиченный код)
import sys as _sys
if "google.genai" not in _sys.modules:
    _sys.modules["google.genai"] = MagicMock()
if "google" not in _sys.modules:
    _sys.modules["google"] = MagicMock()


# ---------------------------------------------------------------------------
# Вспомогательные данные
# ---------------------------------------------------------------------------

_TZ = timezone(timedelta(hours=5))  # локальное время


def _make_ad(ad_id: str = "ad1", name: str = "Тест объявление", spend: float = 100.0,
             recommendation: str = "ОТКЛЮЧИТЬ", effective_status: str = "ACTIVE",
             leads: int = 3, cpl: float = 33.0, romi=None, qual_pct=None,
             city: str = "CityA", adset_type: str = "L2", ad_objective: str = "leadform",
             ctr: float = 1.0) -> dict:
    """Создаёт тестовое объявление.

    city/adset_type/ad_objective задают ключ группы для портфельного слоя.
    Чтобы объявление попало в кандидаты на паузу, оно должно быть в группе
    с явно лучшим аналогом (иначе портфельный слой защитит его).
    """
    return {
        "id": ad_id,
        "name": name,
        "effective_status": effective_status,
        "status": effective_status,
        "recommendation": recommendation,
        "reason": "Тестовая причина",
        "spend": spend,
        "leads": leads,
        "cpl": cpl,
        "ctr": ctr,
        "cpm": 5.0,
        "romi": romi,
        "qual_pct": qual_pct,
        "payments": None,
        "days_running": 10,
        "city": city,
        "adset_type": adset_type,
        "ad_objective": ad_objective,
    }


def _make_portfolio_group(n_bad: int = 4, base_id: int = 1,
                          city: str = "CityA", adset_type: str = "L2",
                          ad_objective: str = "leadform") -> list[dict]:
    """Возвращает группу объявлений одного портфельного сегмента.

    Содержит 1 явно хорошее (CPL=5, CTR=3.0, leads=15) + n_bad плохих
    (CPL=100-130, CTR=0.1, leads=8). Плохие имеют score_gap >> 0.15 от лучшего,
    поэтому портфельный слой отметит нижнюю половину группы как ОТКЛЮЧИТЬ.

    Все объявления принадлежат одной группе (city/adset_type/ad_objective).
    """
    good = _make_ad(
        ad_id=f"ad_good_{base_id}",
        name=f"Хорошее {base_id}",
        spend=80.0, leads=15, cpl=5.0, ctr=3.0,
        city=city, adset_type=adset_type, ad_objective=ad_objective,
    )
    bad_ads = [
        _make_ad(
            ad_id=f"ad_bad_{base_id}_{i}",
            name=f"Плохое {base_id}-{i}",
            spend=50.0, leads=8, cpl=100.0 + i * 5, ctr=0.1,
            city=city, adset_type=adset_type, ad_objective=ad_objective,
        )
        for i in range(n_bad)
    ]
    return [good] + bad_ads


def _make_5_candidates() -> list[dict]:
    """Группа из 7 объявлений, портфельный слой выдаёт >= 3 кандидатов (нижняя половина).

    Структура: 1 хорошее + 6 плохих в одной группе. При cap=3 автопилот ограничит
    паузы ровно тремя. Заменяет старый вариант с 5 одинаковыми объявлениями,
    который не проходил портфельный фильтр из-за нулевого score_gap.
    """
    return _make_portfolio_group(n_bad=6)


def _make_pause_inventory(adset_id: str, active_ids: set[str], candidate_ids: list[str]) -> dict:
    return {
        adset_id: {
            "adset_id": adset_id,
            "active_ids": active_ids,
            "candidate_context": {
                ad_id: {
                    "ad_id": ad_id,
                    "adset_id": adset_id,
                    "configured_status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
                for ad_id in candidate_ids
            },
            "complete": True,
            "pages_read": 1,
            "error": None,
        }
    }


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_state_file(tmp_path, monkeypatch):
    """Перенаправляет STATE_FILE и _AUTO_ACTIONS_FILE на tmp_path.

    D3: также перенаправляет _CLASSIC_DAILY_STATE_FILE (дневной счётчик пауз
    классического автопилота) — иначе тесты читают/пишут реальный
    data/autopilot_classic_daily.json и накопленное состояние с прошлых
    прогонов (pauses_today) искажает результаты active-режима.
    """
    import services.autopilot as ap_module
    state_path = tmp_path / "autopilot_state.json"
    actions_path = tmp_path / "auto_actions.json"
    classic_daily_path = tmp_path / "autopilot_classic_daily.json"
    live_daily_path = tmp_path / "autopilot_live_daily.json"
    monkeypatch.setattr(ap_module, "STATE_FILE", state_path)
    monkeypatch.setattr(ap_module, "_AUTO_ACTIONS_FILE", actions_path)
    monkeypatch.setattr(ap_module, "_CLASSIC_DAILY_STATE_FILE", classic_daily_path)
    monkeypatch.setattr(ap_module, "_LIVE_DAILY_STATE_FILE", live_daily_path)
    yield state_path


@pytest.fixture(autouse=True)
def patch_refresh_statuses(request):
    """По умолчанию мокаем refresh_statuses_in_place (no-op) во всех тестах.

    Тесты, которые специально проверяют поведение refresh, делают patch вручную
    через patch("agent.analyzer.refresh_statuses_in_place", ...) — он перекроет
    этот autouse-мок благодаря порядку применения патчей.
    """
    # Если тест явно помечен как refresh-тест — пропускаем autouse-мок.
    # В остальных случаях refresh работает как no-op (не меняет статусы, не падает).
    with patch("agent.analyzer.refresh_statuses_in_place"):
        yield


@pytest.fixture(autouse=True)
def producer_boundary(tmp_path, monkeypatch):
    """Единая producer-граница для всех тестов автопилота.

    - Изолирует от сети: live inventory и exact-контексты отдаются фейком, у
      каждого кандидата по умолчанию есть доказанная ACTIVE-замена (иначе
      propose_pause справедливо отказал бы с LAST_EFFECTIVE_ACTIVE).
    - Оставляет живой всю discovery/guard-логику propose_pause и подменяет
      только запись proposal в БД, чтобы тест мог проверить kind/target/origin.
    - Вешает Mock'и на реальные FB-мутаторы и на execution boundary: любой
      прямой вызов оттуда роняет тест.

    Guard и producer читают ОДИН И ТОТ ЖЕ inventory: patch
    services.adset_pause_guard.fetch_pause_inventory в конкретном тесте меняет
    картину мира и для propose_pause. Иначе тест мог бы описать «в adset один
    ACTIVE» для guard и «есть замена» для producer — в проде такого расхождения
    нет, и last-active guard проверялся бы вхолостую.
    Полный FB/paging контракт покрыт в test_adset_pause_guard.py.

    Возвращает RecordedProposals — что автопилот предложил владельцу.
    """
    from services import adset_pause_guard

    def fake_inventory(ad_ids):
        inventories = {}
        for index, ad_id in enumerate(ad_ids):
            adset_id = str(900000 + index)
            replacement_id = f"replacement-{ad_id}"
            inventory_context = {
                current_id: {
                    "ad_id": current_id,
                    "adset_id": adset_id,
                    "name": current_id,
                    "configured_status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
                for current_id in (ad_id, replacement_id)
            }
            inventories[adset_id] = {
                "adset_id": adset_id,
                "active_ids": {ad_id, replacement_id},
                "candidate_context": {
                    ad_id: inventory_context[ad_id]
                },
                "inventory_context": inventory_context,
                "complete": True,
                "pages_read": 1,
                "error": None,
            }
        return inventories

    def current_inventory(ad_ids):
        """Читает inventory через актуальный (возможно, замоканный тестом) guard."""
        return adset_pause_guard.fetch_pause_inventory(list(ad_ids))

    def fake_exact_contexts(ad_ids, *, require_names=True):
        del require_names
        inventories = current_inventory(ad_ids)
        contexts = {
            ad_id: context
            for inventory in inventories.values()
            for ad_id, context in (inventory.get("candidate_context") or {}).items()
            if ad_id in set(ad_ids)
        }
        return contexts, None

    monkeypatch.setattr(adset_pause_guard, "fetch_pause_inventory", fake_inventory)
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory",
        current_inventory,
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts",
        fake_exact_contexts,
    )
    return install_proposal_recorder(monkeypatch, tmp_path)


@pytest.fixture
def enabled_dry_run_cfg():
    """Конфиг: включён, режим dry_run."""
    return {
        "enabled": True,
        "mode": "dry_run",
        "max_pauses_per_run": 3,
        "min_hours_between_runs": 3,
    }


@pytest.fixture
def enabled_active_cfg():
    """Конфиг: включён, режим active."""
    return {
        "enabled": True,
        "mode": "active",
        "max_pauses_per_run": 3,
        "min_hours_between_runs": 3,
    }


# ---------------------------------------------------------------------------
# Тест 1: disabled → ran=False
# ---------------------------------------------------------------------------

def test_disabled_returns_ran_false():
    """Если enabled=False — автопилот не запускается."""
    disabled_cfg = {**{"enabled": False, "mode": "dry_run", "max_pauses_per_run": 3, "min_hours_between_runs": 3}}
    with patch("services.autopilot.get_autopilot_config", return_value=disabled_cfg):
        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    assert result["ran"] is False
    assert result["skipped_reason"] == "disabled"


# ---------------------------------------------------------------------------
# Тест 2: dry_run не создаёт proposal, но пишет DRY_RUN
# ---------------------------------------------------------------------------

def test_dry_run_creates_no_proposal_but_writes_dry_run(enabled_dry_run_cfg, producer_boundary):
    """В dry_run владельцу ничего не предлагается, но save_decision пишет DRY_RUN.

    Портфельный слой требует группы с явным лучшим аналогом, поэтому
    даём группу: 1 хорошее + 4 плохих. Нижняя половина получит ОТКЛЮЧИТЬ.
    """
    # Группа: 1 хорошее + 4 плохих → портфель выдаст кандидатов
    ads = _make_portfolio_group(n_bad=4)

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision") as mock_save, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["ran"] is True
    assert result["mode"] == "dry_run"
    # save_decision должен был вызваться с action=DRY_RUN
    assert mock_save.called
    call_kwargs = mock_save.call_args
    assert "DRY_RUN" in call_kwargs.args or call_kwargs.kwargs.get("action") == "DRY_RUN" or \
           (len(call_kwargs.args) >= 4 and call_kwargs.args[3] == "DRY_RUN")


# ---------------------------------------------------------------------------
# Тест 3: active создаёт ровно cap предложений при 5 кандидатах
# ---------------------------------------------------------------------------

def test_active_creates_exactly_cap_proposals(enabled_active_cfg, producer_boundary):
    """active + 5 кандидатов + cap=3 → создано ровно 3 PAUSE-proposal.

    Ни одной прямой FB-мутации: реально паузит только execution boundary
    после одобрения владельца, поэтому `paused` остаётся пустым.
    """
    ads = _make_5_candidates()
    enabled_active_cfg["max_pauses_per_run"] = 3

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_active_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    assert len(producer_boundary.plans) == 3
    assert len(result["proposals"]) == 3
    assert set(producer_boundary.kinds) == {ProposalKind.PAUSE}
    assert set(producer_boundary.origins) == {ProposalOrigin.AUTOPILOT}
    assert set(producer_boundary.action_kinds) == {"PAUSE_AD"}
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []
    assert result["ran"] is True


# ---------------------------------------------------------------------------
# Тест 4: amo_ok=False блокирует паузы в active и шлёт critical alert
# ---------------------------------------------------------------------------

def test_amo_unavailable_blocks_pauses_in_active(enabled_active_cfg, producer_boundary):
    """Если AMO недоступна — предложения не создаются и шлётся critical alert.

    Портфельный слой выдаёт кандидатов только при наличии группы с лучшим аналогом.
    После портфельного слоя у кандидатов AMO-данные отсутствуют (romi/qual_pct=None),
    поэтому amo_ok=False → RAIL срабатывает.
    """
    # Группа с кандидатами: 1 хорошее + 4 плохих
    ads = _make_portfolio_group(n_bad=4)

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_active_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", side_effect=Exception("AMO timeout")), \
         patch("agent.repositories.amo_repo.get_amo_data", side_effect=Exception("файл не найден")), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert") as mock_alert:

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    # Ни proposal, ни мутации: без AMO-данных решение недоказуемо
    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    # critical alert отправлен
    mock_alert.assert_called_once()
    alert_args = mock_alert.call_args.args
    assert "AMO" in alert_args[0]
    assert result["paused"] == []
    assert result["skipped_reason"] == "amo_unavailable"


# ---------------------------------------------------------------------------
# Тест 5: manual_override исключает объявление
# ---------------------------------------------------------------------------

def test_manual_override_excludes_ad(enabled_active_cfg, producer_boundary):
    """Объявление с активным override не попадает в to_act.

    Группа из 3 объявлений одного сегмента:
    - ad1 (хорошее, CPL=5) — защищён manual_override
    - ad2 (плохое, CPL=120) — должен попасть в предложение владельцу
    - ad3 (плохое, CPL=130) — тоже попадёт в кандидаты

    ad1 исключается из to_act через override — proposal на него не создаётся.
    """
    until_future = (datetime.now(timezone(timedelta(hours=5))) + timedelta(days=3)).isoformat()
    # Все три в одной группе: ad1 хорошее, ad2/ad3 плохие
    ad1 = _make_ad("ad1", "Хорошее объявление", spend=80.0, leads=15, cpl=5.0, ctr=3.0,
                   city="CityA", adset_type="L2", ad_objective="leadform")
    ad2 = _make_ad("ad2", "Плохое объявление 2", spend=50.0, leads=8, cpl=120.0, ctr=0.1,
                   city="CityA", adset_type="L2", ad_objective="leadform")
    ad3 = _make_ad("ad3", "Плохое объявление 3", spend=50.0, leads=8, cpl=130.0, ctr=0.1,
                   city="CityA", adset_type="L2", ad_objective="leadform")
    ads = [ad1, ad2, ad3]

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_active_cfg), \
         patch("services.autopilot._load_state", return_value={
             "last_run_at": None,
             "last_run_window": None,
             "manual_overrides": {"ad1": until_future},  # ad1 защищён
         }), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    # ad1 защищён override — proposal на него не создан
    assert "ad1" not in producer_boundary.subject_ids
    # Хотя бы одно из ad2/ad3 предложено владельцу
    assert len(producer_boundary.plans) >= 1
    assert set(producer_boundary.subject_ids) <= {"ad2", "ad3"}
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []


# ---------------------------------------------------------------------------
# Тест 6: гейт min_hours — повторный cron skip, manual не skip
# ---------------------------------------------------------------------------

def test_cron_gate_skips_within_min_hours(enabled_dry_run_cfg):
    """Повторный cron-запуск через 1 час при min_hours_between_runs=3 → skip."""
    last_run = (datetime.now(timezone(timedelta(hours=5))) - timedelta(hours=1)).isoformat()

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={
             "last_run_at": last_run,
             "last_run_window": None,
             "manual_overrides": {},
         }):

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="cron")

    assert result["ran"] is False
    assert result["skipped_reason"] is not None
    assert "слишком рано" in result["skipped_reason"]


def test_manual_trigger_ignores_time_gate(enabled_dry_run_cfg):
    """manual-триггер не проверяет гейт по времени — выполняется даже через 1 минуту."""
    last_run = (datetime.now(timezone(timedelta(hours=5))) - timedelta(minutes=1)).isoformat()
    ads = [_make_ad("ad1")]

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={
             "last_run_at": last_run,
             "last_run_window": None,
             "manual_overrides": {},
         }), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    assert result["ran"] is True


# ---------------------------------------------------------------------------
# Тест 7: ошибка producer на одном объявлении останавливает остаток батча
# ---------------------------------------------------------------------------

def test_pause_error_does_not_block_other_proposals(enabled_active_cfg, producer_boundary):
    """Сбой producer на одном объявлении не мешает предложить остальные.

    Группа из 4 объявлений: 1 хорошее + 3 плохих. Портфель выдаст >=2 кандидата
    (нижняя половина). Плохие объявления отсортированы по spend desc, поэтому
    первым идёт ad_bad_1_0 — на нём propose_pause падает. Следующий за ним
    ad_bad_1_1 должен всё равно попасть в предложения владельцу, а прямой
    FB-мутации не происходит ни в одном из случаев.
    """
    ads = _make_portfolio_group(n_bad=3)
    enabled_active_cfg["max_pauses_per_run"] = 5  # разрешаем предлагать всех

    # bad ads имеют id вида ad_bad_1_0, ad_bad_1_1, ad_bad_1_2
    first_bad_id = "ad_bad_1_0"
    second_bad_id = "ad_bad_1_1"

    real_propose = None

    def flaky_propose(ad_id, **kwargs):
        if ad_id == first_bad_id:
            raise RuntimeError("FB API error")
        return real_propose(ad_id, **kwargs)

    import services.action_producer_gateway as producer_module
    real_propose = producer_module.propose_pause

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_active_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("services.action_producer_gateway.propose_pause", side_effect=flaky_propose), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    # Упавший кандидат не предложен, ошибка зафиксирована
    assert first_bad_id not in producer_boundary.subject_ids
    assert any(first_bad_id in error for error in result["errors"])
    # Следующий кандидат всё равно предложен владельцу
    assert second_bad_id in producer_boundary.subject_ids
    # Ни одной прямой мутации и ни одной «применённой» паузы
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []


# ---------------------------------------------------------------------------
# FIX 4: kill_switch — классический автопилот (_run_autopilot_inner)
# ---------------------------------------------------------------------------

def test_classic_kill_switch_returns_skipped_reason(producer_boundary):
    """kill_switch=true в классическом автопилоте → skipped_reason='kill_switch',
    proposal не создаётся (зеркало test_live_kill_switch_returns_skipped)."""
    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": True, "mode": "active",
        "max_pauses_per_run": 3, "min_hours_between_runs": 3,
    }):
        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    assert result["ran"] is False
    assert result["skipped_reason"] == "kill_switch"
    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()


# ---------------------------------------------------------------------------
# FIX 5-alert: отказ producer в active-пути шлёт Telegram-алерт
# ---------------------------------------------------------------------------

def test_classic_alert_when_producer_blocked(enabled_active_cfg, producer_boundary):
    """Неполный live inventory → propose_pause fail-closed, владелец узнаёт из TG.

    Раньше «провалом» была неудачная FB-мутация. Теперь мутации нет вообще:
    провал — это невозможность честно создать proposal. Инвариант тот же —
    ни одного тихого пропуска, владельцу уходит явное сообщение об ошибках.
    """
    ads = _make_portfolio_group(n_bad=4)
    enabled_active_cfg["max_pauses_per_run"] = 5  # разрешаем предлагать всех кандидатов

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_active_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch(
             "services.action_producer_gateway.fetch_pause_inventory",
             return_value={"900000": {"complete": False, "error": "PAGINATION_INCOMPLETE"}},
         ), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram") as mock_telegram, \
         patch("services.notifications.send_critical_alert"):

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    # Ни одного proposal, ни одной мутации — fail-closed
    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["proposals"] == []
    assert result["paused"] == []
    assert any("PAGINATION_INCOMPLETE" in error for error in result["errors"])
    # Среди вызовов send_telegram должен быть алерт про непримененные паузы
    alert_calls = [
        c for c in mock_telegram.call_args_list
        if c.args and "НЕ применились" in c.args[0]
    ]
    assert alert_calls, f"Не найден алерт о провале пауз среди вызовов: {mock_telegram.call_args_list}"


# ---------------------------------------------------------------------------
# Тест 8: add_manual_override + get_active_overrides
# ---------------------------------------------------------------------------

def test_add_manual_override_and_get_active(tmp_path, monkeypatch):
    """add_manual_override добавляет запись, get_active_overrides возвращает её."""
    import services.autopilot as ap_module
    state_path = tmp_path / "autopilot_state.json"
    monkeypatch.setattr(ap_module, "STATE_FILE", state_path)

    ap_module.add_manual_override("ad_test", days=7)
    active = ap_module.get_active_overrides()

    assert "ad_test" in active
    # until должен быть в будущем
    until = datetime.fromisoformat(active["ad_test"])
    now = datetime.now(timezone(timedelta(hours=5)))
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone(timedelta(hours=5)))
    assert until > now


# ---------------------------------------------------------------------------
# Тест 9: history_flag — информационный флаг, не меняет решение (§9 спеки)
# ---------------------------------------------------------------------------

def test_history_flag_informational_only(enabled_dry_run_cfg):
    """history_flag попадает в сводку кандидатов, но НЕ влияет на решение autopilot.

    Проверяем:
    (а) поле history_flag=True присутствует в candidates результата run_autopilot
    (б) кандидат всё равно отобран — решение определяется портфельным слоем,
        а не history_flag

    Портфельный слой требует группы с явно лучшим аналогом, иначе одиночку
    защитит правило «последнее в группе». Используем группу из 1 хорошего + 3 плохих;
    плохое «История плохого объявления» попадёт в нижнюю половину ранга.
    """
    # Одна группа: 1 хорошее + 3 плохих. ad_hist — одно из плохих.
    ad_good = _make_ad("ad_good", "Хорошее объявление", spend=80.0, leads=15, cpl=5.0, ctr=3.0,
                       city="CityA", adset_type="L2", ad_objective="leadform")
    ad_hist = _make_ad("ad_hist", "История плохого объявления", spend=50.0, leads=8, cpl=120.0, ctr=0.1,
                       city="CityA", adset_type="L2", ad_objective="leadform")
    ad_bad2 = _make_ad("ad_bad2", "Ещё плохое", spend=50.0, leads=8, cpl=125.0, ctr=0.1,
                       city="CityA", adset_type="L2", ad_objective="leadform")
    ads = [ad_good, ad_hist, ad_bad2]

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "плохой CPL"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.autopilot._history_flag", return_value=True) as mock_flag:

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    # (а) кандидаты есть — портфельный слой выдал нижнюю половину группы
    assert len(result["candidates"]) >= 1
    # (а) history_flag=True отражён в каждой сводке (мок всегда возвращает True)
    for candidate in result["candidates"]:
        assert candidate["history_flag"] is True
    # (б) решение не изменилось — кандидаты отобраны
    assert result["ran"] is True
    assert result["mode"] == "dry_run"
    # _history_flag вызывался (сводка строилась через _ad_summary)
    assert mock_flag.called


def test_get_active_overrides_filters_expired(tmp_path, monkeypatch):
    """get_active_overrides не возвращает протухшие записи."""
    import services.autopilot as ap_module
    state_path = tmp_path / "autopilot_state.json"
    monkeypatch.setattr(ap_module, "STATE_FILE", state_path)

    # Сохраняем протухшую запись
    expired = (datetime.now(timezone(timedelta(hours=5))) - timedelta(days=1)).isoformat()
    state_path.write_text(json.dumps({
        "last_run_at": None,
        "last_run_window": None,
        "manual_overrides": {"old_ad": expired},
    }))

    active = ap_module.get_active_overrides()
    assert "old_ad" not in active


# ---------------------------------------------------------------------------
# Тест 12: get_ads_with_metrics падает → кэш свежий → dry_run завершается, source=="cache"
# ---------------------------------------------------------------------------

def test_cache_fallback_dry_run_uses_cache_when_live_fails(enabled_dry_run_cfg, tmp_path, monkeypatch, producer_boundary):
    """get_ads_with_metrics кидает ошибку → свежий кэш → dry_run прогон завершается, source=='cache'.

    Кэш содержит группу с кандидатами (1 хорошее + 4 плохих), чтобы портфельный
    слой выдал кандидатов и dry_run отправил Telegram с пометкой кэша.
    """
    import services.autopilot as ap_module
    import time as time_module

    # Группа с кандидатами: портфель выдаст нижнюю половину плохих
    ads = _make_portfolio_group(n_bad=4)
    now_dt = datetime.now(timezone(timedelta(hours=5)))
    date_to = now_dt.strftime("%Y-%m-%d")
    date_from = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d")

    disk_cache_path = tmp_path / "analytics_cache.json"
    disk_cache_path.write_text(json.dumps({
        "data": ads,
        "date_from": date_from,
        "date_to": date_to,
        "saved_at": time_module.time(),  # только что сохранён → свежий
    }, ensure_ascii=False))

    # Перенаправляем _DISK_CACHE_PATH на tmp
    monkeypatch.setattr(ap_module, "_DISK_CACHE_PATH", disk_cache_path)

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", side_effect=Exception("Please reduce the amount of data")), \
         patch("web.app.get_cached_analytics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert"):

        result = ap_module.run_autopilot(trigger="manual")

    assert result["ran"] is True, f"ожидали ran=True, got: {result}"
    assert result.get("source") == "cache", f"ожидали source='cache', got: {result.get('source')}"
    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    # Telegram-сообщение должно содержать пометку об источнике кэша
    if mock_tg.called:
        tg_text = mock_tg.call_args.args[0]
        assert "кэш" in tg_text.lower()


# ---------------------------------------------------------------------------
# Тест 13: кэш старше 3ч → ran=False (fallback недоступен)
# ---------------------------------------------------------------------------

def test_cache_fallback_fails_when_cache_too_old(enabled_dry_run_cfg, tmp_path, monkeypatch):
    """Если кэш старше 3 часов — fallback недоступен, ran=False."""
    import services.autopilot as ap_module
    import time as time_module

    now_dt = datetime.now(timezone(timedelta(hours=5)))
    date_to = now_dt.strftime("%Y-%m-%d")
    date_from = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d")

    ads = [_make_ad("ad1")]

    # Кэш сохранён 4 часа назад → устарел
    disk_cache_path = tmp_path / "analytics_cache.json"
    disk_cache_path.write_text(json.dumps({
        "data": ads,
        "date_from": date_from,
        "date_to": date_to,
        "saved_at": time_module.time() - 4 * 3600,  # 4 часа назад
    }, ensure_ascii=False))

    monkeypatch.setattr(ap_module, "_DISK_CACHE_PATH", disk_cache_path)

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", side_effect=Exception("Please reduce the amount of data")), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        result = ap_module.run_autopilot(trigger="manual")

    assert result["ran"] is False, f"ожидали ran=False при устаревшем кэше, got: {result}"


# ---------------------------------------------------------------------------
# Тест 14: active + source=="cache" → pause_ad НЕ вызван, alert вызван
# ---------------------------------------------------------------------------

def test_cache_source_blocks_pauses_in_active(enabled_active_cfg, tmp_path, monkeypatch, producer_boundary):
    """В active-режиме при данных из кэша паузы не выполняются, шлётся critical alert.

    Кэш содержит группу с кандидатами (1 хорошее + 4 плохих), чтобы портфельный
    слой выдал to_act и RAIL cache_source_active_blocked сработал.
    """
    import services.autopilot as ap_module
    import time as time_module

    # Группа с кандидатами: портфель выдаст нижнюю половину плохих
    ads = _make_portfolio_group(n_bad=4)
    now_dt = datetime.now(timezone(timedelta(hours=5)))
    date_to = now_dt.strftime("%Y-%m-%d")
    date_from = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d")

    # Свежий кэш
    disk_cache_path = tmp_path / "analytics_cache.json"
    disk_cache_path.write_text(json.dumps({
        "data": ads,
        "date_from": date_from,
        "date_to": date_to,
        "saved_at": time_module.time(),  # только что сохранён
    }, ensure_ascii=False))

    monkeypatch.setattr(ap_module, "_DISK_CACHE_PATH", disk_cache_path)

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_active_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", side_effect=Exception("FB rate limit")), \
         patch("web.app.get_cached_analytics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert") as mock_alert:

        result = ap_module.run_autopilot(trigger="manual")

    # Паузы запрещены при cache source в active-режиме
    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    # Critical alert должен был отправиться
    mock_alert.assert_called_once()
    assert result["paused"] == []
    assert result.get("source") == "cache"


# ---------------------------------------------------------------------------
# Тест 15: успех без кандидатов → send_telegram с "кандидатов на отключение нет"
# ---------------------------------------------------------------------------

def test_no_candidates_sends_telegram_message(enabled_dry_run_cfg):
    """Если анализ прошёл, но кандидатов нет — Telegram получает сообщение об этом."""
    # Объявление НЕ рекомендовано к отключению
    ads = [_make_ad("ad1", recommendation="НАБЛЮДАТЬ", spend=50.0)]

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "НАБЛЮДАТЬ", "reason": "норм"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert"):

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    assert result["ran"] is True
    assert result["candidates"] == []
    # send_telegram вызван с сообщением о нулевых кандидатах
    mock_tg.assert_called_once()
    tg_text = mock_tg.call_args.args[0]
    assert "кандидатов на отключение нет" in tg_text


# ---------------------------------------------------------------------------
# Тест 16: нет данных (live упал, кэш отсутствует) → send_telegram с "пропущен"
# ---------------------------------------------------------------------------

def test_no_data_sends_telegram_skipped(enabled_dry_run_cfg, tmp_path, monkeypatch):
    """Если live упал и кэша нет — Telegram получает сообщение о пропуске."""
    import services.autopilot as ap_module

    # Указываем несуществующий путь к кэшу
    monkeypatch.setattr(ap_module, "_DISK_CACHE_PATH", tmp_path / "nonexistent.json")

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", side_effect=Exception("FB timeout")), \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert"):

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    assert result["ran"] is False
    # send_telegram вызван с упоминанием "пропущен"
    mock_tg.assert_called_once()
    tg_text = mock_tg.call_args.args[0]
    assert "пропущен" in tg_text


# ---------------------------------------------------------------------------
# Тест 17: disabled → send_telegram НЕ вызван
# ---------------------------------------------------------------------------

def test_disabled_does_not_send_telegram():
    """Если автопилот disabled — никаких Telegram-сообщений не шлётся."""
    disabled_cfg = {"enabled": False, "mode": "dry_run", "max_pauses_per_run": 3, "min_hours_between_runs": 3}

    with patch("services.autopilot.get_autopilot_config", return_value=disabled_cfg), \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert"):

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    assert result["ran"] is False
    assert result["skipped_reason"] == "disabled"
    mock_tg.assert_not_called()


# ---------------------------------------------------------------------------
# Тест 18: live-источник зовёт get_ads_with_metrics с light=True
# ---------------------------------------------------------------------------

def test_live_source_uses_get_ads_with_metrics_light(enabled_dry_run_cfg):
    """_get_ads_for_analysis вызывает get_ads_with_metrics(light=True), не analyze_all."""
    ads = [_make_ad("ad1", recommendation="НАБЛЮДАТЬ")]

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads) as mock_light, \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "НАБЛЮДАТЬ", "reason": "норм"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.autopilot import run_autopilot
        run_autopilot(trigger="manual")

    # Проверяем что вызов был с light=True
    mock_light.assert_called_once()
    call_kwargs = mock_light.call_args
    # light=True может быть передан позиционно или именованно
    passed_light = call_kwargs.kwargs.get("light", None)
    if passed_light is None and len(call_kwargs.args) >= 3:
        passed_light = call_kwargs.args[2]
    assert passed_light is True, f"ожидали light=True, получили kwargs={call_kwargs.kwargs}, args={call_kwargs.args}"


# ---------------------------------------------------------------------------
# Тест 19: superset-кэш (период шире) + dry_run → ran=True, source=="cache"
# ---------------------------------------------------------------------------

def test_superset_cache_dry_run(enabled_dry_run_cfg, tmp_path, monkeypatch, producer_boundary):
    """Кэш покрывает более широкий период (superset) → dry_run завершается, source=='cache'.

    Кэш содержит группу с кандидатами (1 хорошее + 4 плохих), чтобы Telegram
    получил сообщение с пометкой кэша (отправляется только при наличии to_act).
    """
    import services.autopilot as ap_module
    import time as time_module

    # Группа с кандидатами: портфель выдаст нижнюю половину плохих
    ads = _make_portfolio_group(n_bad=4)
    now_dt = datetime.now(_TZ)
    date_to = now_dt.strftime("%Y-%m-%d")
    # Запрашиваем последние 7 дней
    date_from = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d")

    # Кэш покрывает более широкий период: с 2026-06-01 по сегодня (superset)
    cache_from = (now_dt - timedelta(days=10)).strftime("%Y-%m-%d")
    cache_to = date_to  # совпадает

    disk_cache_path = tmp_path / "analytics_cache.json"
    disk_cache_path.write_text(json.dumps({
        "data": ads,
        "date_from": cache_from,   # шире запрошенного
        "date_to": cache_to,
        "saved_at": time_module.time(),
    }, ensure_ascii=False))

    monkeypatch.setattr(ap_module, "_DISK_CACHE_PATH", disk_cache_path)

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", side_effect=Exception("CPU budget exceeded")), \
         patch("web.app.get_cached_analytics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert"):

        result = ap_module.run_autopilot(trigger="manual")

    assert result["ran"] is True, f"ожидали ran=True, got: {result}"
    assert result.get("source") == "cache", f"ожидали source='cache', got: {result.get('source')}"
    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    # Telegram-сообщение должно содержать период кэша
    if mock_tg.called:
        tg_text = mock_tg.call_args.args[0]
        assert "кэш" in tg_text.lower(), f"ожидали 'кэш' в Telegram-сообщении: {tg_text}"


# ---------------------------------------------------------------------------
# Тест 20: superset-кэш + active → паузы заблокированы (RAIL)
# ---------------------------------------------------------------------------

def test_superset_cache_active_blocks_pauses(enabled_active_cfg, tmp_path, monkeypatch, producer_boundary):
    """Superset-кэш в active-режиме: RAIL блокирует паузы, шлётся critical alert.

    Кэш содержит группу с кандидатами (1 хорошее + 4 плохих), чтобы to_act
    был непустым и RAIL cache_source_active_blocked сработал.
    """
    import services.autopilot as ap_module
    import time as time_module

    # Группа с кандидатами: портфель выдаст нижнюю половину плохих
    ads = _make_portfolio_group(n_bad=4)
    now_dt = datetime.now(_TZ)
    date_to = now_dt.strftime("%Y-%m-%d")
    date_from = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d")

    # Superset: кэш шире запрошенного периода
    cache_from = (now_dt - timedelta(days=10)).strftime("%Y-%m-%d")

    disk_cache_path = tmp_path / "analytics_cache.json"
    disk_cache_path.write_text(json.dumps({
        "data": ads,
        "date_from": cache_from,
        "date_to": date_to,
        "saved_at": time_module.time(),
    }, ensure_ascii=False))

    monkeypatch.setattr(ap_module, "_DISK_CACHE_PATH", disk_cache_path)

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_active_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", side_effect=Exception("CPU budget exceeded")), \
         patch("web.app.get_cached_analytics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert") as mock_alert:

        result = ap_module.run_autopilot(trigger="manual")

    # RAIL: паузы запрещены при кэш-источнике в active-режиме
    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    mock_alert.assert_called_once()
    assert result["paused"] == []
    assert result.get("source") == "cache"
    assert result.get("skipped_reason") == "cache_source_active_blocked"


# ---------------------------------------------------------------------------
# Тест 21: кэш НЕ покрывает запрошенный период (subset) → отвергнут, ran=False
# ---------------------------------------------------------------------------

def test_subset_cache_rejected(enabled_dry_run_cfg, tmp_path, monkeypatch):
    """Кэш покрывает МЕНЬШИЙ период (cache_from > date_from) → отвергнут, ran=False."""
    import services.autopilot as ap_module
    import time as time_module

    ads = [_make_ad("ad1")]
    now_dt = datetime.now(_TZ)
    date_to = now_dt.strftime("%Y-%m-%d")
    # Запрашиваем 10 дней
    date_from = (now_dt - timedelta(days=10)).strftime("%Y-%m-%d")

    # Кэш: только 5 дней (уже запрошенного периода) → не покрывает
    cache_from = (now_dt - timedelta(days=5)).strftime("%Y-%m-%d")

    disk_cache_path = tmp_path / "analytics_cache.json"
    disk_cache_path.write_text(json.dumps({
        "data": ads,
        "date_from": cache_from,   # cache_from > date_from → не покрывает
        "date_to": date_to,
        "saved_at": time_module.time(),
    }, ensure_ascii=False))

    monkeypatch.setattr(ap_module, "_DISK_CACHE_PATH", disk_cache_path)

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", side_effect=Exception("FB rate limit")), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        result = ap_module.run_autopilot(trigger="manual")

    assert result["ran"] is False, f"ожидали ran=False при subset-кэше, got: {result}"


# ---------------------------------------------------------------------------
# Тест 22: двойной провал → skipped_reason содержит и live-, и cache-причину
# ---------------------------------------------------------------------------

def test_double_failure_skipped_reason_contains_both_errors(enabled_dry_run_cfg, tmp_path, monkeypatch):
    """При падении live и кэша skipped_reason содержит обе причины."""
    import services.autopilot as ap_module
    import time as time_module

    now_dt = datetime.now(_TZ)
    date_to = now_dt.strftime("%Y-%m-%d")
    date_from = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d")

    # Кэш: subset (cache_from > date_from) → не покрывает → отвергнут
    cache_from = (now_dt - timedelta(days=3)).strftime("%Y-%m-%d")
    disk_cache_path = tmp_path / "analytics_cache.json"
    disk_cache_path.write_text(json.dumps({
        "data": [],
        "date_from": cache_from,
        "date_to": date_to,
        "saved_at": time_module.time(),
    }, ensure_ascii=False))

    monkeypatch.setattr(ap_module, "_DISK_CACHE_PATH", disk_cache_path)

    live_error_text = "FB API: ошибка авторизации"

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", side_effect=Exception(live_error_text)), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        result = ap_module.run_autopilot(trigger="manual")

    assert result["ran"] is False
    reason = result.get("skipped_reason", "")
    assert "live:" in reason, f"ожидали 'live:' в skipped_reason: {reason}"
    assert "cache:" in reason, f"ожидали 'cache:' в skipped_reason: {reason}"
    assert live_error_text in reason, f"ожидали текст live-ошибки в skipped_reason: {reason}"


# ---------------------------------------------------------------------------
# НОВЫЕ тесты: pending_approvals — одобрение через Telegram
# ---------------------------------------------------------------------------

def test_dry_run_with_candidates_saves_pending_approvals_and_sends_buttons(enabled_dry_run_cfg, tmp_path, monkeypatch):
    """dry_run с кандидатами сохраняет pending_approvals и зовёт send_with_buttons."""
    import services.autopilot as ap_module

    # Две независимые группы (разные города) — каждая с хорошим аналогом и плохим кандидатом.
    # Только так оба 11111 и 22222 получат ОТКЛЮЧИТЬ через портфельный слой:
    #   - score_gap = 1.0 (best=1.0, плохое=0.0) >= min_score_gap=0.15
    #   - rank плохого = 2 >= ceil(2/2)=1 → in_bottom=True
    #   - leads=5 >= min_leads=5, spend=50 >= portfolio_min_spend=15
    ads = [
        # Группа 1 (CityA): хорошее-аналог + 11111 плохое
        _make_ad("good_citya", "Хорошее CityA", spend=80.0, leads=15, cpl=10.0, ctr=3.0,
                 city="CityA", adset_type="L2", ad_objective="leadform"),
        _make_ad("11111", "Объявление CityA", spend=50.0, leads=5, cpl=90.0, ctr=0.1,
                 city="CityA", adset_type="L2", ad_objective="leadform"),
        # Группа 2 (CityC): хорошее-аналог + 22222 плохое
        _make_ad("good_cityc", "Хорошее CityC", spend=80.0, leads=15, cpl=10.0, ctr=3.0,
                 city="CityC", adset_type="L2", ad_objective="leadform"),
        _make_ad("22222", "Объявление CityC", spend=50.0, leads=5, cpl=90.0, ctr=0.1,
                 city="CityC", adset_type="L2", ad_objective="leadform"),
    ]

    saved_state = {}

    def capture_save(state):
        saved_state.update(state)

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={
             "last_run_at": None, "last_run_window": None,
             "manual_overrides": {}, "pending_approvals": {},
         }), \
         patch("services.autopilot._save_state", side_effect=capture_save), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "плохой CPL"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True) as mock_buttons:

        result = ap_module.run_autopilot(trigger="manual")

    assert result["ran"] is True
    assert result["mode"] == "dry_run"

    # pending_approvals должен быть сохранён
    pending = saved_state.get("pending_approvals", {})
    assert len(pending) == 1, f"ожидали 1 запись в pending_approvals, got: {pending}"

    run_key = list(pending.keys())[0]
    assert run_key.startswith("run-"), f"ключ должен начинаться с 'run-': {run_key}"
    entry = pending[run_key]
    assert "ads" in entry
    assert "created_at" in entry
    # Оба объявления в записи
    ad_ids_in_pending = {ad["id"] for ad in entry["ads"]}
    assert "11111" in ad_ids_in_pending
    assert "22222" in ad_ids_in_pending

    # send_with_buttons вызван с нужными кнопками
    mock_buttons.assert_called_once()
    _text, buttons = mock_buttons.call_args[0]
    # Первая кнопка — "Применить все (N)"
    first_row = buttons[0]
    assert len(first_row) == 1
    label0, cb0 = first_row[0]
    assert "Применить все" in label0
    assert cb0 == f"applyrun:{run_key}"
    # apply: кнопки для каждого объявления
    all_cbs = [btn[1] for row in buttons[1:] for btn in row]
    assert "apply:11111" in all_cbs
    assert "apply:22222" in all_cbs

    # send_telegram НЕ вызывался (т.к. send_with_buttons вернул True)
    mock_tg.assert_not_called()


def test_dry_run_buttons_fallback_to_send_telegram_on_import_error(enabled_dry_run_cfg):
    """Если send_with_buttons недоступен (ImportError) — fallback на send_telegram."""
    import services.autopilot as ap_module

    ads = [_make_ad("11111", "Объявление CityA", spend=200.0)]

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={
             "last_run_at": None, "last_run_window": None,
             "manual_overrides": {}, "pending_approvals": {},
         }), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", side_effect=ImportError("нет модуля")):

        result = ap_module.run_autopilot(trigger="manual")

    assert result["ran"] is True
    # Fallback: send_telegram вызван
    mock_tg.assert_called_once()


def test_pending_approval_ttl_25h_returns_none(tmp_path, monkeypatch):
    """Запись созданная 25 часов назад — get_pending_approval возвращает None (TTL истёк)."""
    import services.autopilot as ap_module

    state_path = tmp_path / "autopilot_state.json"
    monkeypatch.setattr(ap_module, "STATE_FILE", state_path)

    old_time = (datetime.now(timezone(timedelta(hours=5))) - timedelta(hours=25)).isoformat()
    state_path.write_text(json.dumps({
        "last_run_at": None,
        "last_run_window": None,
        "manual_overrides": {},
        "pending_approvals": {
            "run-202601010000": {
                "ads": [{"id": "11111", "name": "Старое", "spend": 50, "reason": "тест"}],
                "created_at": old_time,
            }
        },
    }))

    result = ap_module.get_pending_approval("run-202601010000")
    assert result is None, f"ожидали None для протухшей записи, got: {result}"


def test_pending_approval_fresh_returns_entry(tmp_path, monkeypatch):
    """Свежая запись (1 час назад) — get_pending_approval возвращает её."""
    import services.autopilot as ap_module

    state_path = tmp_path / "autopilot_state.json"
    monkeypatch.setattr(ap_module, "STATE_FILE", state_path)

    recent_time = (datetime.now(timezone(timedelta(hours=5))) - timedelta(hours=1)).isoformat()
    ads = [{"id": "11111", "name": "Объявление", "spend": 100, "reason": "высокий CPL"}]
    state_path.write_text(json.dumps({
        "last_run_at": None,
        "last_run_window": None,
        "manual_overrides": {},
        "pending_approvals": {
            "run-202110242102": {
                "ads": ads,
                "created_at": recent_time,
            }
        },
    }))

    result = ap_module.get_pending_approval("run-202110242102")
    assert result is not None
    assert result["ads"] == ads


def test_approve_pause_creates_proposal_without_mutation(tmp_path, monkeypatch, producer_boundary):
    """Legacy Telegram-кнопка «применить» больше не паузит, а создаёт proposal.

    Кнопка старого автопилота — не полномочие на мутацию: она лишь ставит
    задачу владельцу в approval-контур. Поэтому save_decision (бизнес-успех)
    здесь не пишется, а FB-мутатор не вызывается вовсе.
    """
    import services.autopilot as ap_module

    state_path = tmp_path / "autopilot_state.json"
    monkeypatch.setattr(ap_module, "STATE_FILE", state_path)
    state_path.write_text(json.dumps({
        "last_run_at": None, "last_run_window": None,
        "manual_overrides": {}, "pending_approvals": {},
    }))

    meta = {"name": "Объявление Тест", "reason": "плохой CPL", "spend": 150.0,
            "leads": 3, "cpl": 50.0, "ctr": 1.0, "romi": None, "qual_pct": None}

    with patch("agent.repositories.decisions_repo.save_decision") as mock_save:
        ok, msg = ap_module.approve_pause("11111", meta, command_id="click-1")

    assert ok is True
    # Возвращается id созданного предложения, он же кладётся в meta
    assert msg == meta["_proposal_id"]

    plan = producer_boundary.assert_proposed(
        "11111",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    assert plan.source_ref == "autopilot-owner:click-1:11111"
    producer_boundary.assert_no_direct_provider_mutation()
    # Бизнес-успех не фиксируется: владелец ещё ничего не одобрил
    mock_save.assert_not_called()


def test_approve_pause_returns_false_on_producer_error(tmp_path, monkeypatch, producer_boundary):
    """approve_pause возвращает (False, msg), если proposal создать не удалось."""
    import services.autopilot as ap_module

    state_path = tmp_path / "autopilot_state.json"
    monkeypatch.setattr(ap_module, "STATE_FILE", state_path)
    state_path.write_text(json.dumps({
        "last_run_at": None, "last_run_window": None,
        "manual_overrides": {}, "pending_approvals": {},
    }))

    meta = {"name": "Ошибочное объявление", "reason": "тест", "spend": 50.0}

    with patch(
        "services.action_producer_gateway.propose_pause",
        side_effect=RuntimeError("FB API timeout"),
    ), \
         patch("agent.repositories.decisions_repo.save_decision") as mock_save:

        ok, msg = ap_module.approve_pause("99999", meta)

    assert ok is False
    assert "FB API timeout" in msg
    assert "_proposal_id" not in meta
    # ни бизнес-записи, ни мутации при отказе producer
    mock_save.assert_not_called()
    producer_boundary.assert_no_direct_provider_mutation()


def test_approve_pause_returns_false_when_proposal_not_created(tmp_path, monkeypatch, producer_boundary):
    """Producer честно отказал (receipt=None) → (False, причина), без мутации."""
    import services.autopilot as ap_module

    state_path = tmp_path / "autopilot_state.json"
    monkeypatch.setattr(ap_module, "STATE_FILE", state_path)
    state_path.write_text(json.dumps({
        "last_run_at": None, "last_run_window": None,
        "manual_overrides": {}, "pending_approvals": {},
    }))

    with patch(
        "services.action_producer_gateway.propose_pause",
        return_value=blocked_outcome("LAST_EFFECTIVE_ACTIVE"),
    ), \
         patch("agent.repositories.decisions_repo.save_decision") as mock_save:

        ok, msg = ap_module.approve_pause("99999", {"name": "Последнее"})

    assert ok is False
    assert "LAST_EFFECTIVE_ACTIVE" in msg
    mock_save.assert_not_called()
    producer_boundary.assert_no_direct_provider_mutation()


# ---------------------------------------------------------------------------
# НОВЫЕ тесты: refresh_statuses_in_place — корректная работа с паузными статусами
# ---------------------------------------------------------------------------

def test_refresh_sets_paused_status_excludes_from_candidates(enabled_dry_run_cfg, producer_boundary):
    """(а) refresh ставит PAUSED — объявление исключается из кандидатов."""
    # Объявление изначально ACTIVE с рекомендацией ОТКЛЮЧИТЬ
    ad = _make_ad("ad_paused", recommendation="ОТКЛЮЧИТЬ", effective_status="ACTIVE")

    def fake_refresh(ads):
        # Имитируем: refresh узнаёт что объявление уже PAUSED
        for a in ads:
            if a["id"] == "ad_paused":
                a["effective_status"] = "PAUSED"
                a["status"] = "PAUSED"
                a["is_paused"] = True

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=[ad]), \
         patch("agent.analyzer.refresh_statuses_in_place", side_effect=fake_refresh), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    # Кандидатов нет — объявление после refresh уже PAUSED
    assert result["candidates"] == [], f"PAUSED объявление не должно попадать в кандидаты: {result['candidates']}"
    # pause_ad не вызывался (кандидатов нет)
    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["statuses_refreshed"] is True


def test_refresh_exception_dry_run_continues_statuses_refreshed_false(enabled_dry_run_cfg, producer_boundary):
    """(б) refresh кидает исключение → dry_run продолжается (statuses_refreshed=False)."""
    ad = _make_ad("ad1", recommendation="ОТКЛЮЧИТЬ", effective_status="ACTIVE")

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=[ad]), \
         patch("agent.analyzer.refresh_statuses_in_place", side_effect=Exception("FB rate limit")), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert") as mock_alert:

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    # dry_run не прерывается при сбое refresh
    assert result["ran"] is True
    assert result["statuses_refreshed"] is False
    # В dry_run паузы не выполняются в любом случае
    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    # critical alert НЕ шлётся в dry_run при сбое refresh
    mock_alert.assert_not_called()


def test_refresh_exception_active_blocks_pauses_with_alert(enabled_active_cfg, producer_boundary):
    """(б) refresh кидает исключение → active блокирует паузы и шлёт critical alert.

    Используем группу с реальным кандидатом (1 хорошее + 1 плохое), чтобы
    портфельный слой выдал to_act и RAIL statuses_not_refreshed_active_blocked сработал.
    Одиночное объявление защищается правилом «последнее в группе» и to_act будет пустым.
    """
    # Группа из 2: хорошее (CPL=10, CTR=3.0) + плохое ad1 (CPL=90, CTR=0.1, leads=5 >= min_leads)
    # score_gap = 1.0 >= 0.15 → портфель пометит ad1 как ОТКЛЮЧИТЬ
    ads = _make_portfolio_group(n_bad=1)

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_active_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("agent.analyzer.refresh_statuses_in_place", side_effect=Exception("FB rate limit")), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert") as mock_alert:

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    # В active-режиме при statuses_refreshed=False — паузы запрещены
    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []
    assert result["statuses_refreshed"] is False
    assert result["skipped_reason"] == "statuses_not_refreshed_active_blocked"
    # critical alert отправлен
    mock_alert.assert_called_once()
    alert_title = mock_alert.call_args.args[0]
    assert "статусы не обновлены" in alert_title.lower()


def test_refresh_called_for_cache_source(enabled_dry_run_cfg, tmp_path, monkeypatch):
    """(в) refresh вызван даже при source=='cache'."""
    import services.autopilot as ap_module
    import time as time_module

    ads = [_make_ad("ad1")]
    now_dt = datetime.now(_TZ)
    date_to = now_dt.strftime("%Y-%m-%d")
    date_from = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d")

    disk_cache_path = tmp_path / "analytics_cache.json"
    disk_cache_path.write_text(json.dumps({
        "data": ads,
        "date_from": date_from,
        "date_to": date_to,
        "saved_at": time_module.time(),
    }, ensure_ascii=False))
    monkeypatch.setattr(ap_module, "_DISK_CACHE_PATH", disk_cache_path)

    refresh_called_with = []

    def capture_refresh(ad_list):
        refresh_called_with.extend(ad_list)

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", side_effect=Exception("FB rate limit")), \
         patch("web.app.get_cached_analytics", return_value=ads), \
         patch("agent.analyzer.refresh_statuses_in_place", side_effect=capture_refresh) as mock_refresh, \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        result = ap_module.run_autopilot(trigger="manual")

    # refresh_statuses_in_place вызван именно при source=="cache"
    assert result.get("source") == "cache"
    mock_refresh.assert_called_once(), "refresh_statuses_in_place должен быть вызван для cache-источника"
    assert result["statuses_refreshed"] is True


def test_save_state_cleans_expired_pending_approvals(tmp_path, monkeypatch):
    """_save_state чистит pending_approvals старше 24 часов."""
    import services.autopilot as ap_module

    state_path = tmp_path / "autopilot_state.json"
    monkeypatch.setattr(ap_module, "STATE_FILE", state_path)

    old_time = (datetime.now(timezone(timedelta(hours=5))) - timedelta(hours=25)).isoformat()
    fresh_time = (datetime.now(timezone(timedelta(hours=5))) - timedelta(hours=1)).isoformat()

    state = {
        "last_run_at": None,
        "last_run_window": None,
        "manual_overrides": {},
        "pending_approvals": {
            "run-старый": {"ads": [], "created_at": old_time},
            "run-свежий": {"ads": [{"id": "11111"}], "created_at": fresh_time},
        },
    }

    ap_module._save_state(state)

    saved = json.loads(state_path.read_text())
    assert "run-старый" not in saved["pending_approvals"], "протухший ключ должен быть удалён"
    assert "run-свежий" in saved["pending_approvals"], "свежий ключ должен остаться"


# ===========================================================================
# Тесты run_autopilot_live (боевой автопилот)
# ===========================================================================

def _make_local_ad(ad_id: str = "ad1", ad_name: str = "Тест", spend: float = 100.0,
                   days_running: int = 10, cpl: float = 50.0, ctr: float = 0.5,
                   leads: int = 5, qual_pct=None, romi=None) -> dict:
    """Объявление в формате _fetch_ads_from_local_db (ключи ad_id/ad_name)."""
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "spend": spend,
        "days_running": days_running,
        "cpl": cpl,
        "ctr": ctr,
        "leads": leads,
        "qual_pct": qual_pct,
        "romi": romi,
        "impressions": 1000,
        "city": "CityA",
        "adset_type": "L2",
        "adset_id": None,
        "effective_status": "ACTIVE",
        "recommendation": "ЖДАТЬ",
        "reason": "",
    }


def _make_pause_decision(ad_id: str = "ad1", ad_name: str = "Тест",
                         score: int = 1, adset_id: str = "adset1",
                         reasons: list = None) -> dict:
    """Решение PAUSE от score_and_decide."""
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "adset_id": adset_id,
        "action": "PAUSE",
        "score": score,
        "reasons": reasons or ["плохой CPL"],
    }


def _make_keep_decision(ad_id: str = "ad_good", score: int = 7) -> dict:
    """Решение KEEP от score_and_decide."""
    return {
        "ad_id": ad_id,
        "ad_name": "Хорошее",
        "adset_id": "adset1",
        "action": "KEEP",
        "score": score,
        "reasons": ["KEEP: хороший ROMI"],
    }


# ---------------------------------------------------------------------------
# LV-1: kill_switch / enabled=false → 0 пауз
# ---------------------------------------------------------------------------

def test_live_disabled_returns_skipped():
    """enabled=false → skipped='disabled', pause_ad не вызван."""
    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": False, "kill_switch": False, "max_pauses_per_run": 8, "min_days_protect": 5,
    }):
        from services.autopilot import run_autopilot_live
        result = run_autopilot_live()

    assert result["ran"] is False
    assert result["skipped"] == "disabled"
    assert result["paused"] == []


def test_live_kill_switch_returns_skipped():
    """kill_switch=true → skipped='kill_switch', pause_ad не вызван."""
    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": True, "max_pauses_per_run": 8, "min_days_protect": 5,
    }):
        from services.autopilot import run_autopilot_live
        result = run_autopilot_live()

    assert result["ran"] is False
    assert result["skipped"] == "kill_switch"
    assert result["paused"] == []


# ---------------------------------------------------------------------------
# LV-2: cap соблюдён — паузится не больше max_pauses
# ---------------------------------------------------------------------------

def test_live_cap_respected(producer_boundary):
    """Если кандидатов больше cap — предложений создаётся не больше cap."""
    # 5 кандидатов, cap=3
    local_ads = [_make_local_ad(ad_id=f"ad{i}", days_running=10) for i in range(5)]
    decisions = [_make_pause_decision(ad_id=f"ad{i}", score=i, adset_id=f"adset{i}") for i in range(5)]
    # FB: все ACTIVE, у каждого уникальный adset (guardrail не сработает)
    fb_info = {
        f"ad{i}": {"adset_id": f"adset{i}", "effective_status": "ACTIVE"}
        for i in range(5)
    }

    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": False, "max_pauses_per_run": 3, "min_days_protect": 5,
    }), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("agent.analyzer.get_fresh_ad_statuses", return_value={}), \
         patch("agent.fb_common.build_adset_map", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):

        from services.autopilot import run_autopilot_live
        result = run_autopilot_live(max_pauses=3)

    # Cap ограничивает именно число предложений владельцу
    assert len(producer_boundary.plans) == 3
    assert len(result["proposals"]) == 3
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []


# ---------------------------------------------------------------------------
# LV-3: guardrail «не пустеть» — adset с 1 кандидатом НЕ паузится
# ---------------------------------------------------------------------------

def test_live_guardrail_single_candidate_in_adset_protected(producer_boundary):
    """Если в adset только 1 кандидат — guardrail защищает его (proposal не создаётся)."""
    # 1 кандидат в adset1
    local_ads = [_make_local_ad(ad_id="ad1", days_running=10)]
    decisions = [_make_pause_decision(ad_id="ad1", score=2, adset_id="adset1")]
    # FB: ad1 ACTIVE, его adset_id="adset1"
    fb_info = {"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}
    pause_inventories = _make_pause_inventory("adset1", {"ad1"}, ["ad1"])

    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": False, "max_pauses_per_run": 8, "min_days_protect": 5,
    }), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("services.adset_pause_guard.fetch_pause_inventory", return_value=pause_inventories), \
         patch("agent.analyzer.get_fresh_ad_statuses", return_value={}), \
         patch("agent.fb_common.build_adset_map", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):

        from services.autopilot import run_autopilot_live
        result = run_autopilot_live()

    # Guardrail защитил единственного кандидата в adset
    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []
    assert result["ran"] is True, result


def test_classic_active_blocks_last_active_with_critical_alert(enabled_active_cfg, producer_boundary):
    """Classic-контур не создаёт proposal, когда в adset остался только кандидат.

    Отдельно проверяем, что падение критического алерта (Telegram лежит) не
    превращает блокировку в тихий пропуск и не роняет прогон.
    """
    ads = _make_5_candidates()
    enabled_active_cfg["max_pauses_per_run"] = 1

    def lone_inventory(ad_ids):
        assert len(ad_ids) == 1
        return _make_pause_inventory("100", {ad_ids[0]}, ad_ids)

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_active_cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("services.adset_pause_guard.fetch_pause_inventory", side_effect=lone_inventory), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch(
             "services.notifications.send_critical_alert",
             side_effect=RuntimeError("telegram down"),
         ) as mock_alert:

        from services.autopilot import run_autopilot
        result = run_autopilot(trigger="manual")

    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    mock_alert.assert_called()
    assert result["paused"] == []
    assert result["proposals"] == []
    assert any("last_active_without_replacement" in error for error in result["errors"])


def test_live_guardrail_best_protected_others_proposed(producer_boundary):
    """В adset 3 кандидата — guardrail защищает лучшего, остальные предлагаются."""
    local_ads = [_make_local_ad(ad_id=f"ad{i}", days_running=10) for i in range(3)]
    # ad2 имеет наибольший score=5 — будет защищён guardrail
    decisions = [
        _make_pause_decision(ad_id="ad0", score=1, adset_id="adset1"),
        _make_pause_decision(ad_id="ad1", score=3, adset_id="adset1"),
        _make_pause_decision(ad_id="ad2", score=5, adset_id="adset1"),  # лучший
    ]
    fb_info = {
        "ad0": {"adset_id": "adset1", "effective_status": "ACTIVE"},
        "ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"},
        "ad2": {"adset_id": "adset1", "effective_status": "ACTIVE"},
    }
    pause_inventories = _make_pause_inventory(
        "adset1", {"ad0", "ad1", "ad2"}, ["ad0", "ad1", "ad2"]
    )

    # Свежий дневной state — pauses_today=0, дневной лимит не исчерпан
    import datetime as _dt
    fresh_daily_state = {"date": _dt.date.today().isoformat(), "pauses_today": 0}

    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": False, "max_pauses_per_run": 8, "min_days_protect": 5,
    }), \
         patch("services.autopilot._load_live_daily_state", return_value=fresh_daily_state), \
         patch("services.autopilot._save_live_daily_state"), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("services.adset_pause_guard.fetch_pause_inventory", return_value=pause_inventories), \
         patch("agent.analyzer.get_fresh_ad_statuses", return_value={}), \
         patch("agent.fb_common.build_adset_map", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):

        from services.autopilot import run_autopilot_live
        result = run_autopilot_live()

    # Guard разрешает максимум N-1 из N ACTIVE — один остаётся защищённым
    proposed = producer_boundary.subject_ids
    assert len(proposed) == 2, proposed
    assert set(proposed) < {"ad0", "ad1", "ad2"}
    assert set(producer_boundary.kinds) == {ProposalKind.PAUSE}
    assert len(result["proposals"]) == 2
    # Реальных пауз нет — только предложения владельцу
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []


# ---------------------------------------------------------------------------
# LV-4: кандидат уже PAUSED в FB → пропускается
# ---------------------------------------------------------------------------

def test_live_already_paused_in_fb_skipped(producer_boundary):
    """Кандидат уже PAUSED в FB — proposal на него не создаётся."""
    local_ads = [
        _make_local_ad(ad_id="ad_paused", days_running=10),
        _make_local_ad(ad_id="ad_active", days_running=10),
    ]
    decisions = [
        _make_pause_decision(ad_id="ad_paused", score=1, adset_id="adset1"),
        _make_pause_decision(ad_id="ad_active", score=2, adset_id="adset2"),
    ]
    # ad_paused уже не ACTIVE в FB
    fb_info = {
        "ad_paused": {"adset_id": "adset1", "effective_status": "PAUSED"},
        "ad_active": {"adset_id": "adset2", "effective_status": "ACTIVE"},
    }

    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": False, "max_pauses_per_run": 8, "min_days_protect": 5,
    }), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("agent.analyzer.get_fresh_ad_statuses", return_value={}), \
         patch("agent.fb_common.build_adset_map", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):

        from services.autopilot import run_autopilot_live
        result = run_autopilot_live()

    # ad_paused был PAUSED в FB — предложения на него нет
    assert "ad_paused" not in producer_boundary.subject_ids
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []


# ---------------------------------------------------------------------------
# LV-5: «молодые» объявления (<min_days_protect) не паузятся
# ---------------------------------------------------------------------------

def test_live_young_ads_protected(producer_boundary):
    """Объявления моложе min_days_protect=5 дней не попадают в кандидаты."""
    local_ads = [
        _make_local_ad(ad_id="ad_young", days_running=3),   # моложе 5 дней
        _make_local_ad(ad_id="ad_old", days_running=10),
    ]
    decisions = [
        _make_pause_decision(ad_id="ad_young", score=1, adset_id="adset1"),
        _make_pause_decision(ad_id="ad_old", score=2, adset_id="adset2"),
    ]
    fb_info = {
        "ad_old": {"adset_id": "adset2", "effective_status": "ACTIVE"},
    }

    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": False, "max_pauses_per_run": 8, "min_days_protect": 5,
    }), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("agent.analyzer.get_fresh_ad_statuses", return_value={}), \
         patch("agent.fb_common.build_adset_map", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):

        from services.autopilot import run_autopilot_live
        result = run_autopilot_live()

    # ad_young (3 дня) не должен попадать даже в предложения
    assert "ad_young" not in producer_boundary.subject_ids
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []


# ---------------------------------------------------------------------------
# LV-6: pause_ad вызван только для финального списка (не для всех)
# ---------------------------------------------------------------------------

def test_live_proposals_only_for_post_guard_final_list(producer_boundary):
    """Producer дёргается только для того, что прошло safety guard.

    ad_blocked живёт в adset с неполной пагинацией — guard его снимает, и до
    producer он не доходит вообще. ad_pair с доказанной ACTIVE-заменой попадает
    в предложение владельцу.
    """
    local_ads = [
        _make_local_ad(ad_id="ad_blocked", days_running=10),
        _make_local_ad(ad_id="ad_pair", days_running=10),
    ]
    decisions = [
        _make_pause_decision(ad_id="ad_blocked", score=1, adset_id="adset_blocked"),
        _make_pause_decision(ad_id="ad_pair", score=2, adset_id="adset_pair"),
    ]
    fb_info = {
        "ad_blocked": {"adset_id": "adset_blocked", "effective_status": "ACTIVE"},
        "ad_pair": {"adset_id": "adset_pair", "effective_status": "ACTIVE"},
    }
    blocked_inventory = _make_pause_inventory(
        "adset_blocked", {"ad_blocked", "sibling"}, ["ad_blocked"]
    )
    blocked_inventory["adset_blocked"]["complete"] = False
    blocked_inventory["adset_blocked"]["error"] = "paging_cursor_missing"
    inventories = {
        **blocked_inventory,
        **_make_pause_inventory(
            "adset_pair", {"ad_pair", "replacement"}, ["ad_pair"]
        ),
    }

    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": False, "max_pauses_per_run": 8, "min_days_protect": 5,
    }), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("services.adset_pause_guard.fetch_pause_inventory", return_value=inventories), \
         patch("agent.analyzer.get_fresh_ad_statuses", return_value={}), \
         patch("agent.fb_common.build_adset_map", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):

        from services.autopilot import run_autopilot_live
        result = run_autopilot_live()

    assert producer_boundary.subject_ids == ["ad_pair"]
    assert any(
        "ad_blocked" in error and "paging_cursor_missing" in error
        for error in result["errors"]
    ), result["errors"]
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []


# ---------------------------------------------------------------------------
# LV-7: только PAUSE — никаких delete/archive, ни прямо, ни в предложении
# ---------------------------------------------------------------------------

def test_live_proposal_is_pause_only_never_delete(producer_boundary):
    """Автопилот предлагает исключительно PAUSE_AD: ACTIVE → PAUSED.

    Ни delete, ни archive: в intended_payload зафиксирован ожидаемый статус
    PAUSED, а сами FB-мутаторы (в т.ч. create_ad) остаются нетронутыми.
    """
    local_ads = [_make_local_ad(ad_id="ad1", days_running=10)]
    decisions = [_make_pause_decision(ad_id="ad1", score=1, adset_id="adset_x")]
    fb_info = {"ad1": {"adset_id": "adset_x", "effective_status": "ACTIVE"}}

    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": False, "max_pauses_per_run": 8, "min_days_protect": 5,
    }), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("agent.analyzer.get_fresh_ad_statuses", return_value={}), \
         patch("agent.fb_common.build_adset_map", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):

        from services.autopilot import run_autopilot_live
        run_autopilot_live()

    plan = producer_boundary.assert_proposed(
        "ad1",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    payload = plan.targets[0].intended_payload
    assert payload["operation"] == "PAUSE_AD"
    assert payload["expected_before_status"] == "ACTIVE"
    assert payload["expected_after_status"] == "PAUSED"
    assert "delete" not in json.dumps(dict(payload)).lower()
    assert "archive" not in json.dumps(dict(payload)).lower()
    producer_boundary.assert_no_direct_provider_mutation()


# ---------------------------------------------------------------------------
# LV-8: Telegram отчёт с кнопками по каждому предложенному объявлению
# ---------------------------------------------------------------------------

def test_live_telegram_report_without_undo_buttons(producer_boundary):
    """Отчёт о предложениях уходит БЕЗ кнопок «Вернуть»: ничего не выключено,
    возвращать нечего. Кнопки решений живут на карточках предложений."""
    local_ads = [
        _make_local_ad(ad_id="ad_a", ad_name="Объявление А", days_running=10),
        _make_local_ad(ad_id="ad_b", ad_name="Объявление Б", days_running=10),
    ]
    # Каждый в своём adset → guardrail защищает обоих (единственные в adset)
    # Используем 2 объявления в ОДНОМ adset с третьим хорошим кандидатом,
    # чтобы guardrail не защитил их
    decisions = [
        _make_pause_decision(ad_id="ad_a", score=1, adset_id="adset_shared"),
        _make_pause_decision(ad_id="ad_b", score=2, adset_id="adset_shared"),
        # третий — лучший, guardrail защитит его, ad_a и ad_b паузятся
        {
            "ad_id": "ad_best",
            "ad_name": "Лучший",
            "adset_id": "adset_shared",
            "action": "KEEP",  # не PAUSE — в финальный список не попадёт
            "score": 9,
            "reasons": [],
        },
    ]
    # Два кандидата в одном adset — guardrail оставит лучшего (ad_b score=2)
    fb_info = {
        "ad_a": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
        "ad_b": {"adset_id": "adset_shared", "effective_status": "ACTIVE"},
    }

    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": False, "max_pauses_per_run": 8, "min_days_protect": 5,
    }), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("agent.analyzer.get_fresh_ad_statuses", return_value={}), \
         patch("agent.fb_common.build_adset_map", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True) as mock_buttons:

        from services.autopilot import run_autopilot_live
        result = run_autopilot_live()

    proposed = producer_boundary.subject_ids
    assert proposed, "ожидали хотя бы одно предложение владельцу"
    assert len(result["proposals"]) == len(proposed)
    # Кнопок «Вернуть» на отчёте о ПРЕДЛОЖЕНИЯХ больше нет (решение владельца:
    # возвращать нечего — ничего не выключено). Решения — на карточках
    # предложений; отчёт уходит обычным send_telegram без callback-кнопок.
    mock_buttons.assert_not_called()
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []


# ---------------------------------------------------------------------------
# LV-9: manual_override исключает объявление из кандидатов live
# ---------------------------------------------------------------------------

def test_live_manual_override_excludes_ad(producer_boundary):
    """Объявление с активным manual_override не попадает в кандидаты."""
    from datetime import datetime, timedelta, timezone
    _TZ = timezone(timedelta(hours=5))
    until_future = (datetime.now(_TZ) + timedelta(days=3)).isoformat()

    local_ads = [
        _make_local_ad(ad_id="ad_override", days_running=10),
        _make_local_ad(ad_id="ad_normal", days_running=10),
    ]
    decisions = [
        _make_pause_decision(ad_id="ad_override", score=1, adset_id="adset1"),
        _make_pause_decision(ad_id="ad_normal", score=2, adset_id="adset2"),
    ]
    fb_info = {
        "ad_override": {"adset_id": "adset1", "effective_status": "ACTIVE"},
        "ad_normal": {"adset_id": "adset2", "effective_status": "ACTIVE"},
    }

    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": False, "max_pauses_per_run": 8, "min_days_protect": 5,
    }), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("agent.analyzer.get_fresh_ad_statuses", return_value={}), \
         patch("agent.fb_common.build_adset_map", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={"ad_override": until_future}), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):

        from services.autopilot import run_autopilot_live
        result = run_autopilot_live()

    # ad_override исключён override — proposal на него не создавался
    assert "ad_override" not in producer_boundary.subject_ids
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []


# ---------------------------------------------------------------------------
# LV-10 (фикс 1 safety): кандидат без adset_id в FB-ответе — НЕ паузится
# ---------------------------------------------------------------------------

def test_live_missing_adset_id_in_fb_response_is_protected(producer_boundary):
    """Кандидат, по которому FB не вернул adset_id (пустая строка) → protected, не предлагается.

    Fail-safe принцип: не можем проверить adset → не трогаем.
    """
    local_ads = [
        _make_local_ad(ad_id="ad_no_adset", days_running=10),
        _make_local_ad(ad_id="ad_with_adset", days_running=10),
    ]
    decisions = [
        _make_pause_decision(ad_id="ad_no_adset", score=1, adset_id=""),
        _make_pause_decision(ad_id="ad_with_adset", score=2, adset_id="adset_known"),
    ]
    # FB возвращает ad_no_adset без adset_id (пустая строка — отсутствует поле)
    fb_info = {
        "ad_no_adset": {"adset_id": "", "effective_status": "ACTIVE"},
        "ad_with_adset": {"adset_id": "adset_known", "effective_status": "ACTIVE"},
    }
    pause_inventories = _make_pause_inventory(
        "adset_known", {"ad_with_adset", "replacement"}, ["ad_with_adset"]
    )

    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": False, "max_pauses_per_run": 8, "min_days_protect": 5,
    }), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("services.adset_pause_guard.fetch_pause_inventory", return_value=pause_inventories), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):

        from services.autopilot import run_autopilot_live
        result = run_autopilot_live()

    # ad_no_adset защищён fail-safe — proposal на него не создавался
    assert "ad_no_adset" not in producer_boundary.subject_ids, (
        f"ad_no_adset не должен предлагаться (нет adset_id): {producer_boundary.subject_ids}"
    )
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []
    # ad_with_adset тоже защищён guardrail (единственный в своём adset)
    assert result["ran"] is True


# ===========================================================================
# Тесты _should_run_autopilot_live и _cron_autopilot_live (web/app.py)
# ===========================================================================

class TestShouldRunAutopilotLive:
    """Тесты функции _should_run_autopilot_live — гейт крона (8×/день, Страж).

    ARCH-phase1-guardian (G8): гейт проверяет часы {8,10,12,14,16,18,20,22}
    (было {9,13,17,21} — 4×/день) и использует структуру state с ключом
    "slots" (не "last_run_date").
    """

    def test_hour_9_no_previous_run_returns_true(self, tmp_path, monkeypatch):
        """час == 8 и ещё не запускался в этом слоте сегодня → True."""
        import web.app as app_module
        state_path = tmp_path / "autopilot_live_state.json"
        monkeypatch.setattr(app_module, "_AUTOPILOT_LIVE_STATE", state_path)
        # state-файл отсутствует → нет слотов
        _TZ = timezone(timedelta(hours=5))
        now = datetime(2026, 6, 25, 8, 15, tzinfo=_TZ)
        assert app_module._should_run_autopilot_live(now) is True

    def test_hour_12_already_ran_today_returns_false(self, tmp_path, monkeypatch):
        """час == 8, но этот слот уже запущен сегодня → False (дедупликация)."""
        import web.app as app_module
        state_path = tmp_path / "autopilot_live_state.json"
        monkeypatch.setattr(app_module, "_AUTOPILOT_LIVE_STATE", state_path)
        # Записываем слот 2026-06-25-8 как выполненный
        state_path.write_text(
            json.dumps({"slots": {"2026-06-25-8": {"ran": True}}}),
            encoding="utf-8",
        )
        _TZ = timezone(timedelta(hours=5))
        now = datetime(2026, 6, 25, 8, 45, tzinfo=_TZ)
        assert app_module._should_run_autopilot_live(now) is False

    def test_hour_not_12_returns_false(self, tmp_path, monkeypatch):
        """Часы не из {8,10,12,14,16,18,20,22} → False независимо от даты."""
        import web.app as app_module
        state_path = tmp_path / "autopilot_live_state.json"
        monkeypatch.setattr(app_module, "_AUTOPILOT_LIVE_STATE", state_path)
        _TZ = timezone(timedelta(hours=5))
        # Проверяем несколько невалидных часов
        for hour in (0, 4, 7, 9, 11, 13, 15, 19, 21, 23):
            now = datetime(2026, 6, 25, hour, 0, tzinfo=_TZ)
            assert app_module._should_run_autopilot_live(now) is False, (
                f"час {hour} не должен проходить гейт"
            )

    def test_new_day_after_yesterday_run_returns_true(self, tmp_path, monkeypatch):
        """Вчера запускались в слоте 8, сегодня 8:xx → True (новый слот)."""
        import web.app as app_module
        state_path = tmp_path / "autopilot_live_state.json"
        monkeypatch.setattr(app_module, "_AUTOPILOT_LIVE_STATE", state_path)
        # Слот вчерашнего дня — не блокирует сегодня
        state_path.write_text(
            json.dumps({"slots": {"2026-06-24-8": {"ran": True}}}),
            encoding="utf-8",
        )
        _TZ = timezone(timedelta(hours=5))
        now = datetime(2026, 6, 25, 8, 0, tzinfo=_TZ)
        assert app_module._should_run_autopilot_live(now) is True


class TestCronAutopilotLive:
    """Тесты _cron_autopilot_live — ежедневный крон боевого автопилота."""

    def test_cron_runs_at_hour_12_first_time(self, tmp_path, monkeypatch):
        """Первый тик в 8:xx → вызывает run_autopilot_live с max_pauses из конфига."""
        import web.app as app_module
        state_path = tmp_path / "autopilot_live_state.json"
        monkeypatch.setattr(app_module, "_AUTOPILOT_LIVE_STATE", state_path)

        _TZ = timezone(timedelta(hours=5))
        now_mock = datetime(2026, 6, 25, 8, 5, tzinfo=_TZ)

        live_result = {
            "ran": True, "skipped": None,
            "analyzed": 10, "paused": ["ad1"], "errors": [],
        }

        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.run_autopilot_live", return_value=live_result) as mock_live, \
             patch("services.autopilot.get_autopilot_config",
                   return_value={"enabled": True, "kill_switch": False, "max_pauses_per_run": 5}):
            mock_dt.now.return_value = now_mock
            app_module._cron_autopilot_live()

        mock_live.assert_called_once()
        call_kwargs = mock_live.call_args
        assert call_kwargs.kwargs.get("trigger") == "cron" or \
               (len(call_kwargs.args) >= 2 and call_kwargs.args[1] == "cron")

    def test_cron_skips_at_wrong_hour(self, tmp_path, monkeypatch):
        """Тик вне _AUTOPILOT_LIVE_HOURS ({8,10,...,22}) → run_autopilot_live не вызывается."""
        import web.app as app_module
        state_path = tmp_path / "autopilot_live_state.json"
        monkeypatch.setattr(app_module, "_AUTOPILOT_LIVE_STATE", state_path)

        _TZ = timezone(timedelta(hours=5))

        for hour in (9, 15, 21):
            now_mock = datetime(2026, 6, 25, hour, 0, tzinfo=_TZ)
            with patch("web.app.datetime") as mock_dt, \
                 patch("services.autopilot.run_autopilot_live") as mock_live:
                mock_dt.now.return_value = now_mock
                app_module._cron_autopilot_live()
            mock_live.assert_not_called(), f"час {hour}: run_autopilot_live не должен вызываться"

    def test_cron_does_not_run_twice_same_day(self, tmp_path, monkeypatch):
        """Повторный тик в том же слоте (8:xx) → run_autopilot_live не вызывается второй раз."""
        import web.app as app_module
        state_path = tmp_path / "autopilot_live_state.json"
        monkeypatch.setattr(app_module, "_AUTOPILOT_LIVE_STATE", state_path)

        # Записываем что уже запустились в слоте 2026-06-25-8
        state_path.write_text(
            json.dumps({"slots": {"2026-06-25-8": {"ran": True}}}),
            encoding="utf-8",
        )

        _TZ = timezone(timedelta(hours=5))
        now_mock = datetime(2026, 6, 25, 8, 30, tzinfo=_TZ)

        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.run_autopilot_live") as mock_live:
            mock_dt.now.return_value = now_mock
            app_module._cron_autopilot_live()

        mock_live.assert_not_called()

    def test_cron_marks_date_before_calling_run(self, tmp_path, monkeypatch):
        """Слот помечается ДО вызова run_autopilot_live — защита от дублей."""
        import web.app as app_module
        state_path = tmp_path / "autopilot_live_state.json"
        monkeypatch.setattr(app_module, "_AUTOPILOT_LIVE_STATE", state_path)

        _TZ = timezone(timedelta(hours=5))
        now_mock = datetime(2026, 6, 25, 8, 0, tzinfo=_TZ)

        slots_at_call = []

        def capture_and_return(**kwargs):
            # В момент вызова state-файл уже должен содержать слот 2026-06-25-8
            if state_path.exists():
                s = json.loads(state_path.read_text())
                slots_at_call.append(list(s.get("slots", {}).keys()))
            return {"ran": True, "skipped": None, "analyzed": 5, "paused": [], "errors": []}

        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.run_autopilot_live", side_effect=capture_and_return), \
             patch("services.autopilot.get_autopilot_config",
                   return_value={"enabled": True, "kill_switch": False, "max_pauses_per_run": 5}):
            mock_dt.now.return_value = now_mock
            app_module._cron_autopilot_live()

        # Слот должен был быть записан ДО вызова run_autopilot_live
        assert len(slots_at_call) == 1, f"capture вызвался {len(slots_at_call)} раз"
        assert "2026-06-25-8" in slots_at_call[0], (
            f"ожидали слот '2026-06-25-8' в state при вызове run, получили: {slots_at_call[0]}"
        )

    def test_cron_exception_does_not_propagate(self, tmp_path, monkeypatch):
        """Если run_autopilot_live кидает исключение — крон его ловит и не падает."""
        import web.app as app_module
        state_path = tmp_path / "autopilot_live_state.json"
        monkeypatch.setattr(app_module, "_AUTOPILOT_LIVE_STATE", state_path)

        _TZ = timezone(timedelta(hours=5))
        now_mock = datetime(2026, 6, 25, 8, 0, tzinfo=_TZ)  # 8:xx — допустимый час

        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.run_autopilot_live", side_effect=RuntimeError("критическая ошибка")), \
             patch("services.autopilot.get_autopilot_config",
                   return_value={"enabled": True, "kill_switch": False, "max_pauses_per_run": 5}):
            mock_dt.now.return_value = now_mock
            # Не должен выбрасывать исключение — try/except в кроне
            app_module._cron_autopilot_live()  # должно завершиться без ошибки

    def test_cron_enabled_false_run_autopilot_live_still_called(self, tmp_path, monkeypatch):
        """Крон не проверяет enabled сам — передаёт вызов run_autopilot_live,
        которая сама вернёт skipped=disabled. Это проверяет что крон не дублирует логику."""
        import web.app as app_module
        state_path = tmp_path / "autopilot_live_state.json"
        monkeypatch.setattr(app_module, "_AUTOPILOT_LIVE_STATE", state_path)

        _TZ = timezone(timedelta(hours=5))
        now_mock = datetime(2026, 6, 25, 8, 0, tzinfo=_TZ)  # 8:xx — допустимый час

        skipped_result = {
            "ran": False, "skipped": "disabled",
            "analyzed": 0, "paused": [], "errors": [],
        }

        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.run_autopilot_live", return_value=skipped_result) as mock_live, \
             patch("services.autopilot.get_autopilot_config",
                   return_value={"enabled": False, "kill_switch": False, "max_pauses_per_run": 5}):
            mock_dt.now.return_value = now_mock
            app_module._cron_autopilot_live()

        # run_autopilot_live вызван — она сама обработает enabled=False
        mock_live.assert_called_once()


# ---------------------------------------------------------------------------
# LV-WASTER: тесты confirmed_waster в run_autopilot_live
# ---------------------------------------------------------------------------

def _make_waster_decision(
    ad_id: str = "waster_1",
    ad_name: str = "Слив объявление",
    spend: float = 250.0,
    adset_id: str = "adset_w",
) -> dict:
    """Решение PAUSE с флагом is_confirmed_waster=True."""
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "adset_id": adset_id,
        "action": "PAUSE",
        "score": 0,
        "reasons": [f"PAUSE: подтверждённый слив — расход ${spend:.0f}, 0 оплат, квал 5%"],
        "is_confirmed_waster": True,
    }


def _make_bonus_decision(
    ad_id: str = "bonus_1",
    ad_name: str = "Бонус объявление",
    adset_id: str = "adset_g",
) -> dict:
    """Решение PAUSE без is_confirmed_waster (тема «бонус»)."""
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "adset_id": adset_id,
        "action": "PAUSE",
        "score": 0,
        "reasons": ["вето: тема «бонус» (тема-вето политики)", "PAUSE: ..."],
        "is_confirmed_waster": False,
    }


def test_live_waster_sorted_before_bonus_with_cap(producer_boundary):
    """При лимите cap=1 предложение уходит на confirmed_waster, а не на бонус-тему.

    Сортировка: wasters (is_confirmed_waster=True) → первыми,
    затем остальные. При cap=1 бонус-тема не добирается до очереди.
    """
    local_ads = [
        _make_local_ad(ad_id="bonus_1", ad_name="Бонус", spend=300.0, days_running=10),
        _make_local_ad(ad_id="waster_1", ad_name="Слив", spend=250.0, days_running=10),
    ]
    # bonus идёт первым в списке decisions (до сортировки), waster — вторым
    decisions = [
        _make_bonus_decision(ad_id="bonus_1", adset_id="adset_g"),
        _make_waster_decision(ad_id="waster_1", spend=250.0, adset_id="adset_w"),
    ]
    fb_info = {
        "bonus_1": {"adset_id": "adset_g", "effective_status": "ACTIVE"},
        "waster_1": {"adset_id": "adset_w", "effective_status": "ACTIVE"},
    }

    import datetime as _dt
    fresh_daily_state = {"date": _dt.date.today().isoformat(), "pauses_today": 0}

    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": False, "max_pauses_per_run": 1,
        "max_pauses_per_day": 6, "min_days_protect": 5,
    }), \
         patch("services.autopilot._load_live_daily_state", return_value=fresh_daily_state), \
         patch("services.autopilot._save_live_daily_state"), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):

        from services.autopilot import run_autopilot_live
        result = run_autopilot_live(max_pauses=1)

    # При cap=1 предложение создаётся на waster (подтверждённый слив), а не на bonus
    assert producer_boundary.subject_ids == ["waster_1"], producer_boundary.subject_ids
    assert len(result["proposals"]) == 1
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []


def test_live_guardrail_protects_lone_waster(producer_boundary):
    """Последний confirmed_waster блокируется до появления ACTIVE-замены.

    Даже подтверждённый слив не повод оставить adset пустым: proposal не
    создаётся, ошибка попадает в отчёт прогона, и владелец обязан узнать о
    блокировке — либо критическим алертом, либо текстом Telegram-отчёта.
    Молчаливое «аутсайдеров нет» при заблокированной паузе недопустимо:
    владелец решит, что автопилот всё проверил и всё в порядке.
    """
    local_ads = [_make_local_ad(ad_id="lone_waster", days_running=10, spend=250.0)]
    decisions = [_make_waster_decision(ad_id="lone_waster", adset_id="adset_lone")]
    fb_info = {"lone_waster": {"adset_id": "adset_lone", "effective_status": "ACTIVE"}}
    pause_inventories = {
        "adset_lone": {
            "adset_id": "adset_lone",
            "active_ids": {"lone_waster"},
            "candidate_context": {
                "lone_waster": {
                    "ad_id": "lone_waster",
                    "adset_id": "adset_lone",
                    "configured_status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
            },
            "complete": True,
            "pages_read": 1,
            "error": None,
        }
    }

    import datetime as _dt
    fresh_daily_state = {"date": _dt.date.today().isoformat(), "pauses_today": 0}

    with patch("services.autopilot.get_autopilot_config", return_value={
        "enabled": True, "kill_switch": False, "max_pauses_per_run": 8,
        "max_pauses_per_day": 6, "min_days_protect": 5,
    }), \
         patch("services.autopilot._load_live_daily_state", return_value=fresh_daily_state), \
         patch("services.autopilot._save_live_daily_state"), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("services.adset_pause_guard.fetch_pause_inventory", return_value=pause_inventories), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram") as mock_telegram, \
         patch("services.notifications.send_critical_alert") as mock_alert, \
         patch("services.telegram_bot.send_with_buttons", return_value=True):

        from services.autopilot import run_autopilot_live
        result = run_autopilot_live()

    assert result["paused"] == []
    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["ran"] is True
    assert result["errors"], "блокировка обязана попасть в errors прогона"

    # Владелец должен узнать о блокировке хотя бы одним каналом.
    telegram_texts = [c.args[0] for c in mock_telegram.call_args_list if c.args]
    reassuring = [text for text in telegram_texts if "аутсайдеров нет" in text]
    assert not reassuring or mock_alert.called, (
        "PAUSE заблокирована, но владельцу отправлено успокаивающее "
        f"«аутсайдеров нет» без критического алерта: {telegram_texts}"
    )


# ---------------------------------------------------------------------------
# Lifetime evidence и Live hard-stop для 3+ дней без лидов
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "invalid_value",
    [False, -1, -0.5, 0.9, float("nan"), float("inf"), "0.9", "bad", None],
)
def test_graph_lead_row_strict_values(invalid_value):
    from services.autopilot import _parse_fb_lifetime_leads_row

    row = {"actions": [{"action_type": "lead", "value": invalid_value}]}

    assert _parse_fb_lifetime_leads_row(row) is None


def test_graph_lead_row_canonical_total_and_empty_zero():
    from services.autopilot import _parse_fb_lifetime_leads_row

    assert _parse_fb_lifetime_leads_row({}) == 0
    assert _parse_fb_lifetime_leads_row({"actions": None}) == 0
    assert _parse_fb_lifetime_leads_row({"actions": []}) == 0
    assert _parse_fb_lifetime_leads_row({
        "actions": [
            {"action_type": "lead", "value": 2},
            {"action_type": "onsite_conversion.lead_grouped", "value": "2"},
            {"action_type": "link_click", "value": "99"},
        ]
    }) == 2


def test_graph_lead_row_component_mismatch_is_fail_closed():
    from services.autopilot import _parse_fb_lifetime_leads_row

    row = {
        "actions": [
            {"action_type": "lead", "value": 0},
            {"action_type": "onsite_conversion.lead_grouped", "value": 1},
        ]
    }

    assert _parse_fb_lifetime_leads_row(row) is None


def test_graph_lead_row_duplicate_type_is_ambiguous():
    from services.autopilot import _parse_fb_lifetime_leads_row

    row = {
        "actions": [
            {"action_type": "lead", "value": 0},
            {"action_type": "lead", "value": 0},
        ]
    }

    assert _parse_fb_lifetime_leads_row(row) is None


def _fb_response(payload, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


def test_live_zero_spend_empty_complete_batch_is_verified_zero():
    from services.autopilot import _fetch_live_lifetime_lead_evidence

    response = _fb_response({"data": []})
    with patch("agent.fb_common._throttled_get", return_value=response) as get_mock, \
         patch("services.fb_token_provider.get_fb_token", return_value="test-token"), \
         patch("services.fb_token_provider.get_fb_account_id", return_value="123"):
        evidence = _fetch_live_lifetime_lead_evidence(["ad_zero"])

    assert evidence == {"ad_zero": 0}
    params = get_mock.call_args.kwargs["params"]
    assert params["level"] == "ad"
    assert params["date_preset"] == "maximum"
    assert params["fields"] == "ad_id,actions"
    assert json.loads(params["filtering"]) == [
        {"field": "ad.id", "operator": "IN", "value": ["ad_zero"]}
    ]


def test_graph_batch_is_atomic_on_one_invalid_row():
    from services.autopilot import _fetch_live_lifetime_lead_evidence

    response = _fb_response({
        "data": [
            {"ad_id": "ad_valid", "actions": [{"action_type": "lead", "value": 0}]},
            {"ad_id": "ad_invalid", "actions": [{"action_type": "lead", "value": 0.9}]},
        ]
    })
    with patch("agent.fb_common._throttled_get", return_value=response), \
         patch("services.fb_token_provider.get_fb_token", return_value="test-token"), \
         patch("services.fb_token_provider.get_fb_account_id", return_value="123"):
        evidence = _fetch_live_lifetime_lead_evidence(
            ["ad_valid", "ad_invalid", "ad_missing"]
        )

    assert evidence == {
        "ad_valid": None,
        "ad_invalid": None,
        "ad_missing": None,
    }


@pytest.mark.parametrize(
    "response",
    [
        _fb_response({}, status_code=500),
        _fb_response([]),
        _fb_response({"data": "bad"}),
        _fb_response({"data": [], "paging": {"next": "https://next"}}),
        _fb_response({"data": [{"ad_id": "other", "actions": []}]}),
    ],
)
def test_live_zero_candidate_requires_complete_lifetime_evidence(response):
    from services.autopilot import _fetch_live_lifetime_lead_evidence

    with patch("agent.fb_common._throttled_get", return_value=response), \
         patch("services.fb_token_provider.get_fb_token", return_value="test-token"), \
         patch("services.fb_token_provider.get_fb_account_id", return_value="123"):
        evidence = _fetch_live_lifetime_lead_evidence(["ad_zero"])

    assert evidence == {"ad_zero": None}


def test_refresh_live_zero_lead_evidence_mutates_only_eligible_ads():
    from services.autopilot import _refresh_live_zero_lead_evidence

    ads = [
        _make_local_ad(ad_id="verified", days_running=3, leads=0),
        _make_local_ad(ad_id="nonzero", days_running=4, leads=0),
        _make_local_ad(ad_id="unknown", days_running=5, leads=0),
        _make_local_ad(ad_id="young", days_running=2, leads=0),
        _make_local_ad(ad_id="already_has_lead", days_running=5, leads=1),
    ]

    with patch(
        "services.autopilot._fetch_live_lifetime_lead_evidence",
        return_value={"verified": 0, "nonzero": 2, "unknown": None},
    ) as evidence_mock:
        summary = _refresh_live_zero_lead_evidence(ads)

    evidence_mock.assert_called_once_with(["verified", "nonzero", "unknown"])
    assert [ad["leads"] for ad in ads] == [0, 2, None, 0, 1]
    assert summary == {
        "requested": ["verified", "nonzero", "unknown"],
        "verified_zero": ["verified"],
        "nonzero": ["nonzero"],
        "unknown": ["unknown"],
    }


def _make_zero_live_decision(ad_id="zero_3d", adset_id="zero_adset"):
    decision = _make_pause_decision(ad_id=ad_id, adset_id=adset_id)
    decision.update(
        is_zero_leads_after_3d=True,
        is_confirmed_waster=False,
        business_reason="за 3 полных дн. с запуска не получено ни одного лида",
    )
    return decision


def _zero_live_config(**overrides):
    config = {
        "enabled": True,
        "kill_switch": False,
        "max_pauses_per_run": 8,
        "max_pauses_per_day": 6,
        "min_days_protect": 5,
        "hold_enabled": False,
        "guardian": {},
    }
    config.update(overrides)
    return config


def test_live_zero_leads_after_3d_bypasses_min_days_protect(producer_boundary):
    local_ads = [_make_local_ad(ad_id="zero_3d", days_running=3, leads=0)]
    decision = _make_zero_live_decision()
    inventory = _make_pause_inventory(
        "zero_adset", {"zero_3d", "healthy_sibling"}, ["zero_3d"]
    )

    with patch("services.autopilot.get_autopilot_config", return_value=_zero_live_config()), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.autopilot._fetch_live_lifetime_lead_evidence", return_value={"zero_3d": 0}), \
         patch("services.decision_policy.score_and_decide", return_value=[decision]), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value={
             "zero_3d": {"adset_id": "zero_adset", "effective_status": "ACTIVE"}
         }), \
         patch("services.adset_pause_guard.fetch_pause_inventory", return_value=inventory), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision") as save_decision, \
         patch("services.autopilot._write_auto_action"), \
         patch("services.autopilot.record_pause_undo") as record_undo, \
         patch("services.autopilot._save_live_daily_state") as save_daily, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live()

    # Правило «0 лидов за 3 дня» доводит кандидата до предложения владельцу,
    # несмотря на min_days_protect=5.
    producer_boundary.assert_proposed(
        "zero_3d",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    assert len(result["proposals"]) == 1
    producer_boundary.assert_no_direct_provider_mutation()
    # Бизнес-эффекты паузы (решение, undo, дневной счётчик) — только после
    # одобрения владельцем, producer их не пишет.
    assert result["paused"] == []
    save_decision.assert_not_called()
    record_undo.assert_not_called()
    save_daily.assert_not_called()


# ---------------------------------------------------------------------------
# Живая перепроверка нулевых лидов ПЕРЕД созданием zero-leads PAUSE-proposal:
# score_and_decide здесь настоящий, поэтому evidence из FB реально решает,
# дойдёт ли кандидат до предложения владельцу.
# ---------------------------------------------------------------------------

def _zero_live_local_ads() -> list[dict]:
    """Кандидат «3 дня, 0 лидов» и здоровый сосед в том же адсете."""
    candidate = _make_local_ad(
        ad_id="zero_3d", days_running=3, leads=0, spend=20.0, cpl=5.0, ctr=1.0
    )
    candidate.update(adset_id="zero_adset", day_since_launch=3)
    sibling = _make_local_ad(
        ad_id="healthy_sibling",
        days_running=10,
        leads=8,
        spend=100.0,
        cpl=12.0,
        ctr=1.0,
        qual_pct=30.0,
        romi=150.0,
    )
    sibling.update(adset_id="zero_adset", day_since_launch=10)
    return [candidate, sibling]


def _run_live_with_real_scoring(evidence_patch):
    """run_autopilot_live с настоящим score_and_decide и подменённым FB-evidence."""
    local_ads = _zero_live_local_ads()
    inventory = _make_pause_inventory(
        "zero_adset", {"zero_3d", "healthy_sibling"}, ["zero_3d"]
    )

    with patch("services.autopilot.get_autopilot_config", return_value=_zero_live_config()), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         evidence_patch, \
         patch("services.autopilot._fetch_candidate_fb_info", return_value={
             "zero_3d": {"adset_id": "zero_adset", "effective_status": "ACTIVE"}
         }), \
         patch("services.adset_pause_guard.fetch_pause_inventory", return_value=inventory), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.autopilot.record_pause_undo"), \
         patch("services.autopilot._save_live_daily_state"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):
        from services.autopilot import run_autopilot_live

        return run_autopilot_live()


def test_live_zero_rule_proposes_only_after_live_evidence_confirms_zero(producer_boundary):
    """Живой FB подтвердил 0 лидов → кандидат доходит до proposal владельцу."""
    result = _run_live_with_real_scoring(
        patch(
            "services.autopilot._fetch_live_lifetime_lead_evidence",
            return_value={"zero_3d": 0},
        )
    )

    producer_boundary.assert_proposed(
        "zero_3d",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    assert len(result["proposals"]) == 1
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []


def test_live_zero_rule_skips_candidate_when_leads_appeared(producer_boundary):
    """Лиды появились между локальным срезом и прогоном → PAUSE не предлагаем."""
    result = _run_live_with_real_scoring(
        patch(
            "services.autopilot._fetch_live_lifetime_lead_evidence",
            return_value={"zero_3d": 4},
        )
    )

    assert producer_boundary.plans == []
    assert result.get("proposals", []) == []
    assert result["paused"] == []
    producer_boundary.assert_no_direct_provider_mutation()


@pytest.mark.parametrize(
    "evidence_patch_kwargs",
    [
        pytest.param({"return_value": {"zero_3d": None}}, id="unknown_evidence"),
        pytest.param({"side_effect": RuntimeError("FB timeout")}, id="fb_read_error"),
    ],
)
def test_live_zero_rule_fails_closed_when_evidence_unavailable(
    producer_boundary,
    caplog,
    evidence_patch_kwargs,
):
    """Неизвестный ответ или ошибка чтения FB → кандидат пропускается с логом."""
    with caplog.at_level(logging.WARNING, logger="services.autopilot"):
        result = _run_live_with_real_scoring(
            patch(
                "services.autopilot._fetch_live_lifetime_lead_evidence",
                **evidence_patch_kwargs,
            )
        )

    assert producer_boundary.plans == []
    assert result.get("proposals", []) == []
    assert result["paused"] == []
    producer_boundary.assert_no_direct_provider_mutation()
    if "side_effect" in evidence_patch_kwargs:
        assert any(
            "lifetime lead evidence" in record.getMessage()
            for record in caplog.records
        ), [record.getMessage() for record in caplog.records]


def test_live_confirmed_waster_precedes_zero_rule_under_cap(producer_boundary):
    local_ads = [
        _make_local_ad(ad_id="zero_3d", days_running=3, leads=0),
        _make_local_ad(ad_id="confirmed", days_running=1, leads=5, spend=300),
    ]
    decisions = [
        _make_zero_live_decision(),
        _make_waster_decision(ad_id="confirmed", adset_id="waster_adset"),
    ]
    inventory = _make_pause_inventory(
        "waster_adset", {"confirmed", "healthy_sibling"}, ["confirmed"]
    )

    with patch(
        "services.autopilot.get_autopilot_config",
        return_value=_zero_live_config(max_pauses_per_run=1),
    ), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.autopilot._fetch_live_lifetime_lead_evidence", return_value={"zero_3d": 0}), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value={
             "confirmed": {"adset_id": "waster_adset", "effective_status": "ACTIVE"}
         }) as fb_info_mock, \
         patch("services.adset_pause_guard.fetch_pause_inventory", return_value=inventory), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live(max_pauses=1)

    fb_info_mock.assert_called_once_with(["confirmed"])
    assert producer_boundary.subject_ids == ["confirmed"]
    assert len(result["proposals"]) == 1
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []


def test_live_zero_rule_removes_active_hold_and_proposes(producer_boundary):
    local_ads = [_make_local_ad(ad_id="zero_3d", days_running=3, leads=0)]
    decision = _make_zero_live_decision()
    inventory = _make_pause_inventory(
        "zero_adset", {"zero_3d", "healthy_sibling"}, ["zero_3d"]
    )
    hold_state = {
        "holds": {
            "zero_3d": {
                "ad_name": "Тест",
                "hold_until": "2099-01-01T00:00:00+05:00",
            }
        }
    }

    with patch(
        "services.autopilot.get_autopilot_config",
        return_value=_zero_live_config(hold_enabled=True),
    ), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.autopilot._fetch_live_lifetime_lead_evidence", return_value={"zero_3d": 0}), \
         patch("services.decision_policy.score_and_decide", return_value=[decision]), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value={
             "zero_3d": {"adset_id": "zero_adset", "effective_status": "ACTIVE"}
         }), \
         patch("services.adset_pause_guard.fetch_pause_inventory", return_value=inventory), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.autopilot._enrich_meetings_for_hold"), \
         patch("services.autopilot_hold.load_hold_state", return_value=hold_state), \
         patch("services.autopilot_hold.save_hold_state") as save_hold, \
         patch("services.autopilot_hold.is_held") as is_held, \
         patch("services.autopilot_hold.should_hold") as should_hold, \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live()

    # Правило «0 лидов» сильнее удержания: hold снимается, кандидат уходит
    # в предложение владельцу, но реальной паузы всё ещё нет.
    producer_boundary.assert_proposed(
        "zero_3d",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    assert len(result["proposals"]) == 1
    assert result["paused"] == []
    producer_boundary.assert_no_direct_provider_mutation()
    assert hold_state["holds"] == {}
    save_hold.assert_called()
    is_held.assert_not_called()
    should_hold.assert_not_called()


def test_live_zero_rule_respects_manual_override():
    local_ads = [_make_local_ad(ad_id="zero_3d", days_running=3, leads=0)]
    decision = _make_zero_live_decision()

    with patch("services.autopilot.get_autopilot_config", return_value=_zero_live_config()), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.autopilot._fetch_live_lifetime_lead_evidence", return_value={"zero_3d": 0}), \
         patch("services.decision_policy.score_and_decide", return_value=[decision]), \
         patch("services.autopilot.get_active_overrides", return_value={"zero_3d": "future"}), \
         patch("services.autopilot._fetch_candidate_fb_info") as fb_info_mock, \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live()

    assert result["paused"] == []
    fb_info_mock.assert_not_called()


def test_live_zero_rule_requires_fresh_active():
    local_ads = [_make_local_ad(ad_id="zero_3d", days_running=3, leads=0)]
    decision = _make_zero_live_decision()

    with patch("services.autopilot.get_autopilot_config", return_value=_zero_live_config()), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.autopilot._fetch_live_lifetime_lead_evidence", return_value={"zero_3d": 0}), \
         patch("services.decision_policy.score_and_decide", return_value=[decision]), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value={
             "zero_3d": {"adset_id": "zero_adset", "effective_status": "PAUSED"}
         }), \
         patch("services.replacement_orchestrator.safe_pause_or_enqueue_replacement") as pause_facade, \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live()

    assert result["paused"] == []
    pause_facade.assert_not_called()


@pytest.mark.parametrize(
    "fb_info",
    [
        {"zero_3d": {"adset_id": "zero_adset", "effective_status": "UNKNOWN"}},
        {"unrelated": {"adset_id": "other_adset", "effective_status": "ACTIVE"}},
    ],
    ids=["unknown_status", "candidate_missing"],
)
def test_live_zero_rule_rejects_unknown_or_missing_fresh_status(fb_info):
    local_ads = [_make_local_ad(ad_id="zero_3d", days_running=3, leads=0)]
    decision = _make_zero_live_decision()

    with patch("services.autopilot.get_autopilot_config", return_value=_zero_live_config()), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.autopilot._fetch_live_lifetime_lead_evidence", return_value={"zero_3d": 0}), \
         patch("services.decision_policy.score_and_decide", return_value=[decision]), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("services.adset_pause_guard.fetch_pause_inventory", return_value={}), \
         patch("services.replacement_orchestrator.safe_pause_or_enqueue_replacement") as pause_facade, \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live()

    assert result["paused"] == []
    pause_facade.assert_not_called()


def test_live_zero_rule_last_active_is_blocked_not_counted(producer_boundary):
    """Последний ACTIVE по правилу «0 лидов» не предлагается и не считается паузой.

    Раньше эта ситуация делегировалась в replacement-оркестратор; теперь
    producer отказывает fail-closed ещё до создания proposal. Общий инвариант
    сохраняется: adset не пустеет, бизнес-эффекты не пишутся.
    """
    local_ads = [_make_local_ad(ad_id="zero_3d", days_running=3, leads=0)]
    decision = _make_zero_live_decision()
    lone_inventory = _make_pause_inventory("zero_adset", {"zero_3d"}, ["zero_3d"])

    with patch("services.autopilot.get_autopilot_config", return_value=_zero_live_config()), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.autopilot._fetch_live_lifetime_lead_evidence", return_value={"zero_3d": 0}), \
         patch("services.decision_policy.score_and_decide", return_value=[decision]), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value={
             "zero_3d": {"adset_id": "zero_adset", "effective_status": "ACTIVE"}
         }), \
         patch("services.adset_pause_guard.fetch_pause_inventory", return_value=lone_inventory), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("agent.repositories.decisions_repo.save_decision") as save_decision, \
         patch("services.autopilot.record_pause_undo") as record_undo, \
         patch("services.autopilot._save_live_daily_state") as save_daily, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live()

    assert producer_boundary.plans == []
    producer_boundary.assert_no_direct_provider_mutation()
    assert result["paused"] == []
    assert result.get("proposals", []) == []
    # Producer отказал fail-closed — прогон фиксирует это как ошибку по кандидату
    assert any(
        "zero_3d" in error and "pause_guard_error" in error
        for error in result["errors"]
    ), result["errors"]
    save_decision.assert_not_called()
    record_undo.assert_not_called()
    save_daily.assert_not_called()
