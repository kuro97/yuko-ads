"""
Тесты дневного лимита пауз автопилота (LIVE и классика).

Проект approval-first: автопилот НЕ паузит Facebook сам — он создаёт владельцу
PAUSE-proposal, а мутация происходит только после одобрения. Поэтому дневной
кап здесь ограничивает ЧИСЛО ПРЕДЛОЖЕНИЙ за сутки (`to_act[:remaining_today]`,
`cap = min(cap, remaining_today)`), а сам счётчик `pauses_today` растёт только
от РЕАЛЬНО применённых пауз (`paused_ids` + проекция AUTO_ACTION-эффектов) —
предложение паузой ещё не является.

Проверяем:
1. Остаток дневной квоты ограничивает число созданных предложений
2. При исчерпанном лимите возвращается skipped="daily_cap" и предложений нет
3. Сброс счётчика на новую дату
4. Создание предложений НЕ инкрементит дневной счётчик
5. Ни один путь не мутирует Facebook напрямую
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import install_proposal_recorder

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())

_TZ = timezone(timedelta(hours=5))


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_live_daily_state(tmp_path, monkeypatch):
    """Перенаправляет _LIVE_DAILY_STATE_FILE на tmp_path для изоляции тестов."""
    import services.autopilot as ap
    daily_state_path = tmp_path / "autopilot_live_daily.json"
    monkeypatch.setattr(ap, "_LIVE_DAILY_STATE_FILE", daily_state_path)
    yield daily_state_path


def _shared_adset_inventory(ad_ids, *, adset_id: str = "700100") -> dict:
    """Live inventory: все кандидаты в ОДНОМ adset плюс запасное ACTIVE-объявление.

    Запасное объявление обязательно: без него last-active guard оставил бы
    последнего активного и срезал часть кандидатов, а тест про дневной кап
    измерял бы уже не кап. С запасным объявлением единственный ограничитель
    числа предложений — дневная квота, что и проверяется в этом файле.
    """
    ids = [str(ad_id) for ad_id in ad_ids]
    spare = f"spare-{adset_id}"
    context = {
        ad_id: {
            "ad_id": ad_id,
            "adset_id": adset_id,
            "name": ad_id,
            "configured_status": "ACTIVE",
            "effective_status": "ACTIVE",
        }
        for ad_id in [*ids, spare]
    }
    return {
        adset_id: {
            "adset_id": adset_id,
            "active_ids": {*ids, spare},
            "candidate_context": {ad_id: context[ad_id] for ad_id in ids},
            "inventory_context": context,
            "complete": True,
            "pages_read": 1,
            "error": None,
        }
    }


@pytest.fixture(autouse=True)
def producer_boundary(tmp_path, monkeypatch):
    """Producer-граница: живой guard + запись proposal вместо БД + запрет мутаций.

    Guard и producer читают ОДИН И ТОТ ЖЕ inventory (producer вызывает
    `adset_pause_guard.fetch_pause_inventory` через closure), поэтому patch
    инвентаря в конкретном тесте меняет картину мира сразу для обоих — в проде
    расхождения между ними нет, и подменять их по отдельности было бы обманом.

    Возвращает RecordedProposals: что автопилот предложил владельцу и чего он
    при этом НЕ сделал (прямые FB-мутации и execution boundary под Mock'ами).
    """
    from services import adset_pause_guard

    def default_inventory(ad_ids):
        return _shared_adset_inventory(ad_ids)

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

    monkeypatch.setattr(adset_pause_guard, "fetch_pause_inventory", default_inventory)
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory", current_inventory
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts", fake_exact_contexts
    )
    return install_proposal_recorder(monkeypatch, tmp_path)


def _make_cfg(max_pauses_per_run: int = 3, max_pauses_per_day: int = 6) -> dict:
    return {
        "enabled": True,
        "kill_switch": False,
        "mode": "active",
        "max_pauses_per_run": max_pauses_per_run,
        "max_pauses_per_day": max_pauses_per_day,
        "min_days_protect": 0,
    }


def _run_live_with_n_candidates(n_candidates: int, cfg: dict) -> dict:
    """Запускает run_autopilot_live с n_candidates кандидатами на паузу.

    Все кандидаты лежат в одном adset, где есть ещё и запасное ACTIVE-объявление
    (см. _shared_adset_inventory), поэтому guardrail пропускает всех — число
    созданных предложений режет только дневная квота.
    """
    from services.autopilot import run_autopilot_live

    local_ads = [
        {"ad_id": f"ad{i}", "ad_name": f"Ad {i}", "days_running": 10}
        for i in range(n_candidates)
    ]
    decisions = [
        {"ad_id": f"ad{i}", "ad_name": f"Ad {i}", "action": "PAUSE",
         "score": -i, "reasons": ["test"]}
        for i in range(n_candidates)
    ]
    fb_info = {
        f"ad{i}": {"adset_id": "700100", "effective_status": "ACTIVE"}
        for i in range(n_candidates)
    }

    with patch("services.autopilot.get_autopilot_config", return_value=cfg), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value=fb_info), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.autopilot._write_auto_action"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         patch("services.autopilot.get_active_overrides", return_value={}), \
         patch("services.telegram_bot.send_with_buttons", return_value=False):
        return run_autopilot_live(max_pauses=cfg["max_pauses_per_run"], trigger="cron")


# ---------------------------------------------------------------------------
# Тест 1: три прогона по 3 кандидата при max_pauses_per_day=6 → суммарно ≤ 6 пауз
# ---------------------------------------------------------------------------

def test_daily_cap_limits_total_pauses(tmp_path, monkeypatch, producer_boundary):
    """Остаток квоты режет число предложений, при исчерпании лимита → daily_cap.

    Раньше тест считал реальные вызовы pause_ad. Прямого мутатора больше нет:
    автопилот только предлагает, поэтому квота измеряется числом созданных
    proposal. Инвариант тот же — за сутки автопилот не может «протолкнуть»
    больше max_pauses_per_day отключений.
    """
    import services.autopilot as ap
    state_path = tmp_path / "daily.json"
    monkeypatch.setattr(ap, "_LIVE_DAILY_STATE_FILE", state_path)

    today = datetime.now(_TZ).date().isoformat()
    # Предустанавливаем: осталась 1 пауза из 6
    state_path.write_text(json.dumps({"date": today, "pauses_today": 5}), encoding="utf-8")

    cfg = _make_cfg(max_pauses_per_run=3, max_pauses_per_day=6)

    # Прогон с remaining=1: cap=min(3,1)=1 → ровно одно предложение
    r1 = _run_live_with_n_candidates(3, cfg)
    assert r1["ran"] is True, f"Прогон 1: ожидали ran=True, got: {r1}"
    assert len(r1["proposals"]) == 1, (
        f"остаток квоты 1 → ровно 1 предложение, got: {r1['proposals']}"
    )
    assert r1["paused"] == [], "предложение — ещё не пауза, paused обязан быть пуст"
    producer_boundary.assert_no_direct_provider_mutation()

    # Счётчик пауз НЕ растёт от предложений: он считает только применённые паузы
    # (см. _projected_pause_count по AUTO_ACTION-эффектам). Иначе одно и то же
    # отключение списывалось бы с квоты дважды — на предложении и на исполнении.
    state_after = ap._load_live_daily_state()
    assert state_after["pauses_today"] == 5, (
        f"предложение не должно тратить квоту применённых пауз: {state_after}"
    )

    # Принудительно исчерпываем лимит
    state_path.write_text(json.dumps({"date": today, "pauses_today": 6}), encoding="utf-8")

    # Следующий прогон при remaining=0 → daily_cap, предложений не создаётся
    proposals_before = len(producer_boundary.plans)
    r2 = _run_live_with_n_candidates(3, cfg)
    assert r2["ran"] is False, f"Прогон при исчерпанном лимите: ожидали ran=False, got: {r2}"
    assert r2["skipped"] == "daily_cap", f"Ожидали skipped='daily_cap', got: {r2['skipped']}"
    assert len(producer_boundary.plans) == proposals_before, (
        "при исчерпанной квоте автопилот не должен создавать новых предложений"
    )


# ---------------------------------------------------------------------------
# Тест 2: при выбранном лимите немедленный skipped="daily_cap"
# ---------------------------------------------------------------------------

def test_daily_cap_returns_daily_cap_when_exhausted(tmp_path, monkeypatch, producer_boundary):
    """Исчерпанный лимит → skipped='daily_cap', ни предложений, ни мутаций FB."""
    import services.autopilot as ap

    today = datetime.now(_TZ).date().isoformat()
    # Записываем state: уже сделано 6 пауз сегодня
    state_file = tmp_path / "autopilot_live_daily.json"
    state_file.write_text(json.dumps({"date": today, "pauses_today": 6}), encoding="utf-8")
    monkeypatch.setattr(ap, "_LIVE_DAILY_STATE_FILE", state_file)

    cfg = _make_cfg(max_pauses_per_run=3, max_pauses_per_day=6)

    result = _run_live_with_n_candidates(3, cfg)

    assert result["ran"] is False, f"Ожидали ran=False, got: {result}"
    assert result["skipped"] == "daily_cap", f"Ожидали skipped='daily_cap', got: {result['skipped']}"
    assert result["paused"] == [], "При daily_cap не должно быть реальных пауз"
    assert producer_boundary.plans == [], "При daily_cap не должно быть и предложений"
    producer_boundary.assert_no_direct_provider_mutation()


# ---------------------------------------------------------------------------
# Тест 3: сброс счётчика на новую дату
# ---------------------------------------------------------------------------

def test_daily_state_resets_on_new_date(tmp_path, monkeypatch):
    """State с вчерашней датой сбрасывается на 0 при загрузке."""
    import services.autopilot as ap

    yesterday = (datetime.now(_TZ).date() - timedelta(days=1)).isoformat()
    state_file = tmp_path / "autopilot_live_daily.json"
    state_file.write_text(json.dumps({"date": yesterday, "pauses_today": 99}), encoding="utf-8")
    monkeypatch.setattr(ap, "_LIVE_DAILY_STATE_FILE", state_file)

    state = ap._load_live_daily_state()

    today = datetime.now(_TZ).date().isoformat()
    assert state["date"] == today, f"Дата должна быть сегодняшней: {state['date']}"
    assert state["pauses_today"] == 0, f"Счётчик должен обнулиться: {state['pauses_today']}"


# ---------------------------------------------------------------------------
# Тест 4: частичная квота — второй прогон берёт остаток
# ---------------------------------------------------------------------------

def test_daily_cap_partial_quota(tmp_path, monkeypatch, producer_boundary):
    """При остатке 2 из лимита 6 прогон с cap=3 предлагает ровно 2 отключения.

    Edge-случай «квота меньше числа кандидатов»: кап должен срезать хвост
    кандидатов ДО создания предложений, а не после — иначе владелец получил бы
    больше кнопок, чем разрешает суточный лимит.
    """
    import services.autopilot as ap

    today = datetime.now(_TZ).date().isoformat()
    state_file = tmp_path / "autopilot_live_daily.json"
    # Уже потрачено 4 из 6
    state_file.write_text(json.dumps({"date": today, "pauses_today": 4}), encoding="utf-8")
    monkeypatch.setattr(ap, "_LIVE_DAILY_STATE_FILE", state_file)

    cfg = _make_cfg(max_pauses_per_run=3, max_pauses_per_day=6)

    # 3 кандидата, но лимит позволяет только 2
    result = _run_live_with_n_candidates(3, cfg)

    assert result["ran"] is True
    assert result["paused"] == [], "автопилот не применяет паузы сам"
    assert len(result["proposals"]) == 2, (
        f"Ожидали ровно 2 предложения (остаток квоты), получили: {result['proposals']}"
    )
    assert len(producer_boundary.plans) == 2
    for subject_id in producer_boundary.subject_ids:
        producer_boundary.assert_proposed(
            subject_id,
            kind=ProposalKind.PAUSE,
            origin=ProposalOrigin.AUTOPILOT,
            action_kind="PAUSE_AD",
        )
    producer_boundary.assert_no_direct_provider_mutation()


# ---------------------------------------------------------------------------
# D3 (спека DT): дневной лимит пауз в КЛАССИЧЕСКОМ автопилоте
# (_run_autopilot_inner / run_autopilot, а не run_autopilot_live выше).
# Используем хелперы генерации кандидатов из tests/test_autopilot.py, чтобы
# не дублировать логику построения портфельной группы для Decision Tree.
# ---------------------------------------------------------------------------

from tests.test_autopilot import _make_5_candidates  # noqa: E402


def _classic_active_cfg(max_pauses_per_run: int = 3, max_pauses_per_day: int = 6) -> dict:
    return {
        "enabled": True,
        "kill_switch": False,
        "mode": "active",
        "max_pauses_per_run": max_pauses_per_run,
        "max_pauses_per_day": max_pauses_per_day,
        "min_hours_between_runs": 3,
    }


def _run_classic_autopilot(cfg: dict, ads: list[dict]):
    """Запускает run_autopilot (классика) с полностью замоканными внешними границами.

    Inventory каждого кандидата — свой adset с доказанной ACTIVE-заменой, иначе
    propose_pause справедливо отказал бы с LAST_EFFECTIVE_ACTIVE и тест мерил бы
    guard, а не дневной кап. Producer читает этот же inventory через фикстуру
    producer_boundary.
    """
    from services.autopilot import run_autopilot

    def fake_inventory(ad_ids):
        return {
            str(800000 + index): {
                "adset_id": str(800000 + index),
                "active_ids": {ad_id, f"replacement-{ad_id}"},
                "candidate_context": {
                    ad_id: {
                        "ad_id": ad_id,
                        "adset_id": str(800000 + index),
                        "name": ad_id,
                        "configured_status": "ACTIVE",
                        "effective_status": "ACTIVE",
                    }
                },
                "inventory_context": {
                    current_id: {
                        "ad_id": current_id,
                        "adset_id": str(800000 + index),
                        "name": current_id,
                        "configured_status": "ACTIVE",
                        "effective_status": "ACTIVE",
                    }
                    for current_id in (ad_id, f"replacement-{ad_id}")
                },
                "complete": True,
                "pages_read": 1,
                "error": None,
            }
            for index, ad_id in enumerate(ad_ids)
        }

    with patch("services.autopilot.get_autopilot_config", return_value=cfg), \
         patch("services.autopilot._load_state", return_value={"last_run_at": None, "last_run_window": None, "manual_overrides": {}}), \
         patch("services.autopilot._save_state"), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("agent.analyzer.get_ads_with_metrics", return_value=ads), \
         patch("agent.analyzer.refresh_statuses_in_place"), \
         patch("integrations.amo.sync_amo_data", return_value={}), \
         patch("agent.analyzer.apply_decision_tree", return_value={"action": "ОТКЛЮЧИТЬ", "reason": "тест"}), \
         patch("services.adset_pause_guard.fetch_pause_inventory", side_effect=fake_inventory), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):
        return run_autopilot(trigger="manual")


def test_classic_daily_cap_blocks_after_limit(tmp_path, monkeypatch, producer_boundary):
    """Классика: дневной лимит исчерпан (6/6) → skipped_reason=daily_cap,
    active-путь пропускается целиком: ни предложений, ни мутаций FB."""
    import services.autopilot as ap
    classic_state_path = tmp_path / "autopilot_classic_daily.json"
    monkeypatch.setattr(ap, "_CLASSIC_DAILY_STATE_FILE", classic_state_path)

    today = datetime.now(_TZ).date().isoformat()
    classic_state_path.write_text(json.dumps({"date": today, "pauses_today": 6}), encoding="utf-8")

    cfg = _classic_active_cfg(max_pauses_per_run=3, max_pauses_per_day=6)
    ads = _make_5_candidates()

    result = _run_classic_autopilot(cfg, ads)

    assert result["ran"] is True, f"Ожидали ran=True (лимит исчерпан, но прогон 'состоялся'), got: {result}"
    assert result["skipped_reason"] == "daily_cap", f"Ожидали skipped_reason='daily_cap', got: {result}"
    assert result["paused"] == []
    assert producer_boundary.plans == [], "при исчерпанном дневном лимите предложений быть не должно"
    producer_boundary.assert_no_direct_provider_mutation()


def test_classic_proposals_do_not_increment_daily_counter(tmp_path, monkeypatch, producer_boundary):
    """Классика: успешный прогон создаёт PAUSE-предложения и НЕ трогает счётчик.

    Раньше тест проверял «счётчик вырос ровно на число реально применённых пауз».
    Прямой мутатор удалён, паузы применяются только после одобрения владельца,
    поэтому в прогоне producer'а применённых пауз ноль. Инвариант перевёрнут и
    стал строже: квота списывается на ИСПОЛНЕНИИ, а не на предложении — иначе
    отклонённое владельцем предложение навсегда съедало бы дневной лимит.
    """
    import services.autopilot as ap
    classic_state_path = tmp_path / "autopilot_classic_daily.json"
    monkeypatch.setattr(ap, "_CLASSIC_DAILY_STATE_FILE", classic_state_path)

    today = datetime.now(_TZ).date().isoformat()
    classic_state_path.write_text(json.dumps({"date": today, "pauses_today": 0}), encoding="utf-8")

    cfg = _classic_active_cfg(max_pauses_per_run=3, max_pauses_per_day=6)
    ads = _make_5_candidates()  # портфельный слой выдаст 3 кандидата (cap=3)

    result = _run_classic_autopilot(cfg, ads)

    assert result["ran"] is True
    assert result["paused"] == [], "классика больше не применяет паузы сама"
    assert result["proposals"], "кандидаты были — предложения владельцу обязаны появиться"
    assert len(producer_boundary.plans) == len(result["proposals"])
    for subject_id in producer_boundary.subject_ids:
        producer_boundary.assert_proposed(
            subject_id,
            kind=ProposalKind.PAUSE,
            origin=ProposalOrigin.AUTOPILOT,
            action_kind="PAUSE_AD",
        )
    producer_boundary.assert_no_direct_provider_mutation()

    state_after = ap._load_classic_daily_state()
    assert state_after["pauses_today"] == 0, \
        f"предложения не должны списывать дневную квоту пауз: {state_after}"


def test_classic_remaining_quota_caps_proposal_count(tmp_path, monkeypatch, producer_boundary):
    """Классика: остаток дневной квоты (1 из 3) режет прогон до 1 предложения.

    Проверяет, что `to_act[:remaining_today]` работает поверх max_pauses_per_run:
    без этого среза три кроновых прогона в сутки предложили бы владельцу
    3×max_pauses_per_run отключений вместо max_pauses_per_day.
    """
    import services.autopilot as ap
    classic_state_path = tmp_path / "autopilot_classic_daily.json"
    monkeypatch.setattr(ap, "_CLASSIC_DAILY_STATE_FILE", classic_state_path)

    today = datetime.now(_TZ).date().isoformat()
    classic_state_path.write_text(json.dumps({"date": today, "pauses_today": 2}), encoding="utf-8")

    cfg = _classic_active_cfg(max_pauses_per_run=3, max_pauses_per_day=3)
    ads = _make_5_candidates()

    result = _run_classic_autopilot(cfg, ads)

    assert result["ran"] is True
    assert result["skipped_reason"] is None
    assert len(result["proposals"]) == 1, (
        f"остаток квоты 1 → ровно 1 предложение, got: {result['proposals']}"
    )
    producer_boundary.assert_no_direct_provider_mutation()


def test_classic_daily_counter_resets_on_new_day(tmp_path, monkeypatch):
    """Классика: state с вчерашней датой сбрасывается на 0 при загрузке
    (_load_classic_daily_state), отдельно от LIVE-счётчика."""
    import services.autopilot as ap
    classic_state_path = tmp_path / "autopilot_classic_daily.json"
    monkeypatch.setattr(ap, "_CLASSIC_DAILY_STATE_FILE", classic_state_path)

    yesterday = (datetime.now(_TZ).date() - timedelta(days=1)).isoformat()
    classic_state_path.write_text(json.dumps({"date": yesterday, "pauses_today": 99}), encoding="utf-8")

    state = ap._load_classic_daily_state()

    today = datetime.now(_TZ).date().isoformat()
    assert state["date"] == today, f"Дата должна быть сегодняшней: {state['date']}"
    assert state["pauses_today"] == 0, f"Счётчик должен обнулиться: {state['pauses_today']}"


def test_classic_and_live_counters_are_independent(tmp_path, monkeypatch):
    """Классический и LIVE дневные счётчики паузят независимо — разные файлы,
    исчерпание одного не влияет на остаток другого."""
    import services.autopilot as ap
    classic_path = tmp_path / "autopilot_classic_daily.json"
    live_path = tmp_path / "autopilot_live_daily.json"
    monkeypatch.setattr(ap, "_CLASSIC_DAILY_STATE_FILE", classic_path)
    monkeypatch.setattr(ap, "_LIVE_DAILY_STATE_FILE", live_path)

    today = datetime.now(_TZ).date().isoformat()
    # LIVE лимит исчерпан
    live_path.write_text(json.dumps({"date": today, "pauses_today": 6}), encoding="utf-8")
    # Классика ещё не паузила сегодня
    classic_state = ap._load_classic_daily_state()
    assert classic_state["pauses_today"] == 0, \
        "Классический счётчик не должен зависеть от исчерпанного LIVE-лимита"
