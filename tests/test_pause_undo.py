"""
Тесты кнопки «↩️ Вернуть» в отчётах пауз автопилота (services/autopilot.py):
undo_map (record_pause_undo/get_pause_undo_entry/mark_pause_undone),
_prune_undo_map (ретеншн, мирроит _prune_stop_map из auto_launch.py),
_build_pause_undo_buttons (рендер кнопок), _send_pause_report (текст+кнопки+fallback).

STATE_FILE переопределяется через monkeypatch на tmp_path — боевой
data/autopilot_state.json не трогаем.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.gateway_test_helpers import install_proposal_recorder, proposal_receipt

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта (чужой незакоммиченный код)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

import services.autopilot as ap_module

_TZ_LOCAL = timezone(timedelta(hours=5))


@pytest.fixture(autouse=True)
def patch_state_file(tmp_path, monkeypatch):
    """Перенаправляет STATE_FILE на tmp_path (как в tests/test_autopilot.py)."""
    monkeypatch.setattr(ap_module, "STATE_FILE", tmp_path / "autopilot_state.json")


def _ad(ad_id="ad1", name="Тестовое объявление"):
    return {"id": ad_id, "name": name}


# ---------------------------------------------------------------------------
# record_pause_undo / get_pause_undo_entry / mark_pause_undone
# ---------------------------------------------------------------------------

def test_record_then_get_returns_entry():
    """После record_pause_undo запись читается с returned=False."""
    ap_module.record_pause_undo("111", "Тестовое объявление")
    entry = ap_module.get_pause_undo_entry("111")

    assert entry is not None
    assert entry["ad_id"] == "111"
    assert entry["ad_name"] == "Тестовое объявление"
    assert entry["returned"] is False


def test_get_unknown_ad_returns_none():
    """Объявления нет в undo_map -> None."""
    assert ap_module.get_pause_undo_entry("999999999") is None


def test_mark_pause_undone_sets_returned_true():
    """mark_pause_undone помечает запись returned=True и возвращает True."""
    ap_module.record_pause_undo("222", "Реклама Б")
    ok = ap_module.mark_pause_undone("222")

    assert ok is True
    assert ap_module.get_pause_undo_entry("222")["returned"] is True


def test_mark_pause_undone_unknown_returns_false():
    """Объявления нет в undo_map -> mark_pause_undone возвращает False."""
    assert ap_module.mark_pause_undone("no-such-id") is False


def test_record_pause_undo_resets_returned_on_repeat_pause():
    """Повторная пауза того же ad_id (после возврата) сбрасывает returned в False —
    новая пауза даёт новую возможность вернуть."""
    ap_module.record_pause_undo("333", "Реклама В")
    ap_module.mark_pause_undone("333")
    assert ap_module.get_pause_undo_entry("333")["returned"] is True

    ap_module.record_pause_undo("333", "Реклама В")
    assert ap_module.get_pause_undo_entry("333")["returned"] is False


# ---------------------------------------------------------------------------
# _prune_undo_map — ретеншн (мирроит _prune_stop_map из auto_launch.py)
# ---------------------------------------------------------------------------

def test_prune_undo_map_removes_old_entries():
    """Записи старше UNDO_MAP_RETENTION_DAYS удаляются, свежие остаются."""
    now = datetime.now(_TZ_LOCAL)
    old_at = (now - timedelta(days=ap_module.UNDO_MAP_RETENTION_DAYS + 1)).isoformat()
    undo_map = {
        "old_ad": {"ad_id": "old_ad", "ad_name": "Старое", "returned": False, "at": old_at},
        "fresh_ad": {"ad_id": "fresh_ad", "ad_name": "Свежее", "returned": False, "at": now.isoformat()},
    }
    ap_module._prune_undo_map(undo_map, now)

    assert "old_ad" not in undo_map
    assert "fresh_ad" in undo_map


def test_prune_undo_map_keeps_only_max_entries():
    """Свыше MAX_UNDO_ENTRIES — обрезаем до самых свежих."""
    now = datetime.now(_TZ_LOCAL)
    undo_map = {}
    for i in range(ap_module.MAX_UNDO_ENTRIES + 10):
        at = (now - timedelta(minutes=i)).isoformat()
        undo_map[f"ad_{i}"] = {"ad_id": f"ad_{i}", "ad_name": "x", "returned": False, "at": at}
    ap_module._prune_undo_map(undo_map, now)

    assert len(undo_map) == ap_module.MAX_UNDO_ENTRIES
    # Самая свежая (i=0, at=now) остаётся, самая старая — уходит
    assert "ad_0" in undo_map
    assert f"ad_{ap_module.MAX_UNDO_ENTRIES + 9}" not in undo_map


def test_save_state_prunes_undo_map():
    """_save_state вызывает _prune_undo_map — протухшие записи не переживают сохранение."""
    now = datetime.now(_TZ_LOCAL)
    old_at = (now - timedelta(days=ap_module.UNDO_MAP_RETENTION_DAYS + 5)).isoformat()
    state = ap_module._load_state()
    state["undo_map"] = {"stale": {"ad_id": "stale", "ad_name": "x", "returned": False, "at": old_at}}
    ap_module._save_state(state)

    reloaded = ap_module._load_state()
    assert "stale" not in reloaded["undo_map"]


# ---------------------------------------------------------------------------
# _build_pause_undo_buttons
# ---------------------------------------------------------------------------

def test_buttons_one_per_ad():
    """По одной кнопке на объявление, callback_data = undo_pause:<ad_id>."""
    ads = [_ad("ad1", "Первое"), _ad("ad2", "Второе")]
    buttons = ap_module._build_pause_undo_buttons(ads)

    assert len(buttons) == 2
    assert buttons[0][0][1] == "undo_pause:ad1"
    assert buttons[1][0][1] == "undo_pause:ad2"
    assert "Первое" in buttons[0][0][0]
    assert "↩️ Вернуть" in buttons[0][0][0]


def test_buttons_capped_at_max_10():
    """Больше MAX_PAUSE_UNDO_BUTTONS объявлений -> кнопок не больше лимита (10)."""
    ads = [_ad(f"ad{i}", f"Объявление {i}") for i in range(15)]
    buttons = ap_module._build_pause_undo_buttons(ads)

    assert ap_module.MAX_PAUSE_UNDO_BUTTONS == 10
    assert len(buttons) == 10


def test_buttons_callback_data_under_64_bytes():
    """callback_data всегда укладывается в лимит Telegram (64 байта)."""
    long_name = "Очень длинное имя объявления которое режется по границе слова при рендере"
    ads = [_ad("1" * 25, long_name)]  # ad_id — максимальная длина по regex (25 цифр)
    buttons = ap_module._build_pause_undo_buttons(ads)

    assert len(buttons) == 1
    callback = buttons[0][0][1]
    assert len(callback.encode("utf-8")) <= 64


def test_buttons_skip_ad_without_id():
    """Объявление без id пропускается — не роняем рендер остальных."""
    ads = [{"name": "Без id"}, _ad("ad2", "С id")]
    buttons = ap_module._build_pause_undo_buttons(ads)

    assert len(buttons) == 1
    assert buttons[0][0][1] == "undo_pause:ad2"


def test_buttons_empty_list_returns_empty():
    """Пустой список пауз -> пустой список кнопок."""
    assert ap_module._build_pause_undo_buttons([]) == []


# ---------------------------------------------------------------------------
# _send_pause_report — интеграция текст+кнопки+fallback
# ---------------------------------------------------------------------------

def test_send_pause_report_plain_without_undo_buttons():
    """Отчёт о ПРЕДЛОЖЕНИЯХ пауз уходит обычным send_telegram БЕЗ кнопок
    «Вернуть» (решение владельца: возвращать нечего — ничего не выключено;
    решения — на карточках предложений)."""
    ads = [_ad("11111", "Реклама А")]
    with patch("services.telegram_bot.send_with_buttons") as mock_buttons, \
         patch("services.notifications.send_telegram") as mock_tg:
        ap_module._send_pause_report(ads)

    mock_buttons.assert_not_called()
    mock_tg.assert_called_once()
    text = mock_tg.call_args[0][0]
    assert "undo_pause:" not in text
    assert "🤖 <b>Автопилот: предлагаю паузу 1" in text
    assert "Ничего не выключено" in text


def test_send_pause_report_fallback_when_buttons_fail():
    """Кнопочный транспорт не используется вовсе — отчёт всегда идёт обычным
    send_telegram, даже если send_with_buttons сломан."""
    ads = [_ad("22222", "Реклама Б")]
    with patch("services.telegram_bot.send_with_buttons", side_effect=RuntimeError("нет сети")), \
         patch("services.notifications.send_telegram") as mock_tg:
        ap_module._send_pause_report(ads)

    mock_tg.assert_called_once()


def test_send_pause_report_fallback_on_import_error():
    """send_with_buttons бросил исключение -> фолбэк, отчёт всё равно уходит."""
    ads = [_ad("33333", "Реклама В")]
    with patch("services.telegram_bot.send_with_buttons", side_effect=RuntimeError("网络")), \
         patch("services.notifications.send_telegram") as mock_tg:
        ap_module._send_pause_report(ads)

    mock_tg.assert_called_once()


# ---------------------------------------------------------------------------
# dry_run — БЕЗ кнопок «↩️ Вернуть» (у dry_run своя ветка apply/applyrun,
# которая ничего не паузит по-настоящему — undo_map трогать нечего)
# ---------------------------------------------------------------------------

def test_dry_run_never_registers_undo_map(tmp_path, monkeypatch):
    """В режиме dry_run объявления НЕ регистрируются в undo_map — ничего не
    запаузено по-настоящему, кнопка «Вернуть» не имеет смысла (только
    apply/applyrun из существующей ветки one-off approval)."""

    def _make_ad(ad_id, spend=200.0):
        return {
            "id": ad_id, "name": f"Объявление {ad_id}", "adset_id": "adset1",
            "adset_type": "L2", "ad_objective": "leadform", "city": "CityA",
            "effective_status": "ACTIVE", "spend": spend, "leads": 3, "cpl": 66.0,
            "ctr": 0.5, "impressions": 1000, "clicks": 10, "days_running": 5,
            "created_time": "2026-06-01T00:00:00+0000",
        }

    ads = [_make_ad("11111")]
    dry_run_cfg = {
        "enabled": True, "mode": "dry_run",
        "max_pauses_per_run": 3, "min_hours_between_runs": 3,
    }

    with patch("services.autopilot.get_autopilot_config", return_value=dry_run_cfg), \
         patch("services.autopilot._load_state", return_value={
             "last_run_at": None, "last_run_window": None,
             "manual_overrides": {}, "pending_approvals": {}, "undo_map": {},
         }), \
         patch("services.autopilot._save_state"), \
         patch("services.autopilot.record_pause_undo") as mock_record, \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("agent.analyzer.get_fresh_ad_statuses", return_value={}), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("services.action_producer_gateway.execute_pause"), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True) as mock_buttons:

        result = ap_module.run_autopilot(trigger="manual")

    assert result["mode"] == "dry_run"
    mock_record.assert_not_called()
    # Кнопки dry_run — apply/applyrun, НЕ undo_pause
    if mock_buttons.called:
        _text, buttons = mock_buttons.call_args[0]
        callbacks = [btn[1] for row in buttons for btn in row]
        assert not any(cb.startswith("undo_pause:") for cb in callbacks)


# ---------------------------------------------------------------------------
# РЕГРЕССИЯ: run_autopilot(active) с РЕАЛЬНЫМИ _load_state/_save_state —
# кнопки «↩️ Вернуть» не должны быть мертвы (баг из CTO-ревью).
# ---------------------------------------------------------------------------

def _make_active_ad(ad_id: str, name: str, spend: float = 100.0) -> dict:
    """Минимальный набор полей для полного прохода run_autopilot(active).

    apply_decision_tree и apply_portfolio_decisions мокаются в тесте ниже,
    поэтому портфельная группа (хорошее+плохие) не нужна — recommendation
    проставляет мок apply_decision_tree.
    """
    return {
        "id": ad_id,
        "name": name,
        "effective_status": "ACTIVE",
        "status": "ACTIVE",
        "recommendation": "ДЕРЖАТЬ",  # перезапишется моком apply_decision_tree
        "reason": "",
        "spend": spend,
        "leads": 3,
        "cpl": 33.0,
        "ctr": 1.0,
        "cpm": 5.0,
        "romi": None,
        "qual_pct": None,
        "payments": None,
        "days_running": 10,
        "city": "CityA",
        "adset_type": "L2",
        "ad_objective": "leadform",
    }


def test_run_autopilot_active_real_state_keeps_concurrent_undo_map(tmp_path, monkeypatch):
    """Регрессия: финальный _save_state в active-ветке не затирает undo_map с диска.

    Изначально undo_map писал record_pause_undo прямо внутри цикла пауз, а
    финальный `_save_state({**state, "last_run_at": ...})` сохранял устаревшую
    in-memory копию state (загруженную ДО цикла) и убивал кнопки «↩️ Вернуть».
    Прямых пауз в автопилоте больше нет — undo пишет уже execution boundary
    ПОСЛЕ одобрения владельца, то есть параллельно прогону producer'а. Риск
    затирания от этого только вырос, поэтому инвариант сохранён: запись
    undo_map, появившаяся на диске во время прогона, обязана выжить.

    Параллельного писателя имитируем внутри propose_pause (в момент создания
    предложения по кандидату), т.к. это единственная точка прогона, где
    гарантированно уже загружена устаревшая in-memory копия state.

    КРИТИЧНО: НЕ мокаем services.autopilot._load_state/_save_state — мок с
    фиксированным dict возвращал бы ОДИН И ТОТ ЖЕ объект, и «устаревшая копия»
    физически не смогла бы отличаться от свежей, то есть баг был бы замаскирован.
    Подменяются только пути к файлам, весь путь идёт через реальный диск.
    """
    monkeypatch.setattr(ap_module, "_AUTO_ACTIONS_FILE", tmp_path / "auto_actions.json")
    monkeypatch.setattr(ap_module, "_CLASSIC_DAILY_STATE_FILE", tmp_path / "autopilot_classic_daily.json")

    ads = [
        _make_active_ad("ad_undo_1", "Реклама 1", spend=300.0),
        _make_active_ad("ad_undo_2", "Реклама 2", spend=200.0),
        _make_active_ad("ad_undo_3", "Реклама 3", spend=100.0),
    ]
    cfg = {
        "enabled": True, "mode": "active",
        "max_pauses_per_run": 3, "min_hours_between_runs": 3,
    }

    from services import adset_pause_guard

    adset_by_ad_id = {
        "ad_undo_1": "910001",
        "ad_undo_2": "910002",
        "ad_undo_3": "910003",
    }

    def fake_pause_inventory(ad_ids):
        """Возвращает точный контекст кандидата и доказанную ACTIVE-замену."""
        inventories = {}
        for ad_id in ad_ids:
            adset_id = adset_by_ad_id[ad_id]
            replacement_id = f"replacement-{ad_id}"
            inventories[adset_id] = {
                "adset_id": adset_id,
                "active_ids": {ad_id, replacement_id},
                "candidate_context": {
                    ad_id: {
                        "ad_id": ad_id,
                        "adset_id": adset_id,
                        "name": ad_id,
                        "configured_status": "ACTIVE",
                        "effective_status": "ACTIVE",
                    },
                },
                "inventory_context": {
                    current_id: {
                        "ad_id": current_id, "adset_id": adset_id, "name": current_id,
                        "configured_status": "ACTIVE", "effective_status": "ACTIVE",
                    }
                    for current_id in (ad_id, replacement_id)
                },
                "complete": True,
                "pages_read": 1,
                "error": None,
            }
        return inventories

    def fake_exact_contexts(ad_ids, *, require_names=True):
        del require_names
        wanted = {str(ad_id) for ad_id in ad_ids}
        contexts = {
            ad_id: context
            for inventory in fake_pause_inventory(list(ad_ids)).values()
            for ad_id, context in inventory["candidate_context"].items()
            if ad_id in wanted
        }
        return contexts, None

    monkeypatch.setattr(adset_pause_guard, "_LOCKS_DIR", tmp_path / "pause_locks")
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory", fake_pause_inventory
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts", fake_exact_contexts
    )
    # install_proposal_recorder закрывает FB-мутаторы и execution boundary Mock'ами;
    # запись proposal ниже переопределяем, чтобы вклинить параллельного писателя.
    recorder = install_proposal_recorder(monkeypatch, tmp_path)

    def record_proposal_and_undo(plan, *, now=None):
        """Пишет план и, как исполнение после одобрения, кладёт undo-запись на диск."""
        del now
        recorder.plans.append(plan)
        for target in plan.targets:
            ap_module.record_pause_undo(target.subject_id, f"Реклама {target.subject_id}")
        return proposal_receipt(f"proposal-{len(recorder.plans)}")

    monkeypatch.setattr(
        "services.owner_action_repository.propose_action", record_proposal_and_undo
    )

    with patch("services.autopilot.get_autopilot_config", return_value=cfg), \
         patch("agent.analyzer.refresh_statuses_in_place"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("agent.analyzer.apply_portfolio_decisions"), \
         patch("services.adset_pause_guard.fetch_pause_inventory", side_effect=fake_pause_inventory), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.telegram_bot.send_with_buttons", return_value=True):

        result = ap_module.run_autopilot(trigger="manual")

    assert result["ran"] is True
    assert result["mode"] == "active"
    assert result["errors"] == []
    # Автопилот только предлагает: применённых пауз ноль, предложений — три.
    assert result["paused"] == []
    assert len(result["proposals"]) == 3, f"ожидали 3 предложения, получили: {result}"
    recorder.assert_no_direct_provider_mutation()

    # ГЛАВНАЯ проверка регрессии: undo-записи, появившиеся на диске во время
    # прогона, читаемы после него — иначе кнопка «↩️ Вернуть» будет мертва.
    for ad_id in adset_by_ad_id:
        entry = ap_module.get_pause_undo_entry(ad_id)
        assert entry is not None, (
            f"undo-запись для {ad_id} потеряна — финальный _save_state затёр "
            f"undo_map устаревшей in-memory копией state"
        )
        assert entry["ad_id"] == ad_id
        assert entry["returned"] is False

    # last_run_at тоже должен сохраниться (не только undo_map) — фикс не должен
    # случайно потерять другие поля state при финальном сохранении.
    final_state = ap_module._load_state()
    assert final_state["last_run_at"] is not None
