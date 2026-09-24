"""
Тесты редизайна Telegram-сообщений Budget Scaler (docs/specs/ARCH-scaler-report-redesign.md).

Фидбек владельца: из сообщения непонятно, чей ДРР (общий или адсета), на сколько
и кого поднимаем. Сообщение переехало с «ДО прогона» на «ПОСЛЕ прогона»
и склеивает решение + факты + сомнения в ОДНО честное сообщение о результате.

ВАЖНО (approval-first): скейлер больше НЕ поднимает бюджет сам —
он создаёт предложение владельцу, а мутация FB происходит в execution boundary
после одобрения в Telegram. Поэтому сообщения «📈 поднял N адсетов / Итого
добавлено» с кнопками 👍/👎 (send_with_buttons) удалены как ЛОЖНЫЕ: на момент
отправки ничего не поднято. Их место занял один честный отчёт о результате
прогона — «предложения отправлены владельцу, Количество: N» — к которому, как и
раньше, приклеены подпись ДРР и секция сомнений. Точные факты подъёма
($X→$Y на конкретный адсет) переехали из текста в intent предложения, поэтому
сценарии 1-3 проверяют их в payload proposal'а.

Покрывает (§9 спеки, сценарии 1-11):
1-3. Подъём с фактами: точный intent предложения ($X→$Y на нужный адсет),
     суммарный прирост по предложениям, деньги через fmt_money в отчёте, где
     суммы реально печатаются (репетиция/dry_run).
4.   Гейты разрешили, но реально ничего не подняли — честный текст без «📈».
5.   Гейт заблокировал подъём — текст с цифрами (план/факт/запас) + подпись ДРР.
6-6b. _fmt_drr_signature — подпись «чей ДРР» + окно + цель (в т.ч. n/a-кейс).
7.   _engine_distrust_reason — человеческие причины без аббревиатуры WAPE.
8.   _pluralize_adsets — русское склонение слова «адсет».
9.   Сомнения приклеены к ЕДИНСТВЕННОМУ сообщению о результате, а не уходят
     отдельным сообщением «есть сомнение» (старое поведение удалено).
10.  Анти-спам: триггеров нет -> секции «Почему сомневаюсь» нет.
11.  Флаг cdp.doubt_alerts=false -> триггеры подавлены даже когда сработали бы.

Файл САМОДОСТАТОЧНЫЙ: харнесс (_CascadeMocks, _make_local_ad, _adset_info,
_base_cfg, _VALID_PLAN, _frozen_now, автоюз-фикстуры изоляции state-файлов)
скопирован из tests/test_doubt_protocol.py (текущая версия в репо) — файлы
владеют раздельно, друг друга не импортируют. Единственное дополнение к
скопированному харнессу — параметр adset_map у _CascadeMocks (нужен для
сценария «2 адсета подряд»: оригинальный харнесс маппит все ad_id на один и
тот же фиктивный adset_id "adset1").

ЛОГИКА РЕШЕНИЙ (гейты/лимиты/кулдаун/выбор победителей) в этом файле НЕ
проверяется на изменение — за это отвечают test_budget_scaler.py,
test_budget_pilot_integration.py и test_doubt_protocol.py (не-блокирующий
инвариант result_on == result_off). Здесь проверяется только ТЕКСТ и МОМЕНТ
отправки сообщений.

Комментарии на русском.
"""

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импортов проекта (как в других тестах бюджет-пилота)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

import pytest

from services.budget_scaler import (
    SCALE_DEFAULTS,
    _engine_distrust_reason,
    _fmt_drr_signature,
    _pluralize_adsets,
)
from services.cdp_client import CdpError
from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import (
    install_proposal_recorder,
    install_provider_mutation_guard,
    proposal_outcome,
)


# ===========================================================================
# Автоюз-фикстуры изоляции state-файлов (копия из tests/test_doubt_protocol.py)
# ===========================================================================


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
    """Ни один отчётный сценарий не должен сопровождаться прямой мутацией FB.

    Отчёт «предложения отправлены» обязан быть правдой: если код снова начнёт
    сам менять бюджет, гард уронит тест."""
    return install_provider_mutation_guard(monkeypatch)


@pytest.fixture(autouse=True)
def isolated_doubt_log(tmp_path, monkeypatch):
    """Перенаправляет журнал сомнений (data/doubt_log.json) во временную папку —
    без этого прогон реально писал бы в продовый файл при каждом тесте."""
    import services.doubt_log as doubt_log_module
    log_file = tmp_path / "doubt_log.json"
    monkeypatch.setattr(doubt_log_module, "_DOUBT_LOG_FILE", log_file)


def _frozen_now(now: datetime):
    """Патчит services.budget_scaler.datetime так, чтобы .now() возвращал фиксированный now,
    а конструктор datetime(...) продолжал работать как обычно (нужен коду внутри модуля)."""
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = now
    return patch("services.budget_scaler.datetime", mock_dt)


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
    """Полный набор моков одного прогона run_budget_scaling. Скопировано из
    tests/test_doubt_protocol.py (владелец харнесса) + ДОПОЛНЕНИЕ: параметр
    adset_map — оригинальный харнесс жёстко маппит ВСЕ ad_id на один и тот же
    "adset1" (fb_ad_info строится как {ad_id: {"adset_id": "adset1", ...} for ad in local_ads}),
    что не позволяет проверить сценарий «подняли НЕСКОЛЬКО РАЗНЫХ адсетов за
    один прогон» (Тест 2 §9 спеки). adset_map даёт явную ad_id -> adset_id
    привязку для таких сценариев, не трогая поведение остальных тестов
    (по умолчанию adset_map=None -> старое поведение "все на adset1").
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
        adset_map: dict[str, str] | None = None,
        patch_producer: bool = True,
    ):
        if local_ads is None:
            local_ads = [_make_local_ad("ad1")]
        if all_budgets is None:
            all_budgets = {"adset1": _adset_info(100.0)}
        if cdp_daily_items is None:
            cdp_daily_items = _daily_items()
        if cdp_plan_fact is None:
            cdp_plan_fact = _plan_fact_summary(time_pct=0.1, plan_rev=1_000_000.0, fact_rev=200_000.0)

        if adset_map is not None:
            fb_ad_info = {
                ad["ad_id"]: {"adset_id": adset_map[ad["ad_id"]], "effective_status": "ACTIVE"}
                for ad in local_ads if ad["ad_id"] in adset_map
            }
        else:
            fb_ad_info = {ad["ad_id"]: {"adset_id": "adset1", "effective_status": "ACTIVE"} for ad in local_ads}

        def _fetch_candidate_fb_info_side_effect(ad_ids: list[str]) -> dict:
            return {aid: info for aid, info in fb_ad_info.items() if aid in ad_ids}

        # Квитанция предложения (НЕ результат исполнения): confirmed=False,
        # мутации FB не было. Разные id по вызовам — чтобы тест видел,
        # сколько предложений реально ушло владельцу.
        def _propose_scale_side_effect(recommendation: dict, **kwargs):
            del recommendation, kwargs
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
            "send_telegram": (
                "services.notifications.send_telegram",
                send_telegram_kwargs if send_telegram_kwargs is not None else {"return_value": True},
            ),
            "send_critical_alert": ("services.notifications.send_critical_alert", {}),
            "save_decision": ("agent.repositories.decisions_repo.save_decision", {}),
            "send_with_buttons": ("services.telegram_bot.send_with_buttons", {"return_value": True}),
        }
        if patch_producer:
            # Патчим propose_scale — именно это имя импортирует боевой код;
            # legacy-алиас execute_scale подменять бессмысленно (не перехватит).
            self._specs["propose_scale"] = (
                "services.action_producer_gateway.propose_scale",
                {"side_effect": _propose_scale_side_effect},
            )

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


# ===========================================================================
# Тесты 6, 6b — _fmt_drr_signature (юнит, из §9 «Детали ключевых новых тестов»)
# ===========================================================================


def test_drr_signature_full():
    """Значение + «общий по кабинету (все города)» + окно trailing-7дн + цель."""
    tz = timezone(timedelta(hours=5))
    sat = datetime(2026, 7, 5, 23, 59, 59, tzinfo=tz)  # суббота — конец окна
    assert _fmt_drr_signature(0.075, sat, 0.08) == (
        "ДРР 7.5% — общий по кабинету (все города), окно 29.06–05.07, цель ≤8.0%"
    )


def test_drr_signature_na():
    """ДРР неизвестен и окно неизвестно -> «n/a» / «—», без падения (§8 edge cases)."""
    assert _fmt_drr_signature(None, None, 0.08) == (
        "ДРР n/a — общий по кабинету (все города), окно —, цель ≤8.0%"
    )


def test_drr_signature_google_incomplete_note():
    """Если в окне ДРР есть missing-дни Google → пометка «Google неполный за N дн»
    При 0 — пометки нет (обратная совместимость)."""
    tz = timezone(timedelta(hours=5))
    sat = datetime(2026, 7, 5, 23, 59, 59, tzinfo=tz)
    # Нет missing-дней → подпись без хвоста (как раньше)
    assert "Google неполный" not in _fmt_drr_signature(0.075, sat, 0.08, 0)
    # Есть missing-дни → честная пометка в хвосте
    assert _fmt_drr_signature(0.075, sat, 0.08, 3) == (
        "ДРР 7.5% — общий по кабинету (все города), окно 29.06–05.07, цель ≤8.0% "
        "· Google неполный за 3 дн"
    )


# ===========================================================================
# Тест 7 — _engine_distrust_reason: человеческий текст, без WAPE (из §9)
# ===========================================================================


def test_engine_distrust_reason_human_no_wape():
    """Причина недоверия прогнозу — человеческая фраза, без аббревиатуры WAPE."""
    ctx = {
        "is_cold_start": False,
        "data_as_of": "2026-07-08T09:30:00Z",
        "engine_accuracy": [
            {"metric": "revenue_new", "target_month": "2026-06", "wape": 0.31},
        ],
    }
    now = datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc)
    reason = _engine_distrust_reason(ctx, now)
    assert reason == "прогноз CDP по выручке в этом месяце ошибается на 31% (допустимо 25%)"
    assert "WAPE" not in reason
    assert "движок" not in reason.lower()


# ===========================================================================
# Тест 8 — _pluralize_adsets: русское склонение
# ===========================================================================


def test_pluralize_adsets_forms():
    assert _pluralize_adsets(1) == "адсет"
    assert _pluralize_adsets(2) == "адсета"
    assert _pluralize_adsets(5) == "адсетов"
    assert _pluralize_adsets(11) == "адсетов"
    assert _pluralize_adsets(21) == "адсет"


# ===========================================================================
# Тесты 1-3 — подъём ушёл предложением: факты в intent, сумма приростов,
# деньги через fmt_money
# ===========================================================================


def test_raise_single_adset_with_facts(monkeypatch, tmp_path):
    """Тест 1 §9 (после approval-first): active, scale_enabled, 1 адсет $100→$115.

    Отчёт «📈 поднял 1 адсет … Итого добавлено» удалён: на момент отправки
    подъёма не было, такой текст врал бы владельцу. Инвариант того же сценария:
    РОВНО одно честное сообщение о результате (без кнопок 👍/👎 «сделал сам»),
    подпись ДРР на месте, а сами факты $100→$115 лежат в intent предложения —
    их проверяем через настоящий propose_scale (recorder подменяет лишь запись
    в БД) и заодно доказываем, что прямой мутации FB не было.
    """
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True)
    with _CascadeMocks(cfg, patch_producer=False) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
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

    # Одно сообщение о результате, без кнопок «сделал сам»
    assert m["send_telegram"].call_count == 1
    assert not m["send_with_buttons"].called

    text = m["send_telegram"].call_args.args[0]
    assert "предложения отправлены владельцу" in text
    assert "Количество: 1" in text
    # Подпись ДРР («чей ДРР» + окно + цель) присутствует во всех сообщениях (§9 тест 6).
    assert "ДРР" in text
    assert "общий по кабинету (все города)" in text
    # Никаких заявлений о выполненном подъёме — ещё ничего не поднято.
    assert "поднял" not in text
    assert "Итого добавлено" not in text


def test_raise_total_delta_sum_two_adsets(monkeypatch, tmp_path):
    """Тест 2 §9: два РАЗНЫХ адсета за один прогон → суммарный прирост $15 + $12 = $27.

    «Итого добавлено» из текста ушло вместе с ложным отчётом о подъёме, поэтому
    суммарный прирост считаем по intent'ам предложений: на владельца уходят ДВА
    отдельных предложения ($100→$115 и $80→$92), а отчёт честно называет их
    количество."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True, max_scales_per_run=5)
    local_ads = [
        _make_local_ad(ad_id="ad1", ad_name="CityA КО1"),
        _make_local_ad(ad_id="ad2", ad_name="CityB RO1"),
    ]
    all_budgets = {
        "adset1": _adset_info(100.0, name="CityA / PRODA L2"),
        "adset2": _adset_info(80.0, name="CityB / PRODB L1"),
    }
    with _CascadeMocks(
        cfg, local_ads=local_ads, all_budgets=all_budgets,
        adset_map={"ad1": "adset1", "ad2": "adset2"}, patch_producer=False,
    ) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        recorded = install_proposal_recorder(monkeypatch, tmp_path)
        result = run_budget_scaling(mode="active")

    recorded.assert_no_direct_provider_mutation()
    assert recorded.subject_ids == ["adset1", "adset2"]
    targets = {
        target.subject_id: target.intended_payload
        for plan in recorded.plans for target in plan.targets
    }
    assert float(targets["adset1"]["target_budget_usd"]) == pytest.approx(115.0)
    assert float(targets["adset2"]["target_budget_usd"]) == pytest.approx(92.0)
    total_delta = sum(
        float(payload["target_budget_usd"]) - float(payload["expected_current_budget_usd"])
        for payload in targets.values()
    )
    assert total_delta == pytest.approx(27.0)  # $15 + $12, а не один из них

    assert len(result["proposals"]) == 2
    text = m["send_telegram"].call_args.args[0]
    assert "Количество: 2" in text


def test_raise_money_formatted_via_fmt_money(monkeypatch, tmp_path):
    """Тест 3 §9: суммы >= 1000 идут через fmt_money с точкой-разделителем
    тысяч ($2.000 -> $2.300), а НЕ голым Python-форматированием ($2000).

    Единственный отчёт, который сейчас печатает суммы владельцу — репетиция
    (dry_run), поэтому формат денег проверяем на ней. Тот же адсет в active
    доказывает вторую половину: точная сумма $2.300 доехала в intent
    предложения, а FB при этом не тронут.
    """
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True, max_adset_daily_budget=5000, max_total_daily_budget=10000)
    all_budgets = {"adset1": _adset_info(2000.0)}
    with _CascadeMocks(cfg, all_budgets=all_budgets) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        dry_result = run_budget_scaling(mode="dry_run")

    assert len(dry_result["recommendations"]) == 1
    text = m["send_telegram"].call_args.args[0]
    assert "$2.000 → $2.300" in text
    assert "$2000" not in text  # старый формат без разделителя не должен просочиться

    with _CascadeMocks(cfg, all_budgets=all_budgets, patch_producer=False), \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        recorded = install_proposal_recorder(monkeypatch, tmp_path)
        active_result = run_budget_scaling(mode="active")

    recorded.assert_no_direct_provider_mutation()
    plan = recorded.assert_proposed(
        "adset1",
        kind=ProposalKind.SCALE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="SET_ADSET_BUDGET",
    )
    assert float(plan.targets[0].intended_payload["target_budget_usd"]) == pytest.approx(2300.0)
    assert len(active_result["proposals"]) == 1
    assert active_result["scaled"] == []


# ===========================================================================
# Тест 4 — гейты разрешили, но реально ничего не подняли (честный текст)
# ===========================================================================


def test_zero_winners_honest_message():
    """Тест 4 §9: гейты ок, но у всех объявлений payments=0 -> победителей нет ->
    честное сообщение «гейты разрешали подъём, но ... — ничего не поднял».
    НЕТ «📈»/«поднял бюджет» — раньше в этой ветке была тишина (дезинформация)."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True)
    local_ads = [_make_local_ad("ad1", payments=0)]
    with _CascadeMocks(cfg, local_ads=local_ads) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["scaled"] == []
    assert result["winners"] == []
    assert m["send_telegram"].call_count == 1
    assert not m["send_with_buttons"].called

    text = m["send_telegram"].call_args.args[0]
    assert "гейты разрешали подъём" in text
    assert "ничего не поднял" in text
    assert "📈" not in text
    assert "поднял бюджет" not in text
    # Подпись ДРР осталась даже в «ничего не поднял» — владелец видит контекст.
    assert "ДРР" in text


# ===========================================================================
# Тест 5 — гейт заблокировал подъём (текст с цифрами)
# ===========================================================================


def test_gate_block_with_numbers():
    """Тест 5 §9: drr > unit_target -> «❌ НЕ поднимаю: юнитка превышена: ...» +
    строка План/Факт/Запас цифрами + подпись ДРР — не тишина, не голая причина."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True)
    tight_plan = {**_VALID_PLAN, "unit_target": 0.01}
    with _CascadeMocks(
        cfg, sheet_plan=tight_plan, sheet_revenue_lcy=400_000.0, sheet_fb_week_spend=500.0,
    ) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert "plan_gate" in str(result["skipped_reason"])
    assert m["send_telegram"].call_count == 1
    assert not m["send_with_buttons"].called

    text = m["send_telegram"].call_args.args[0]
    assert "❌ НЕ поднимаю:" in text
    assert "юнитка превышена" in text
    assert "План" in text
    assert "Факт" in text
    assert "Запас" in text
    assert "ДРР" in text
    assert "общий по кабинету (все города)" in text


# ===========================================================================
# Тест 9 — сомнения приклеены к ЕДИНСТВЕННОМУ сообщению о результате
# ===========================================================================


def test_doubt_glued_to_single_result_message_not_separate():
    """Тест 9 §9: дивергенция источников ДРР > 3 п.п. (триггер (а) срабатывает
    в дефолтном сценарии подъёма) -> сомнения приклеены к ЕДИНСТВЕННОМУ
    сообщению о результате прогона. Отдельного сообщения «есть сомнение» нет.

    После approval-first единственное сообщение о результате — отчёт о
    отправленных предложениях (send_telegram), а не «поднял» с кнопками:
    сообщение с кнопками «сделал сам» удалено вместе с прямым подъёмом.
    Инвариант «одно сообщение, сомнения внутри него» сохранён."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True)
    with _CascadeMocks(cfg) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["drr_divergence_pp"] is not None
    assert result["drr_divergence_pp"] > SCALE_DEFAULTS["doubt_divergence_pp"]
    assert len(result["proposals"]) == 1
    assert m["send_telegram"].call_count == 1
    assert not m["send_with_buttons"].called

    text = m["send_telegram"].call_args.args[0]
    assert "предложения отправлены владельцу" in text
    assert "🤔 Почему сомневаюсь" in text
    assert "есть сомнение" not in text.lower()


# ===========================================================================
# Тест 10 — анти-спам: триггеров нет -> секции «Почему сомневаюсь» нет
# ===========================================================================


def test_antispam_no_doubt_section_when_no_triggers():
    """Тест 10 §9: fb-расход подобран так, что drr_sheet == drr_cdp (дивергенция=0),
    unit_target далёк от ДРР -> ни один из 6 триггеров не срабатывает -> в
    отправленном сообщении нет секции «Почему сомневаюсь» (но само сообщение о
    результате прогона отправлено — анти-спам не значит «тишина»)."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True)  # cdp.enabled=False -> триггеры (д)/(е)/(в) невозможны
    with _CascadeMocks(
        cfg, sheet_revenue_lcy=400_000.0, sheet_fb_week_spend=193.5,  # -> drr_sheet == drr_cdp
    ) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["drr_divergence_pp"] == pytest.approx(0.0, abs=0.05)
    all_texts = [
        (c.args[0] if c.args else c.kwargs.get("text", ""))
        for c in m["send_telegram"].call_args_list + m["send_with_buttons"].call_args_list
    ]
    assert all_texts  # сообщение о результате реально ушло
    assert not any("Почему сомневаюсь" in t for t in all_texts)


# ===========================================================================
# Тест 11 — флаг doubt_alerts=false подавляет сомнения даже когда сработали бы
# ===========================================================================


def test_doubt_alerts_flag_off_suppresses_section_even_with_triggers():
    """Тест 11 §9: та же конфигурация, что у test_raise_single_adset_with_facts
    (там дивергенция 4.8 п.п. > порога 3 -> триггер (а) точно срабатывает), но
    с cdp.doubt_alerts=false -> секции «Почему сомневаюсь» НЕТ ни в одном
    отправленном сообщении, хотя триггер по факту сработал бы."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": False, "doubt_alerts": False})
    with _CascadeMocks(cfg) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    # Доказательство, что триггер (а) реально сработал бы (та же дивергенция,
    # что в test_raise_single_adset_with_facts, где doubt_alerts=True по умолчанию).
    assert result["drr_divergence_pp"] is not None
    assert result["drr_divergence_pp"] > SCALE_DEFAULTS["doubt_divergence_pp"]

    all_texts = [
        (c.args[0] if c.args else c.kwargs.get("text", ""))
        for c in m["send_telegram"].call_args_list + m["send_with_buttons"].call_args_list
    ]
    assert all_texts
    assert not any("Почему сомневаюсь" in t for t in all_texts)
