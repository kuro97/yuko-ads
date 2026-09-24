"""
Сквозные (интеграционные) тесты Бюджет-пилота Фазы 2 — services.budget_scaler.run_budget_scaling.

Проверяют сценарии из §9 спеки ARCH-phase2-budget-pilot.md на уровне ЦЕЛОГО прогона
(не отдельных функций), с полным набором моков:
- FB (_fetch_candidate_fb_info, _fetch_all_account_adset_budgets,
  _fetch_adset_budgets) — БЕЗ реальной сети (pytest-socket блокирует сокеты).
- propose_scale — producer-граница: active-прогон только СОЗДАЁТ owner-proposal.
- Telegram (send_telegram, send_critical_alert, send_with_buttons).
- План-гейт (read_general_plan + недельные расходы/выручка) — валидный план с запасом.
- decisions_repo.save_decision — проверяем факт вызова и action.
- services.budget_daily_cap — state-файл капа перенаправлен в tmp_path (isolated_daily_cap_state
  фикстура, автоприменяется). services.budget_scaler — state-файл кулдауна тоже в tmp_path.

Approval-first: прямого подъёма бюджета из скейлера НЕТ. В active-режиме прогон
создаёт предложение владельцу (result["proposals"]), а result["scaled"] всегда
пуст; реальная мутация FB живёт в execution boundary после одобрения в Telegram.
Дневной кап (services.budget_daily_cap) поэтому расходуется НЕ в момент
предложения, а при исполнении — в многопрогонных сценариях исполнение
одобренного подъёма эмулируется прямым budget_daily_cap.record_raise.

Задача T5 волны 3 спеки ARCH-phase2-budget-pilot.md.
"""

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pytest

from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import (
    blocked_outcome,
    install_proposal_recorder,
    install_provider_mutation_guard,
    proposal_outcome,
)

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импортов проекта (как в test_budget_scaler.py)
import sys as _sys
if "google.genai" not in _sys.modules:
    _sys.modules["google.genai"] = MagicMock()
if "google" not in _sys.modules:
    _sys.modules["google"] = MagicMock()

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
def no_direct_fb_mutation(monkeypatch):
    """Ни один сценарий прогона не имеет права мутировать FB напрямую.

    Гард висит на всех тестах файла (а не только на «блокирующих»), поэтому
    возврат прямого подъёма бюджета в скейлер уронит и happy-path тоже."""
    return install_provider_mutation_guard(monkeypatch)


# ---------------------------------------------------------------------------
# Вспомогательные данные (аналогично tests/test_budget_scaler.py)
# ---------------------------------------------------------------------------

def _make_local_ad(
    ad_id: str = "ad1",
    ad_name: str = "Победитель",
    qual_pct: float = 20.0,
    romi: float = 150.0,
    spend: float = 80.0,
    leads: int = 8,
    payments: int | None = 3,
    days_running: int = 10,
    outcomes_matched_at: str | None = None,
) -> dict:
    """Объявление из локальной БД (creative_kb) с метриками для отбора кандидата."""
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "city": "CityA",
        "adset_type": "L2",
        "adset_id": None,
        "spend": spend,
        "leads": leads,
        "qual_pct": qual_pct,
        "romi": romi,
        "cpl": spend / leads if leads else 0,
        "ctr": 2.0,
        "hook_rate": 30.0,
        "impressions": 5000,
        "video_p25": 0,
        "video_p100": 0,
        "video_views_3s": 0,
        "payments": payments,
        "outcomes_matched_at": outcomes_matched_at,
        "days_running": days_running,
        "effective_status": "ACTIVE",
        "recommendation": "ЖДАТЬ",
        "reason": "",
    }


def _adset_info(budget_usd: float = 100.0, status: str = "ACTIVE", name: str = "Тестовый адсет") -> dict:
    return {"daily_budget_usd": budget_usd, "effective_status": status, "name": name}


def _base_cfg(**overrides) -> dict:
    """Базовый конфиг автопилота с включённым бюджет-пилотом. plan_sheet_id задан —
    в тестах, где план-гейт нужен, дополнительно патчим read_general_plan/week-функции.
    """
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
    }
    cfg.update(overrides)
    return cfg


# План с большим запасом и мягкой юниткой — гейт пропускает поток дальше.
_VALID_PLAN = {
    "week_label": "неделя 1 (2026-07-01–07)",
    "budget_usd": 100_000.0,
    "revenue_plan_lcy": 10_000_000.0,
    "unit_target": 0.99,
}

# План с превышенной юниткой факта над планом — блокирует ЛЮБОЙ подъём.
_TIGHT_PLAN = {
    "week_label": "неделя 1 (2026-07-01–07)",
    "budget_usd": 100_000.0,
    "revenue_plan_lcy": 10_000_000.0,
    "unit_target": 0.01,  # 1% таргет — легко превышается фактом
}


class _ScalingMocks:
    """Контекст-менеджер, входящий во ВСЕ моки одного прогона run_budget_scaling
    и отдающий именованный доступ к нужным (без хрупкой индексации по позиции).
    """

    def __init__(
        self,
        local_ads: list[dict],
        all_budgets: dict,
        cfg: dict,
        plan_data: dict | None = _VALID_PLAN,
        fb_ad_info: dict | None = None,
        revenue_lcy: float = 200_000.0,
        fb_week_spend: float = 0.0,
        proposal_blocked: bool = False,
        patch_producer: bool = True,
    ):
        if fb_ad_info is None:
            fb_ad_info = {ad["ad_id"]: {"adset_id": "adset1", "effective_status": "ACTIVE"} for ad in local_ads}

        # ВАЖНО: эмулируем реальное поведение FB — _fetch_candidate_fb_info
        # возвращает данные ТОЛЬКО для тех ad_id, которые реально были переданы
        # ему в вызове (в проде это все local_ads, см. services/budget_scaler.py
        # шаг 4). Раньше мок был return_value=fb_ad_info без учёта аргументов —
        # тест был ложно-зелёным, т.к. подсовывал adset_id даже для объявлений,
        # которые прод не передавал (например, слив с payments=0, если fb_ad_info
        # не был явно сужен). side_effect воспроизводит фильтрацию по аргументам.
        def _fetch_candidate_fb_info_side_effect(ad_ids: list[str]) -> dict:
            return {aid: info for aid, info in fb_ad_info.items() if aid in ad_ids}

        # Producer возвращает КВИТАНЦИЮ предложения, а не результат исполнения:
        # он не резервирует дневной кап и не трогает FB (это делает execution
        # boundary после одобрения владельцем). Дневной кап здесь намеренно НЕ
        # расходуется — иначе тест закреплял бы удалённое поведение «producer
        # сам применил подъём».
        def _propose_scale_side_effect(recommendation: dict, **kwargs):
            del recommendation, kwargs
            if proposal_blocked:
                return blocked_outcome("PROVIDER_REJECTED")
            self.proposal_calls += 1
            return proposal_outcome(f"prop-{self.proposal_calls}")

        self.proposal_calls = 0
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
            "send_telegram": ("services.notifications.send_telegram", {}),
            "send_critical_alert": ("services.notifications.send_critical_alert", {}),
            "save_decision": ("agent.repositories.decisions_repo.save_decision", {}),
            # active-режим при успешных подъёмах шлёт сообщение с кнопками 👍/👎 через
            # telegram_bot.send_with_buttons — без явного мока полез бы в реальную сеть
            # (pytest-socket блокирует, код перехватывает исключение и падает в
            # send_telegram fallback — тест «случайно» проходил бы, но неявно).
            "send_with_buttons": ("services.telegram_bot.send_with_buttons", {"return_value": True}),
        }
        if patch_producer:
            # Патчим ИМЕННО propose_scale: боевой код импортирует это имя
            # локально, поэтому подмена legacy-алиаса execute_scale ничего не
            # перехватывает (мок молчит, прогон уходит в реального producer'а).
            self._specs["propose_scale"] = (
                "services.action_producer_gateway.propose_scale",
                {"side_effect": _propose_scale_side_effect},
            )
        if plan_data is not None:
            self._specs.update({
                "read_general_plan": ("services.plan_reader.read_general_plan", {"return_value": plan_data}),
                "get_usd_to_lcy": ("services.exchange_rate.get_usd_to_lcy", {"return_value": 100.0}),
                "get_fb_week_spend": ("services.budget_scaler.get_fb_week_spend", {"return_value": fb_week_spend}),
                "get_google_week_spend": ("services.budget_scaler.get_google_week_spend", {"return_value": 0.0}),
                "get_amo_week_revenue": ("services.budget_scaler.get_amo_week_revenue", {"return_value": revenue_lcy}),
            })
        self.mocks: dict[str, MagicMock] = {}

    def __enter__(self) -> "_ScalingMocks":
        for name, (target, kwargs) in self._specs.items():
            self.mocks[name] = self._stack.enter_context(patch(target, **kwargs))
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._stack.__exit__(exc_type, exc, tb)

    def __getitem__(self, name: str) -> MagicMock:
        return self.mocks[name]


def _frozen_now(now: datetime):
    """Патчит services.budget_scaler.datetime так, чтобы .now() возвращал фиксированный now,
    а конструктор datetime(...) продолжал работать как обычно (нужен коду внутри модуля).
    """
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = now
    return patch("services.budget_scaler.datetime", mock_dt)


# ===========================================================================
# Сценарий 1: кап 15%/сутки через несколько прогонов
# ===========================================================================

def test_cap_15_pct_across_multiple_runs_same_day():
    """Прогон1 предлагает +10% -> прогон2 может ещё максимум +5% -> прогон3 ничего.

    Дневной кап расходуется при ИСПОЛНЕНИИ одобренного подъёма, а не при
    создании предложения (producer к капу не прикасается). Поэтому между
    прогонами исполнение одобренного владельцем подъёма эмулируется явным
    budget_daily_cap.record_raise — ровно то, что делает execution boundary
    после нажатия «одобряю». Инвариант тот же, что и до approval-first:
    суммарно за сутки адсет не должен вырасти больше чем на 15%.
    """
    import services.budget_daily_cap as cap_module
    from services.budget_scaler import run_budget_scaling

    local_ads = [_make_local_ad("ad1")]
    now = datetime(2026, 7, 2, 10, 0, tzinfo=_TZ)

    # --- Прогон 1: max_budget_increase_pct=10 -> имитирует, что сегодня уже подняли +10%
    # (сценарий из спеки: "прогон1 поднял +10%"; конкретный % первого прогона не привязан
    # к дефолту 15 — задаём его конфигом, чтобы получить контролируемое промежуточное состояние).
    cfg_run1 = _base_cfg(scale_enabled=True, max_budget_increase_pct=10)
    all_budgets_run1 = {"adset1": _adset_info(100.0)}
    with _ScalingMocks(local_ads, all_budgets_run1, cfg_run1) as m1, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(now):
        result1 = run_budget_scaling(mode="active")

    assert result1["proposals"] == ["prop-1"]
    assert result1["scaled"] == []  # до одобрения ничего не поднято
    rec1 = m1["propose_scale"].call_args.args[0]
    assert rec1["new_budget_usd"] == pytest.approx(110.0)  # 100 + 10%
    # Владелец одобрил → execution boundary применил подъём и списал кап.
    cap_module.record_raise("adset1", 100.0, 110.0, now=now)

    # --- Прогон 2 в тот же день: теперь кап 15%, но уже израсходовано 10% -> остаток 5%.
    cfg_run2 = _base_cfg(scale_enabled=True, max_budget_increase_pct=15)
    all_budgets_run2 = {"adset1": _adset_info(110.0)}  # текущий бюджет после прогона1
    with _ScalingMocks(local_ads, all_budgets_run2, cfg_run2) as m2, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(now):
        result2 = run_budget_scaling(mode="active")

    assert result2["proposals"] == ["prop-1"]
    rec2 = m2["propose_scale"].call_args.args[0]
    # remaining=5% от start=$100 => +$5 => 110+5=115
    assert rec2["new_budget_usd"] == pytest.approx(115.0)
    cap_module.record_raise("adset1", 110.0, 115.0, now=now)

    # --- Прогон 3 в тот же день: лимит 15% исчерпан полностью -> кандидат скипается.
    cfg_run3 = _base_cfg(scale_enabled=True, max_budget_increase_pct=15)
    all_budgets_run3 = {"adset1": _adset_info(115.0)}
    with _ScalingMocks(local_ads, all_budgets_run3, cfg_run3) as m3, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(now):
        result3 = run_budget_scaling(mode="active")

    # Кап исчерпан → даже предложение не создаётся (нечего одобрять).
    # `.get`, а не `[...]`: на ветке «рекомендаций нет» прогон возвращает ранний
    # словарь без ключа "proposals" — сильная проверка здесь именно в том, что
    # producer не вызван ни разу.
    m3["propose_scale"].assert_not_called()
    assert not result3.get("proposals")
    assert result3["scaled"] == []
    assert result3["recommendations"] == []


# ===========================================================================
# Сценарий 2: кандидат без оплат — не в кандидатах, set_adset_budget не вызван
# ===========================================================================

def test_candidate_without_payments_excluded():
    """payments=0 (сверено) и payments=None (нет сверки) — оба не проходят отбор."""
    from services.budget_scaler import run_budget_scaling

    local_ads = [
        _make_local_ad("ad_zero", payments=0, outcomes_matched_at=None),
        _make_local_ad("ad_none", payments=None),
    ]
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=True)

    with _ScalingMocks(local_ads, all_budgets, cfg) as m:
        result = run_budget_scaling(mode="dry_run")

    m["propose_scale"].assert_not_called()
    assert result["winners"] == []
    assert result["recommendations"] == []


# ===========================================================================
# Сценарий 3: гейт юнитки блокирует любые подъёмы
# ===========================================================================

def test_unit_economics_gate_blocks_raises():
    """actual_drr > unit_target -> ran=True, подъёмов 0, telegram с причиной."""
    from services.budget_scaler import run_budget_scaling

    local_ads = [_make_local_ad("ad1")]
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=True)

    # revenue маленькая при заметном расходе -> drr выйдет большим, а unit_target у _TIGHT_PLAN = 1%
    with _ScalingMocks(
        local_ads, all_budgets, cfg,
        plan_data=_TIGHT_PLAN, revenue_lcy=200_000.0, fb_week_spend=500.0,
    ) as m:
        result = run_budget_scaling(mode="active")

    m["propose_scale"].assert_not_called()
    assert result["ran"] is True
    assert result["scaled"] == []
    assert "plan_gate" in (result["skipped_reason"] or "")
    assert "юнитка" in (result["skipped_reason"] or "")
    m["send_telegram"].assert_called_once()


# ===========================================================================
# Сценарий 4: dry_run — рекомендации есть, set_adset_budget не вызван,
# decision DRY_RUN_RAISE, telegram «поднял бы»
# ===========================================================================

def test_dry_run_records_dry_run_raise_and_no_mutation():
    from services.budget_scaler import run_budget_scaling

    local_ads = [_make_local_ad("ad1")]
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=False)

    with _ScalingMocks(local_ads, all_budgets, cfg) as m:
        result = run_budget_scaling(mode="dry_run")

    m["propose_scale"].assert_not_called()
    assert len(result["recommendations"]) == 1
    assert result["scaled"] == []

    m["save_decision"].assert_called_once()
    assert m["save_decision"].call_args.args[3] == "DRY_RUN_RAISE"

    m["send_telegram"].assert_called_once()
    telegram_text = m["send_telegram"].call_args.args[0]
    assert "поднял бы" in telegram_text.lower()


# ===========================================================================
# Сценарий 5: переход дня CityA сбрасывает дневной счёт
# ===========================================================================

def test_day_rollover_resets_daily_cap():
    """Вчера исчерпали 15% -> сегодня снова доступны +15% от НОВОГО start (текущего бюджета).

    Approval-first: «доступны» проверяется по intent предложения (какой бюджет
    ушёл владельцу на одобрение), т.к. сам подъём в прогоне не выполняется.
    """
    import services.budget_daily_cap as cap_module
    from services.budget_scaler import run_budget_scaling

    yesterday = datetime(2026, 7, 1, 10, 0, tzinfo=_TZ)
    today = datetime(2026, 7, 2, 10, 0, tzinfo=_TZ)

    # Вчера: адсет $100 -> исчерпал 15% (raised_pct=15)
    cap_module.get_day_start_budget("adset1", 100.0, now=yesterday)
    cap_module.record_raise("adset1", 100.0, 115.0, now=yesterday)
    remaining_yesterday = cap_module.get_remaining_daily_pct("adset1", 115.0, 15.0, now=yesterday)
    assert remaining_yesterday == 0.0

    # Сегодня: текущий бюджет $115 (результат вчерашнего подъёма) -> rollover, remaining=15% заново
    local_ads = [_make_local_ad("ad1")]
    all_budgets = {"adset1": _adset_info(115.0)}
    cfg = _base_cfg(scale_enabled=True)

    with _ScalingMocks(local_ads, all_budgets, cfg) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(today):
        result = run_budget_scaling(mode="active")

    assert result["proposals"] == ["prop-1"]
    assert result["scaled"] == []
    # Новый старт дня = $115 (текущий бюджет на момент первого обращения сегодня)
    recommendation = m["propose_scale"].call_args.args[0]
    assert recommendation["new_budget_usd"] == pytest.approx(115.0 * 1.15, rel=1e-3)

    # Убедимся через сам модуль капа, что start_budget сегодня зафиксирован как $115
    start_today = cap_module.get_day_start_budget("adset1", 115.0, now=today)
    assert start_today == pytest.approx(115.0)


# ===========================================================================
# Сценарий 6a: confirmed_waster внутри адсета исключает адсет целиком
# ===========================================================================

def test_confirmed_waster_excludes_whole_adset():
    """Адсет с ad-победителем и ad-confirmed_waster внутри -> адсет исключён целиком.

    Регрессионный тест на баг: _fetch_candidate_fb_info раньше запрашивался только
    по winner_ad_ids (кандидаты с payments>0), поэтому ad_waste (payments=0) не
    получал adset_id и не попадал в групповую защиту confirmed_waster — адсет
    ложно проходил бы проверку и получал подъём («долив в слив»). Явно проверяем,
    что ad_waste (не имеющий оплат) тоже передан в вызов _fetch_candidate_fb_info —
    это гарантия того, что групповая защита реально видит слив внутри адсета.
    """
    from services.budget_scaler import run_budget_scaling

    local_ads = [
        _make_local_ad("ad_win", payments=3, days_running=10),
        _make_local_ad("ad_waste", payments=0, outcomes_matched_at="2026-07-01T10:00:00", days_running=10),
    ]
    fb_ad_info = {
        "ad_win": {"adset_id": "adset1", "effective_status": "ACTIVE"},
        "ad_waste": {"adset_id": "adset1", "effective_status": "ACTIVE"},
    }
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=True)

    with _ScalingMocks(local_ads, all_budgets, cfg, fb_ad_info=fb_ad_info) as m:
        result = run_budget_scaling(mode="active")

    # Ключевая проверка прод-поведения: _fetch_candidate_fb_info должен быть вызван
    # СО ВСЕМИ ad_id (включая waster с payments=0), а не только с кандидатами по
    # продажам — иначе слив останется невидимым для групповой защиты.
    m["fetch_candidate_fb_info"].assert_called_once()
    called_ad_ids = m["fetch_candidate_fb_info"].call_args.args[0]
    assert set(called_ad_ids) == {"ad_win", "ad_waste"}

    m["propose_scale"].assert_not_called()
    assert result["recommendations"] == []
    assert result["scaled"] == []


# ===========================================================================
# Сценарий 6a-bis: порог значимости слива waster_min_spend_usd (компромисс)
# Строгое all-or-nothing вето сохранено, но «значимым» слив считается только при
# расходе ≥ порога — молодая околонулевая нулёвка адсет НЕ рубит.
# ===========================================================================

def test_micro_waster_below_threshold_does_not_veto():
    """Слив с расходом $5 (< порога $10), оплат 0, сверено → адсет НЕ исключён,
    подъём проходит. Это фикс бага «микро-нулёвка рубит адсет целиком»:
    молодое/почти не открутившееся объявление не должно блокировать победителя.
    """
    from services.budget_scaler import run_budget_scaling

    local_ads = [
        _make_local_ad("ad_win", payments=3, spend=80.0, days_running=10),
        # Расход $5 < дефолтного порога $10 → НЕ значимый слив, вето не срабатывает.
        _make_local_ad(
            "ad_micro_waste", payments=0, spend=5.0,
            outcomes_matched_at="2026-07-01T10:00:00", days_running=10,
        ),
    ]
    fb_ad_info = {
        "ad_win": {"adset_id": "adset1", "effective_status": "ACTIVE"},
        "ad_micro_waste": {"adset_id": "adset1", "effective_status": "ACTIVE"},
    }
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=True, max_budget_increase_pct=15)

    with _ScalingMocks(local_ads, all_budgets, cfg, fb_ad_info=fb_ad_info) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"):
        result = run_budget_scaling(mode="active")

    # Адсет НЕ вырезан порогом → подъём дошёл до предложения владельцу.
    m["propose_scale"].assert_called_once()
    recommendation = m["propose_scale"].call_args.args[0]
    assert recommendation["adset_id"] == "adset1"
    assert recommendation["new_budget_usd"] == pytest.approx(115.0)
    assert result["proposals"] == ["prop-1"]
    assert result["scaled"] == []


def test_significant_waster_above_threshold_still_vetoes():
    """Слив с расходом ровно $10 (== порог), оплат 0, сверено → адсет исключён.
    Граничный случай: spend >= порога включает вето (>=, не строгое >)."""
    from services.budget_scaler import run_budget_scaling

    local_ads = [
        _make_local_ad("ad_win", payments=3, spend=80.0, days_running=10),
        _make_local_ad(
            "ad_waste", payments=0, spend=10.0,
            outcomes_matched_at="2026-07-01T10:00:00", days_running=10,
        ),
    ]
    fb_ad_info = {
        "ad_win": {"adset_id": "adset1", "effective_status": "ACTIVE"},
        "ad_waste": {"adset_id": "adset1", "effective_status": "ACTIVE"},
    }
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=True)

    with _ScalingMocks(local_ads, all_budgets, cfg, fb_ad_info=fb_ad_info) as m:
        result = run_budget_scaling(mode="active")

    m["propose_scale"].assert_not_called()
    assert result["recommendations"] == []
    assert result["scaled"] == []


@pytest.mark.parametrize("payments_source", ["amo", "shadow", "erp"])
def test_confirmed_waster_excludes_adset_all_payment_sources(payments_source):
    """1 победитель + 1 значимый слив (spend $80) в адсете → адсет исключён,
    set_adset_budget НЕ вызван — во ВСЕХ режимах payments_source (amo/shadow/erp).

    cdp.enabled=False, поэтому CDP-движок не дёргается (сеть не нужна), но
    payments_source читается и определяет семантику effective payments.
    """
    from services.budget_scaler import run_budget_scaling

    local_ads = [
        _make_local_ad("ad_win", payments=3, spend=80.0, days_running=10),
        _make_local_ad(
            "ad_waste", payments=0, spend=80.0,
            outcomes_matched_at="2026-07-01T10:00:00", days_running=10,
        ),
    ]
    fb_ad_info = {
        "ad_win": {"adset_id": "adset1", "effective_status": "ACTIVE"},
        "ad_waste": {"adset_id": "adset1", "effective_status": "ACTIVE"},
    }
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(
        scale_enabled=True,
        cdp={"enabled": False, "payments_source": payments_source},
    )

    with _ScalingMocks(local_ads, all_budgets, cfg, fb_ad_info=fb_ad_info) as m:
        result = run_budget_scaling(mode="active")

    m["propose_scale"].assert_not_called()
    assert result["recommendations"] == []
    assert result["scaled"] == []


def test_none_payments_not_confirmed_waster():
    """payments is None (нет данных, даже при сверке) НЕ считается confirmed zero →
    адсет НЕ вето (сосед с payments=None не блокирует победителя), предложение
    подъёма создаётся.
    """
    from services.budget_scaler import run_budget_scaling

    local_ads = [
        _make_local_ad("ad_win", payments=3, spend=80.0, days_running=10),
        # payments=None + сверка прошла — но None != 0, это НЕ подтверждённый слив.
        _make_local_ad(
            "ad_none", payments=None, spend=80.0,
            outcomes_matched_at="2026-07-01T10:00:00", days_running=10,
        ),
    ]
    fb_ad_info = {
        "ad_win": {"adset_id": "adset1", "effective_status": "ACTIVE"},
        "ad_none": {"adset_id": "adset1", "effective_status": "ACTIVE"},
    }
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=True, max_budget_increase_pct=15)

    with _ScalingMocks(local_ads, all_budgets, cfg, fb_ad_info=fb_ad_info) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"):
        result = run_budget_scaling(mode="active")

    m["propose_scale"].assert_called_once()
    assert result["proposals"] == ["prop-1"]
    assert result["scaled"] == []


def test_adset_no_waster_valid_payments_is_candidate():
    """Адсет без единого слива, с двумя объявлениями с валидными оплатами →
    остаётся кандидатом, предложение подъёма создаётся (соседний победитель не мешает)."""
    from services.budget_scaler import run_budget_scaling

    local_ads = [
        _make_local_ad("ad_win1", payments=3, spend=80.0, days_running=10),
        _make_local_ad("ad_win2", payments=2, spend=50.0, days_running=10),
    ]
    fb_ad_info = {
        "ad_win1": {"adset_id": "adset1", "effective_status": "ACTIVE"},
        "ad_win2": {"adset_id": "adset1", "effective_status": "ACTIVE"},
    }
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=True, max_budget_increase_pct=15)

    with _ScalingMocks(local_ads, all_budgets, cfg, fb_ad_info=fb_ad_info) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"):
        result = run_budget_scaling(mode="active")

    m["propose_scale"].assert_called_once()
    recommendation = m["propose_scale"].call_args.args[0]
    assert recommendation["adset_id"] == "adset1"
    assert result["proposals"] == ["prop-1"]
    assert result["scaled"] == []


# ===========================================================================
# Сценарий 6b: все объявления адсета в learning-фазе (days_running<3) — адсет исключён
# ===========================================================================

def test_learning_phase_excludes_adset():
    """Единственное объявление адсета days_running=1 (свежезапущенное) -> адсет исключён."""
    from services.budget_scaler import run_budget_scaling

    local_ads = [_make_local_ad("ad1", payments=5, days_running=1)]
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=True)

    with _ScalingMocks(local_ads, all_budgets, cfg) as m:
        result = run_budget_scaling(mode="active")

    # _fetch_candidate_fb_info вызван по всем local_ads (единственное объявление ad1)
    m["fetch_candidate_fb_info"].assert_called_once()
    called_ad_ids = m["fetch_candidate_fb_info"].call_args.args[0]
    assert set(called_ad_ids) == {"ad1"}

    m["propose_scale"].assert_not_called()
    assert result["recommendations"] == []
    assert result["scaled"] == []


def test_learning_phase_excludes_adset_with_non_candidate_ad():
    """Адсет с ad-победителем (payments>0, days_running=1, свежий) и вторым НЕ-кандидатом
    того же адсета (payments=0, days_running=1, тоже свежий) -> адсет исключён, т.к.
    §10.2 требует, чтобы ХОТЯ БЫ ОДНО объявление адсета прошло learning-фазу
    (days_running>=3), а тут ни одно не прошло.

    Регрессионный тест: раньше _fetch_candidate_fb_info запрашивался только по
    winner_ad_ids (кандидатам с payments>0), поэтому non-candidate объявление
    ("ad_fresh_other", payments=0) не получало adset_id и не участвовало бы в
    проверке learning-гейта на уровне адсета — ослабляло защиту (критерий №7
    спеки). В данном тесте это не меняет исход (кандидат сам свежий), но проверка
    аргументов вызова гарантирует, что _fetch_candidate_fb_info видит ОБА объявления.
    """
    from services.budget_scaler import run_budget_scaling

    local_ads = [
        _make_local_ad("ad_win", payments=3, days_running=1),
        _make_local_ad("ad_fresh_other", payments=0, days_running=1),
    ]
    fb_ad_info = {
        "ad_win": {"adset_id": "adset1", "effective_status": "ACTIVE"},
        "ad_fresh_other": {"adset_id": "adset1", "effective_status": "ACTIVE"},
    }
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=True)

    with _ScalingMocks(local_ads, all_budgets, cfg, fb_ad_info=fb_ad_info) as m:
        result = run_budget_scaling(mode="active")

    m["fetch_candidate_fb_info"].assert_called_once()
    called_ad_ids = m["fetch_candidate_fb_info"].call_args.args[0]
    assert set(called_ad_ids) == {"ad_win", "ad_fresh_other"}

    m["propose_scale"].assert_not_called()
    assert result["recommendations"] == []
    assert result["scaled"] == []


# ===========================================================================
# Сценарий 7: active-режим — подъём уходит владельцу как SCALE-proposal,
# FB не тронут, дневной кап не израсходован до одобрения
# ===========================================================================

def test_active_raise_creates_owner_proposal_without_touching_fb(monkeypatch, tmp_path):
    """active-прогон создаёт предложение владельцу вместо подъёма бюджета.

    Здесь работает НАСТОЯЩИЙ propose_scale (recorder подменяет только запись
    proposal в БД), поэтому проверяется реальная форма предложения и обе
    половины инварианта approval-first:
    (а) ни один FB-мутатор и ни одна точка исполнения не вызваны;
    (б) создан корректный SCALE-proposal с точным intent $100 → $115.

    Раньше этот тест требовал decision BUDGET_RAISED, записи кулдауна и
    израсходованного дневного капа — всё это следствия ВЫПОЛНЕННОГО подъёма,
    которого в прогоне больше нет. Кап не должен расходоваться на действие,
    которое владелец ещё не одобрил, иначе неодобренное предложение съедало бы
    суточный лимит реальных подъёмов.
    """
    from services.budget_scaler import run_budget_scaling
    import services.budget_daily_cap as cap_module

    local_ads = [_make_local_ad("ad1")]
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=True, max_budget_increase_pct=15)

    with _ScalingMocks(local_ads, all_budgets, cfg, patch_producer=False) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"):
        recorded = install_proposal_recorder(monkeypatch, tmp_path)
        result = run_budget_scaling(mode="active")

    recorded.assert_no_direct_provider_mutation()
    plan = recorded.assert_proposed(
        "adset1",
        kind=ProposalKind.SCALE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="SET_ADSET_BUDGET",
    )
    payload = plan.targets[0].intended_payload
    assert float(payload["expected_current_budget_usd"]) == pytest.approx(100.0)
    assert float(payload["target_budget_usd"]) == pytest.approx(115.0)

    assert len(result["proposals"]) == 1
    assert result["scaled"] == []

    # Никакого «поднял» в журнале решений — подъёма ещё не было.
    raised_calls = [
        c for c in m["save_decision"].call_args_list
        if len(c.args) > 3 and c.args[3] == "BUDGET_RAISED"
    ]
    assert raised_calls == []

    # Владелец получил ровно одно сообщение о том, что предложения отправлены.
    m["send_telegram"].assert_called_once()
    telegram_text = m["send_telegram"].call_args.args[0]
    assert "предложения отправлены владельцу" in telegram_text
    assert "Количество: 1" in telegram_text

    # Дневной кап НЕ израсходован предложением: +15% всё ещё доступны до одобрения.
    remaining = cap_module.get_remaining_daily_pct("adset1", 100.0, 15.0)
    assert remaining == pytest.approx(15.0)


# ===========================================================================
# Сценарий 8: кулдаун предложений — два подряд прогона крона не спамят владельца
# ===========================================================================

def test_two_consecutive_active_runs_create_exactly_one_proposal():
    """Второй прогон крона в окне кулдауна молчит: предложение ровно одно.

    Регрессия: `_record_scaled_at` перестал вызываться (подъёма в прогоне больше
    нет), `last_scaled_at` оставался пустым, ГЕЙТ 4 (4ч) был мёртвым — и крон
    заново предлагал одно и то же на каждом тике. Кулдаун в approval-first
    считается от момента ПРОСЬБЫ: предложение отправлено → 4 часа тишины.

    Кулдаун здесь НЕ замокан (state-файл в tmp_path через autouse-фикстуру) —
    проверяется настоящая связка _record_scaled_at → _is_in_cooldown.
    """
    from services.budget_scaler import run_budget_scaling

    local_ads = [_make_local_ad("ad1")]
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=True, max_budget_increase_pct=15)
    now = datetime(2026, 7, 2, 10, 0, tzinfo=_TZ)

    with _ScalingMocks(local_ads, all_budgets, cfg) as m1, _frozen_now(now):
        first = run_budget_scaling(mode="active")

    assert first["proposals"] == ["prop-1"]
    assert m1["propose_scale"].call_count == 1

    # Тот же адсет, тот же день, +30 минут — обычный следующий тик крона.
    with _ScalingMocks(local_ads, all_budgets, cfg) as m2, \
         _frozen_now(now + timedelta(minutes=30)):
        second = run_budget_scaling(mode="active")

    m2["propose_scale"].assert_not_called()
    assert second["ran"] is False
    assert "cooldown" in (second["skipped_reason"] or "")
    assert second["proposals"] == []
    # Тишина: ни одного нового сообщения владельцу во втором прогоне.
    m2["send_telegram"].assert_not_called()


def test_cooldown_expires_after_window_and_allows_next_proposal():
    """Через 4+ часа кулдаун отпускает, и крон снова может предложить подъём."""
    from services.budget_scaler import run_budget_scaling

    local_ads = [_make_local_ad("ad1")]
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=True, max_budget_increase_pct=15)
    now = datetime(2026, 7, 2, 10, 0, tzinfo=_TZ)

    with _ScalingMocks(local_ads, all_budgets, cfg), _frozen_now(now):
        run_budget_scaling(mode="active")

    with _ScalingMocks(local_ads, all_budgets, cfg) as m2, \
         _frozen_now(now + timedelta(hours=5)):
        later = run_budget_scaling(mode="active")

    assert m2["propose_scale"].call_count == 1
    assert later["proposals"] == ["prop-1"]


def test_deduplicated_receipt_is_not_counted_as_new_proposal():
    """Квитанция «уже предложено» не создаёт вторую запись и не шумит в Telegram.

    Producer идемпотентен по scope: повтор того же intent возвращает квитанцию с
    deduplicated=True. Раньше флаг игнорировался — прогон рапортовал «отправлено
    предложений: 1», хотя владельцу ничего нового не ушло.
    """
    from services.budget_scaler import run_budget_scaling

    local_ads = [_make_local_ad("ad1")]
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg(scale_enabled=True, max_budget_increase_pct=15)

    with _ScalingMocks(local_ads, all_budgets, cfg) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at") as mock_record:
        m["propose_scale"].side_effect = None
        m["propose_scale"].return_value = proposal_outcome(
            "prop-existing", deduplicated=True
        )
        result = run_budget_scaling(mode="active")

    assert result["proposals"] == []
    assert result["scaled"] == []
    # Кулдаун не взводим: новой просьбы не было, окно тишины уже идёт от первой.
    mock_record.assert_not_called()
    m["send_telegram"].assert_not_called()


# ===========================================================================
# Сценарий 9: единая форма результата — ключ "proposals" есть у любого выхода
# ===========================================================================

@pytest.mark.parametrize(
    ("cfg_override", "expected_reason"),
    [
        ({"enabled": False}, "disabled"),
        ({"kill_switch": True}, "kill_switch"),
    ],
)
def test_early_gate_exits_carry_proposals_key(cfg_override, expected_reason):
    """Ранние гейты возвращают счётчик предложений, а не KeyError у вызывающего."""
    from services.budget_scaler import run_budget_scaling

    cfg = {**_base_cfg(scale_enabled=True), **cfg_override}
    with _ScalingMocks([_make_local_ad("ad1")], {"adset1": _adset_info(100.0)}, cfg):
        result = run_budget_scaling(mode="active")

    assert result["skipped_reason"] == expected_reason
    assert result["proposals"] == []


def test_no_winners_exit_carries_proposals_key():
    """Ветка «победителей нет» — тоже с ключом proposals (ран early-return)."""
    from services.budget_scaler import run_budget_scaling

    local_ads = [_make_local_ad("ad1", payments=0, outcomes_matched_at=None)]
    cfg = _base_cfg(scale_enabled=True)
    with _ScalingMocks(local_ads, {"adset1": _adset_info(100.0)}, cfg), \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)):
        result = run_budget_scaling(mode="active")

    assert result["winners"] == []
    assert result["proposals"] == []


def test_cooldown_exit_carries_proposals_key():
    """Ветка кулдауна — ключ proposals на месте (её читает web/app.py)."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True)
    with _ScalingMocks([_make_local_ad("ad1")], {"adset1": _adset_info(100.0)}, cfg), \
         patch(
             "services.budget_scaler._is_in_cooldown",
             return_value=(True, "cooldown: последнее повышение 1.0ч назад"),
         ):
        result = run_budget_scaling(mode="active")

    assert "cooldown" in (result["skipped_reason"] or "")
    assert result["proposals"] == []
