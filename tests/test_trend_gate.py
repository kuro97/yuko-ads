"""
Тесты волны 3 недельных когорт — встраивание тренда в решения.

Главный инвариант, ради которого написан почти каждый тест ниже: ТРЕНД НЕ
МОЖЕТ ПОДНЯТЬ БЮДЖЕТ. Разрешены ровно два применения — вето на подъём и
дополнительный вес к уже обоснованной паузе (см. services/trend_gate.py).

Второй инвариант: дефолтный режим shadow не меняет НИ ОДНОГО исхода. Проверяем
это не только по флагам, но и сравнением полного результата прогона скейлера с
прогоном без тренда вообще.

Данные вердиктов строятся настоящим правилом (services/trend_policy.py), а не
подделываются: тест обязан ломаться, если правило перестанет считать эти недели
падением.
"""

import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импортов проекта (как в остальных тестах пакета)
import sys as _sys  # noqa: E402
if "google.genai" not in _sys.modules:
    _sys.modules["google.genai"] = MagicMock()
if "google" not in _sys.modules:
    _sys.modules["google"] = MagicMock()

from services.trend_gate import (  # noqa: E402
    SHADOW_SUFFIX,
    TrendContext,
    TrendMode,
    describe_verdict,
    evaluate_raise_veto,
    load_trend_context,
    resolve_mode,
)
from services.trend_policy import (  # noqa: E402
    TrendLevel,
    TrendStatus,
    WeekPoint,
    evaluate_trend,
)

# Понедельник. Возраст W-1 = 8 дней ≥ MIN_WEEK_AGE_DAYS.
_AS_OF = date(2026, 8, 3)
_W1 = date(2026, 7, 20)
_W2 = date(2026, 7, 13)
_W3 = date(2026, 7, 6)

# Квал 40% → 28% → 16% при 100 лидах в неделю: три недели в одну сторону,
# суммарная дельта 24 п.п. выше порога шума (13,8 п.п. на 80–159 лидах),
# интервалы Уилсона краёв не пересекаются → подтверждённое ПАДЕНИЕ.
_DECLINE_SERIES = ((_W3, 100, 40), (_W2, 100, 28), (_W1, 100, 16))
_GROWTH_SERIES = ((_W3, 100, 16), (_W2, 100, 28), (_W1, 100, 40))
# Меньше MIN_WEEK_LEADS — правило обязано сказать МАЛО ДАННЫХ.
_THIN_SERIES = ((_W2, 10, 1), (_W1, 10, 4))


@pytest.fixture(autouse=True)
def isolated_daily_cap_state(tmp_path, monkeypatch):
    """Изолирует state-файл дневного капа (прогоны скейлера ниже его трогают)."""
    import services.budget_daily_cap as cap_module

    monkeypatch.setattr(
        cap_module, "_CAP_STATE_FILE", tmp_path / "budget_daily_cap_state.json"
    )


def _point(week_start: date, leads: int, quals: int) -> WeekPoint:
    return WeekPoint(
        week_start=week_start,
        amo_leads=leads,
        quals=quals,
        comparable=True,
        spend_usd=100.0,
    )


def _verdict(series, *, level=TrendLevel.ADSET, entity_id="adset1"):
    """Настоящий вердикт правила по заданным неделям."""
    return evaluate_trend(
        [_point(*item) for item in series],
        level=level,
        entity_id=entity_id,
        as_of=_AS_OF,
    )


def _ctx(mode: TrendMode, *verdicts, error: str | None = None) -> TrendContext:
    return TrendContext(
        mode=mode,
        verdicts={verdict.entity_id: verdict for verdict in verdicts},
        error=error,
        as_of=_AS_OF,
    )


# ---------------------------------------------------------------------------
# Фикстуры вердиктов — сначала убеждаемся, что правило действительно так считает
# ---------------------------------------------------------------------------

def test_decline_series_is_action_grade_decline():
    """Опорные данные тестов — действительно подтверждённое падение адсета."""
    verdict = _verdict(_DECLINE_SERIES)
    assert verdict.status is TrendStatus.DECLINE
    assert verdict.action_grade is True


def test_growth_series_is_action_grade_growth():
    verdict = _verdict(_GROWTH_SERIES)
    assert verdict.status is TrendStatus.GROWTH
    assert verdict.action_grade is True


def test_thin_series_is_insufficient():
    verdict = _verdict(_THIN_SERIES)
    assert verdict.status is TrendStatus.INSUFFICIENT
    assert verdict.action_grade is False


def test_ad_level_decline_never_action_grade():
    """Уровень объявления — только диагностика: права действовать нет никогда."""
    verdict = _verdict(_DECLINE_SERIES, level=TrendLevel.AD)
    assert verdict.status is TrendStatus.DECLINE
    assert verdict.action_grade is False


# ---------------------------------------------------------------------------
# Режимы и разбор настройки
# ---------------------------------------------------------------------------

def test_default_mode_is_shadow():
    """Дефолт — тень: после деплоя тренд не меняет ничего."""
    assert resolve_mode({}) is TrendMode.SHADOW
    assert resolve_mode(None) is TrendMode.SHADOW


def test_mode_parsed_from_config():
    assert resolve_mode({"trend": {"mode": "active"}}) is TrendMode.ACTIVE
    assert resolve_mode({"trend": {"mode": "off"}}) is TrendMode.OFF
    assert resolve_mode({"trend": {"mode": "SHADOW"}}) is TrendMode.SHADOW


def test_unknown_mode_degrades_to_shadow():
    """Правленый руками settings.json не должен случайно включить боевой режим."""
    assert resolve_mode({"trend": {"mode": "ACTIVE!"}}) is TrendMode.SHADOW
    assert resolve_mode({"trend": {"mode": 1}}) is TrendMode.SHADOW
    assert resolve_mode({"trend": "active"}) is TrendMode.SHADOW


def test_autopilot_defaults_carry_shadow_mode():
    from services.autopilot import AUTOPILOT_DEFAULTS

    assert AUTOPILOT_DEFAULTS["trend"] == {"mode": "shadow"}


def test_autopilot_config_sanitizes_unknown_mode():
    """Неизвестный режим из settings.json приводится к shadow в runtime-конфиге."""
    from services.autopilot import get_autopilot_config

    with patch(
        "agent.scheduler.load_settings",
        return_value={"autopilot": {"trend": {"mode": "ВКЛЮЧИ"}}},
    ):
        cfg = get_autopilot_config()
    assert cfg["trend"]["mode"] == "shadow"


def test_off_mode_does_not_read_cohorts():
    """Режим off не ходит в БД вообще."""
    with patch("services.trend_gate._fetch_cohort_rows") as mock_fetch:
        ctx = load_trend_context(cfg={"trend": {"mode": "off"}})
    mock_fetch.assert_not_called()
    assert ctx.mode is TrendMode.OFF
    assert ctx.verdicts == {}


# ---------------------------------------------------------------------------
# Правило (а): вето на подъём
# ---------------------------------------------------------------------------

def test_active_decline_vetoes_raise():
    ctx = _ctx(TrendMode.ACTIVE, _verdict(_DECLINE_SERIES))
    res = evaluate_raise_veto(ctx, "adset1")
    assert res.matched is True
    assert res.applied is True
    assert "квал 40% → 16% три недели подряд" in res.reason


def test_shadow_decline_does_not_veto_but_reports():
    ctx = _ctx(TrendMode.SHADOW, _verdict(_DECLINE_SERIES))
    res = evaluate_raise_veto(ctx, "adset1")
    assert res.matched is True
    assert res.applied is False
    assert res.reason.endswith(SHADOW_SUFFIX)


@pytest.mark.parametrize("mode", [TrendMode.OFF, TrendMode.SHADOW, TrendMode.ACTIVE])
def test_growth_never_vetoes_and_never_raises(mode):
    """РОСТ не даёт ни вето, ни какого-либо разрешения поднять.

    Функции, которая вернула бы «поднять», в trend_gate нет вовсе — это и есть
    главный предохранитель волны 3.
    """
    import services.trend_gate as trend_gate

    ctx = _ctx(mode, _verdict(_GROWTH_SERIES))
    res = evaluate_raise_veto(ctx, "adset1")
    assert res.matched is False
    assert res.applied is False
    assert not [
        name
        for name in trend_gate.__all__
        if any(word in name.lower() for word in ("raise_allow", "boost", "scale_up"))
    ]


@pytest.mark.parametrize("series", [_THIN_SERIES, _GROWTH_SERIES])
def test_non_decline_changes_nothing(series):
    ctx = _ctx(TrendMode.ACTIVE, _verdict(series))
    res = evaluate_raise_veto(ctx, "adset1")
    assert (res.matched, res.applied, res.reason) == (False, False, None)


def test_ad_level_verdict_does_not_veto():
    """Не-action-grade вердикт (уровень объявления) не ветирует даже в active."""
    verdict = _verdict(_DECLINE_SERIES, level=TrendLevel.AD, entity_id="adset1")
    ctx = _ctx(TrendMode.ACTIVE, verdict)
    assert evaluate_raise_veto(ctx, "adset1").matched is False


def test_unknown_adset_and_empty_context_do_not_veto():
    ctx = _ctx(TrendMode.ACTIVE, _verdict(_DECLINE_SERIES))
    assert evaluate_raise_veto(ctx, "adset-другой").matched is False
    assert evaluate_raise_veto(ctx, None).matched is False
    assert evaluate_raise_veto(None, "adset1").matched is False


def test_describe_verdict_names_both_ends():
    assert (
        describe_verdict(_verdict(_DECLINE_SERIES))
        == "тренд: квал 40% → 16% три недели подряд"
    )


# ---------------------------------------------------------------------------
# Чтение когорт: сбой НЕ блокирует подъём, но логируется
# ---------------------------------------------------------------------------

def _make_cohort_db(tmp_path: Path, rows) -> str:
    db_path = tmp_path / "kb.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE ad_weekly_cohorts (
            ad_id TEXT, week_start TEXT, adset_id TEXT, adset_name TEXT,
            ad_name TEXT, city TEXT, spend_usd REAL, impressions INTEGER,
            fb_leads INTEGER, amo_leads INTEGER, quals INTEGER,
            days_covered INTEGER, days_expected INTEGER, comparable INTEGER,
            not_comparable_reason TEXT, builder_version TEXT, computed_at TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO ad_weekly_cohorts (
            ad_id, week_start, adset_id, adset_name, ad_name, spend_usd,
            amo_leads, quals, comparable, not_comparable_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
        """,
        rows,
    )
    conn.commit()
    conn.close()
    return str(db_path)


def test_load_trend_context_reads_real_cohorts(tmp_path):
    rows = [
        ("ad1", week.isoformat(), "adset1", "Адсет", "Реклама", 100.0, leads, quals)
        for week, leads, quals in _DECLINE_SERIES
    ]
    db_path = _make_cohort_db(tmp_path, rows)

    with patch("services.creative_intelligence.DB_PATH", db_path):
        ctx = load_trend_context(cfg={"trend": {"mode": "active"}}, as_of=_AS_OF)

    assert ctx.error is None
    assert ctx.verdicts["adset1"].status is TrendStatus.DECLINE
    assert evaluate_raise_veto(ctx, "adset1").applied is True


def test_missing_table_is_not_an_error(tmp_path):
    """БД без миграции 024 — не сбой: тренд просто не участвует."""
    db_path = tmp_path / "empty.db"
    sqlite3.connect(db_path).close()

    with patch("services.creative_intelligence.DB_PATH", str(db_path)):
        ctx = load_trend_context(cfg={"trend": {"mode": "active"}}, as_of=_AS_OF)

    assert ctx.error is None
    assert ctx.verdicts == {}
    assert evaluate_raise_veto(ctx, "adset1").matched is False


def test_cohort_read_failure_does_not_veto_and_is_logged(tmp_path, caplog):
    """Сбой чтения когорт: вето НЕ применяется, факт сбоя виден в логе.

    Здесь сознательно НЕ fail-closed: сбой аналитики не повод блокировать
    подъём, прошедший все денежные гейты скейлера.
    """
    broken = tmp_path / "broken.db"
    broken.write_text("это не sqlite", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="services.trend_gate"), patch(
        "services.creative_intelligence.DB_PATH", str(broken)
    ):
        ctx = load_trend_context(cfg={"trend": {"mode": "active"}}, as_of=_AS_OF)

    assert ctx.error is not None
    assert ctx.verdicts == {}
    assert evaluate_raise_veto(ctx, "adset1").matched is False
    assert any("trend_gate" in record.name for record in caplog.records)


def test_rule_failure_does_not_veto(tmp_path):
    """Исключение внутри правила тоже даёт пустой контекст, а не падение."""
    rows = [("ad1", _W1.isoformat(), "adset1", "Адсет", "Реклама", 1.0, 10, 1)]
    db_path = _make_cohort_db(tmp_path, rows)

    with patch("services.creative_intelligence.DB_PATH", db_path), patch(
        "services.trend_gate.evaluate_cohort_rows", side_effect=RuntimeError("бум")
    ):
        ctx = load_trend_context(cfg={"trend": {"mode": "active"}}, as_of=_AS_OF)

    assert ctx.error is not None and "trend_rule_error" in ctx.error
    assert evaluate_raise_veto(ctx, "adset1").matched is False


def test_uninitialized_db_is_silent(tmp_path):
    with patch("services.creative_intelligence.DB_PATH", None):
        ctx = load_trend_context(cfg={"trend": {"mode": "active"}}, as_of=_AS_OF)
    assert ctx.error is None
    assert ctx.verdicts == {}


# ---------------------------------------------------------------------------
# Budget Scaler: вето подъёма
# ---------------------------------------------------------------------------

_VALID_PLAN = {
    "week_label": "неделя 1 (2026-07-01–07)",
    "budget_usd": 100_000.0,
    "revenue_plan_lcy": 10_000_000.0,
    "unit_target": 0.99,
}


def _scaler_cfg(**over) -> dict:
    return {
        "enabled": True,
        "kill_switch": False,
        "scale_enabled": False,
        "max_budget_increase_pct": 15,
        "max_adset_budget_mult": 2.0,
        "max_adset_daily_budget": 300,
        "max_total_daily_budget": 4000,
        "max_scales_per_run": 2,
        "plan_sheet_id": "test_sheet_id",
        **over,
    }


def _winner_ad(ad_id: str = "ad1") -> dict:
    """Объявление-победитель по продажам (payments>0) — кандидат на подъём."""
    return {
        "ad_id": ad_id,
        "ad_name": "Победитель",
        "city": "CityA",
        "adset_type": "L2",
        "adset_id": None,
        "spend": 80.0,
        "leads": 8,
        "qual_pct": 20.0,
        "romi": 150.0,
        "cpl": 10.0,
        "ctr": 2.0,
        "hook_rate": 30.0,
        "impressions": 5000,
        "video_p25": 0,
        "video_p100": 0,
        "video_views_3s": 0,
        "payments": 3,
        "outcomes_matched_at": None,
        "days_running": 10,
        "effective_status": "ACTIVE",
        "recommendation": "ЖДАТЬ",
        "reason": "",
    }


def _run_scaler(trend_ctx, *, mode: str = "dry_run", cfg: dict | None = None):
    """Прогон скейлера с подставленным контекстом тренда. Все внешние — моки."""
    local_ads = [_winner_ad("ad1")]
    decisions = [{
        "ad_id": "ad1", "ad_name": "Победитель", "adset_id": "",
        "action": "SCALE", "score": 7, "reasons": [],
    }]
    all_budgets = {
        "adset1": {
            "daily_budget_usd": 100.0,
            "effective_status": "ACTIVE",
            "name": "Тестовый адсет",
        }
    }

    with patch("services.budget_scaler.get_scale_config",
               return_value=cfg or _scaler_cfg()), \
         patch("services.trend_gate.load_trend_context", return_value=trend_ctx), \
         patch("services.plan_reader.read_general_plan", return_value=_VALID_PLAN), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=200_000.0), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets",
               return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         patch("services.budget_scaler.set_adset_budget") as mock_set, \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode=mode)

    return result, mock_set, mock_tg


def test_scaler_active_trend_vetoes_raise():
    """Падающий тренд адсета снимает подъём — рекомендаций не остаётся."""
    ctx = _ctx(TrendMode.ACTIVE, _verdict(_DECLINE_SERIES))
    result, mock_set, mock_tg = _run_scaler(ctx)

    mock_set.assert_not_called()
    assert result["recommendations"] == []
    assert result["skipped_reason"] == "no_adset_candidates"
    assert result["trend_mode"] == "active"
    assert result["trend_vetoed"] == 1
    assert result["trend_shadow_vetoed"] == 0
    text = "\n".join(str(call.args[0]) for call in mock_tg.call_args_list)
    assert "Тренд снял подъёмов: 1" in text
    assert "квал 40% → 16% три недели подряд" in text


def test_scaler_shadow_changes_nothing_but_reports():
    """Shadow: исход БИТ-В-БИТ такой же, как без тренда, но в отчёте видно."""
    shadow_result, shadow_set, shadow_tg = _run_scaler(
        _ctx(TrendMode.SHADOW, _verdict(_DECLINE_SERIES))
    )
    plain_result, plain_set, _ = _run_scaler(_ctx(TrendMode.OFF))

    assert shadow_result["recommendations"] == plain_result["recommendations"]
    assert shadow_result["skipped_reason"] == plain_result["skipped_reason"]
    assert shadow_result["scaled"] == plain_result["scaled"] == []
    assert shadow_set.call_count == plain_set.call_count == 0
    # Отличается ТОЛЬКО наблюдаемость
    assert shadow_result["trend_vetoed"] == 0
    assert shadow_result["trend_shadow_vetoed"] == 1
    text = "\n".join(str(call.args[0]) for call in shadow_tg.call_args_list)
    assert "Тренд снял бы подъёмов: 1 (тень: не применено)" in text


@pytest.mark.parametrize("mode", [TrendMode.OFF, TrendMode.SHADOW, TrendMode.ACTIVE])
def test_scaler_growth_gives_no_extra_raise(mode):
    """РОСТ не добавляет ни подъёма, ни процента — ни в одном режиме."""
    grown, _, _ = _run_scaler(_ctx(mode, _verdict(_GROWTH_SERIES)))
    plain, _, _ = _run_scaler(_ctx(TrendMode.OFF))

    assert grown["recommendations"] == plain["recommendations"]
    assert grown["trend_vetoed"] == 0
    assert grown["trend_shadow_vetoed"] == 0


def test_scaler_insufficient_trend_changes_nothing():
    thin, _, _ = _run_scaler(_ctx(TrendMode.ACTIVE, _verdict(_THIN_SERIES)))
    plain, _, _ = _run_scaler(_ctx(TrendMode.OFF))
    assert thin["recommendations"] == plain["recommendations"]
    assert thin["trend_vetoed"] == 0


def test_scaler_ad_level_verdict_does_not_veto():
    """Диагностический вердикт уровня объявления подъём не блокирует."""
    verdict = _verdict(_DECLINE_SERIES, level=TrendLevel.AD, entity_id="adset1")
    result, _, _ = _run_scaler(_ctx(TrendMode.ACTIVE, verdict))
    plain, _, _ = _run_scaler(_ctx(TrendMode.OFF))
    assert result["recommendations"] == plain["recommendations"]
    assert result["trend_vetoed"] == 0


def test_scaler_trend_read_failure_does_not_block_raise():
    """Сбой чтения когорт не блокирует подъём (не fail-closed намеренно)."""
    ctx = _ctx(TrendMode.ACTIVE, error="cohort_read_error: файл битый")
    result, _, _ = _run_scaler(ctx)
    plain, _, _ = _run_scaler(_ctx(TrendMode.OFF))

    assert result["recommendations"] == plain["recommendations"]
    assert len(result["recommendations"]) == 1
    assert result["trend_vetoed"] == 0


def test_scaler_active_mode_trend_blocks_proposal(monkeypatch, tmp_path):
    """Боевой режим скейлера: при вето тренда владельцу ничего не предлагается."""
    from tests.gateway_test_helpers import install_proposal_recorder

    recorded = install_proposal_recorder(monkeypatch, tmp_path)
    ctx = _ctx(TrendMode.ACTIVE, _verdict(_DECLINE_SERIES))
    result, mock_set, _ = _run_scaler(
        ctx, mode="active", cfg=_scaler_cfg(scale_enabled=True)
    )

    mock_set.assert_not_called()
    assert recorded.plans == []
    assert result["proposals"] == []
    assert result["trend_vetoed"] == 1


# ---------------------------------------------------------------------------
# Decision Policy: тренд как ВЕС, а не как основание
# ---------------------------------------------------------------------------

def _tier_b_ad(ad_id: str = "ad1", adset_id: str = "adset1", city: str = "CityA") -> dict:
    """Объявление тира B: сверка прошла, 0 оплат, расход выше порога, квал 5%.

    До волны 3 это диагностика, а не пауза, — ей не хватало доказательства
    зрелости лидов.

    city разводится по тестам намеренно: портфельный ранг группирует по городу
    и в группе из одного объявления никогда не ставит «ОТКЛЮЧИТЬ». Так тесты
    ниже меряют вклад ИМЕННО тренда, а не портфельного аутсайдера, который и
    без тренда дал бы PAUSE.
    """
    return {
        "ad_id": ad_id,
        "ad_name": "Слабое",
        "adset_id": adset_id,
        "city": city,
        "adset_type": "L2",
        "spend": 200.0,
        "leads": 20,
        "qual_pct": 5.0,
        "romi": 0.0,
        "cpl": 10.0,
        "ctr": 1.0,
        "hook_rate": 10.0,
        "impressions": 5000,
        "payments": 0,
        "outcomes_matched_at": "2026-08-01T00:00:00+05:00",
        "days_running": 20,
        "leads_lifetime": 20,
    }


def _decide(ads, trend_ctx=None):
    from services.decision_policy import score_and_decide

    with patch("agent.scheduler.load_settings", return_value={}):
        return score_and_decide(ads, None, trend_ctx=trend_ctx)


def _weak_and_strong() -> list[dict]:
    """Слабое (тир B) и сильное объявление ОДНОГО падающего адсета."""
    return [
        _tier_b_ad("ad1", city="CityA"),
        {
            **_tier_b_ad("ad2", city="CityB"),
            "qual_pct": 30.0,
            "payments": 5,
            "romi": 300.0,
        },
    ]


def test_trend_alone_does_not_pause_healthy_ad():
    """Здоровое объявление в падающем адсете НЕ паузится: тренд не основание."""
    healthy = {**_tier_b_ad("ad1", city="CityA"), "qual_pct": 30.0, "payments": 4, "romi": 300.0}
    other = {**_tier_b_ad("ad2", city="CityB"), "qual_pct": 28.0, "payments": 3, "romi": 280.0}
    ctx = _ctx(TrendMode.ACTIVE, _verdict(_DECLINE_SERIES))

    results = _decide([healthy, other], trend_ctx=ctx)

    assert [r["action"] for r in results] == ["KEEP", "KEEP"]
    assert all(r["trend_reinforced"] is False for r in results)
    assert any("сам по себе тренд паузу не даёт" in r for r in results[0]["reasons"])


def test_trend_promotes_only_already_weak_ad():
    """Тир B + подтверждённое падение адсета → PAUSE с честной причиной."""
    ctx = _ctx(TrendMode.ACTIVE, _verdict(_DECLINE_SERIES))

    results = {r["ad_id"]: r for r in _decide(_weak_and_strong(), trend_ctx=ctx)}

    assert results["ad1"]["action"] == "PAUSE"
    assert results["ad1"]["trend_reinforced"] is True
    assert "квал 40% → 16% три недели подряд" in results["ad1"]["business_reason"]
    # Сильное объявление того же адсета не тронуто
    assert results["ad2"]["action"] == "KEEP"
    assert results["ad2"]["trend_reinforced"] is False


def test_shadow_mode_does_not_change_decisions():
    """Shadow: те же действия, что без тренда, только пометка в reasons."""
    ads = _weak_and_strong()
    verdict = _verdict(_DECLINE_SERIES)

    plain = {r["ad_id"]: r for r in _decide([dict(a) for a in ads])}
    shadow = {
        r["ad_id"]: r
        for r in _decide([dict(a) for a in ads], trend_ctx=_ctx(TrendMode.SHADOW, verdict))
    }

    assert [plain[k]["action"] for k in ("ad1", "ad2")] == ["KEEP", "KEEP"]
    assert {k: shadow[k]["action"] for k in shadow} == {
        k: plain[k]["action"] for k in plain
    }
    assert shadow["ad1"]["trend_shadow"] is True
    assert shadow["ad1"]["trend_reinforced"] is False
    assert any(SHADOW_SUFFIX in r for r in shadow["ad1"]["reasons"])


def test_growth_never_produces_scale_or_upgrade():
    """РОСТ адсета не превращает KEEP в SCALE и вообще ничего не меняет."""
    ads = _weak_and_strong()
    ctx = _ctx(TrendMode.ACTIVE, _verdict(_GROWTH_SERIES))

    plain = {r["ad_id"]: r for r in _decide([dict(a) for a in ads])}
    grown = {r["ad_id"]: r for r in _decide([dict(a) for a in ads], trend_ctx=ctx)}

    for ad_id in ("ad1", "ad2"):
        assert grown[ad_id]["action"] == plain[ad_id]["action"]
        assert grown[ad_id]["action"] != "SCALE"
        assert grown[ad_id]["trend_reinforced"] is False


def test_decline_vetoes_scale_action():
    """SCALE от скоринга снимается падающим трендом — это вето, а не подъём."""
    from services.decision_policy import _apply_trend_weight

    ctx = _ctx(TrendMode.ACTIVE, _verdict(_DECLINE_SERIES))
    reasons: list[str] = []
    action, business, flags = _apply_trend_weight(
        trend_ctx=ctx, adset_id="adset1", action="SCALE", business_reason="",
        reasons=reasons, is_tier_b_diagnostic=False, spend=100.0, qual_pct=20.0,
    )
    assert action == "KEEP"
    assert flags["trend_vetoed_scale"] is True
    assert any("вето подъёма" in r for r in reasons)


def test_trend_never_upgrades_action_to_scale():
    """Ни один вход не даёт тренду превратить решение в SCALE."""
    from services.decision_policy import _apply_trend_weight

    for mode in (TrendMode.OFF, TrendMode.SHADOW, TrendMode.ACTIVE):
        for series in (_DECLINE_SERIES, _GROWTH_SERIES, _THIN_SERIES):
            for action in ("KEEP", "PAUSE", "SCALE"):
                new_action, _, _ = _apply_trend_weight(
                    trend_ctx=_ctx(mode, _verdict(series)),
                    adset_id="adset1",
                    action=action,
                    business_reason="",
                    reasons=[],
                    is_tier_b_diagnostic=False,
                    spend=100.0,
                    qual_pct=20.0,
                )
                assert not (new_action == "SCALE" and action != "SCALE")


def test_insufficient_trend_leaves_decisions_untouched():
    ads = [_tier_b_ad("ad1")]
    ctx = _ctx(TrendMode.ACTIVE, _verdict(_THIN_SERIES))

    plain = _decide([dict(a) for a in ads])[0]
    thin = _decide([dict(a) for a in ads], trend_ctx=ctx)[0]

    assert thin["action"] == plain["action"]
    assert thin["trend_reinforced"] is False
    assert thin["trend_status"] == TrendStatus.INSUFFICIENT.value


def test_trend_does_not_disturb_peer_comparison():
    """Тренд подключён ПОСЛЕ медиан и ранга — score и порядок не меняются."""
    ads = [
        {**_tier_b_ad("ad1"), "qual_pct": 12.0, "ctr": 3.0, "hook_rate": 40.0},
        {**_tier_b_ad("ad2"), "qual_pct": 30.0, "ctr": 1.0, "hook_rate": 10.0},
        {**_tier_b_ad("ad3"), "adset_id": "adset2", "qual_pct": 25.0, "ctr": 2.0},
    ]
    ctx = _ctx(TrendMode.ACTIVE, _verdict(_DECLINE_SERIES))

    plain = {r["ad_id"]: r for r in _decide([dict(a) for a in ads])}
    with_trend = {r["ad_id"]: r for r in _decide([dict(a) for a in ads], trend_ctx=ctx)}

    for ad_id in plain:
        assert with_trend[ad_id]["score"] == plain[ad_id]["score"]
    # Адсет без вердикта живёт ровно как раньше
    assert with_trend["ad3"]["action"] == plain["ad3"]["action"]
    assert with_trend["ad3"]["trend_status"] is None


def test_no_trend_context_keeps_legacy_shape():
    """Без контекста поля тренда нейтральны — поведение как до волны 3."""
    result = _decide([_tier_b_ad("ad1")])[0]
    assert result["trend_status"] is None
    assert result["trend_reinforced"] is False
    assert result["trend_vetoed_scale"] is False
    assert result["trend_shadow"] is False


# ---------------------------------------------------------------------------
# Удержание: тренд входит в него, но не обходит
# ---------------------------------------------------------------------------

_HOLD_CFG = {
    "hold_enabled": True,
    "hold_romi_target": 200,
    "hold_romi_ratio": 0.8,
    "hold_spend_max": 800,
    "hold_min_qual_pct": 15,
    "hold_min_meetings": 2,
    "hold_days": 8,
}


def test_trend_reinforced_pause_enters_hold():
    """Тренд — основание ВОЙТИ в удержание, если у объявления есть потенциал."""
    from services.autopilot_hold import should_hold

    decision = {
        "ad_id": "ad1", "action": "PAUSE", "reasons": ["вес к паузе: тренд..."],
        "trend_reinforced": True,
    }
    metrics = {"spend": 100.0, "qual_pct": 20.0, "payments": 0}
    do_hold, reason = should_hold(decision, metrics, _HOLD_CFG)
    assert do_hold is True
    assert reason == ""


def test_trend_hold_timer_is_shorter():
    """Таймер удержания при падающем тренде вдвое короче, но не меньше суток."""
    from services.autopilot_hold import make_hold_entry

    metrics = {"spend": 100.0, "qual_pct": 20.0, "payments": 0}
    base = make_hold_entry({"ad_id": "ad1", "ad_name": "A", "reasons": []}, metrics, _HOLD_CFG)
    trended = make_hold_entry(
        {"ad_id": "ad1", "ad_name": "A", "reasons": [], "trend_reinforced": True},
        metrics,
        _HOLD_CFG,
    )
    base_days = (
        datetime.fromisoformat(base["hold_until"])
        - datetime.fromisoformat(base["held_at"])
    ).days
    trend_days = (
        datetime.fromisoformat(trended["hold_until"])
        - datetime.fromisoformat(trended["held_at"])
    ).days
    assert base_days == 8
    assert trend_days == 4

    short_cfg = {**_HOLD_CFG, "hold_days": 1}
    shortest = make_hold_entry(
        {"ad_id": "ad1", "ad_name": "A", "reasons": [], "trend_reinforced": True},
        metrics,
        short_cfg,
    )
    assert datetime.fromisoformat(shortest["hold_until"]) > datetime.fromisoformat(
        shortest["held_at"]
    )


def test_trend_does_not_bypass_hold():
    """Падающий тренд НЕ даёт права проскочить удержание мимо его условий."""
    from services.autopilot_hold import should_hold

    decision = {
        "ad_id": "ad1", "action": "PAUSE", "reasons": ["портфельный аутсайдер"],
        "trend_reinforced": True,
    }
    # Расход выше hold_spend_max — удержание не ставится, но и обхода нет:
    # решение остаётся ровно тем же, что без тренда.
    heavy = {"spend": 5000.0, "qual_pct": 20.0, "payments": 0}
    with_trend = should_hold(decision, heavy, _HOLD_CFG)
    without_trend = should_hold({**decision, "trend_reinforced": False}, heavy, _HOLD_CFG)
    assert with_trend == without_trend == (False, "расход большой")

    # Выключенное удержание тренд тоже не включает
    off_cfg = {**_HOLD_CFG, "hold_enabled": False}
    assert should_hold(decision, {"spend": 10.0, "qual_pct": 20.0}, off_cfg) == (
        False,
        "удержание выключено",
    )


def test_trend_does_not_override_waster_immediate_pause():
    """Слив паузится сразу и при падающем тренде — приоритет правил сохранён."""
    from services.autopilot_hold import should_hold

    decision = {
        "ad_id": "ad1", "action": "PAUSE", "reasons": ["портфельный аутсайдер"],
        "is_confirmed_waster": True, "trend_reinforced": True,
    }
    assert should_hold(decision, {"spend": 10.0, "qual_pct": 30.0}, _HOLD_CFG) == (
        False,
        "слив — не держим",
    )


# ---------------------------------------------------------------------------
# Настройки: переключение режима без деплоя
# ---------------------------------------------------------------------------

def test_settings_accept_all_modes():
    from web.settings_validation import validate_settings_update

    for mode in ("off", "shadow", "active"):
        out = validate_settings_update({"autopilot": {"trend": {"mode": mode}}}, {})
        assert out["autopilot"]["trend"]["mode"] == mode


def test_settings_reject_bad_mode_and_fields():
    from fastapi import HTTPException
    from web.settings_validation import validate_settings_update

    with pytest.raises(HTTPException) as exc:
        validate_settings_update({"autopilot": {"trend": {"mode": "on"}}}, {})
    assert exc.value.status_code == 400
    assert "autopilot.trend.mode" in exc.value.detail

    with pytest.raises(HTTPException):
        validate_settings_update({"autopilot": {"trend": {"режим": "off"}}}, {})

    with pytest.raises(HTTPException):
        validate_settings_update({"autopilot": {"trend": "active"}}, {})


def test_settings_merge_keeps_other_autopilot_blocks():
    from web.settings_validation import validate_settings_update

    current = {"autopilot": {"enabled": True, "trend": {"mode": "shadow"}}}
    out = validate_settings_update({"autopilot": {"trend": {"mode": "active"}}}, current)
    assert out["autopilot"]["trend"] == {"mode": "active"}
    assert out["autopilot"]["enabled"] is True


# ---------------------------------------------------------------------------
# Наблюдаемость автопилота
# ---------------------------------------------------------------------------

def test_autopilot_trend_note_marks_shadow():
    from services.autopilot import _format_trend_note

    assert _format_trend_note([]) == ""
    assert "Тренд усилил пауз: 2" in _format_trend_note(
        [{"trend_reinforced": True}, {"trend_reinforced": True}]
    )
    note = _format_trend_note([{"trend_shadow": True}])
    assert "Тренд усилил бы пауз: 1 (тень: не применено)" in note


def test_autopilot_trend_telemetry():
    from services.autopilot import _trend_telemetry

    ctx = _ctx(TrendMode.SHADOW, _verdict(_DECLINE_SERIES))
    telem = _trend_telemetry([{"trend_shadow": True}, {}], ctx)
    assert telem == {
        "trend_mode": "shadow",
        "trend_reinforced": 0,
        "trend_shadow_reinforced": 1,
    }
    assert _trend_telemetry([], None)["trend_mode"] == "none"


def test_context_counters_report_declines():
    ctx = _ctx(
        TrendMode.ACTIVE,
        _verdict(_DECLINE_SERIES, entity_id="adset1"),
        _verdict(_GROWTH_SERIES, entity_id="adset2"),
    )
    counters = ctx.counters()
    assert counters["mode"] == "active"
    assert counters["adsets"] == 2
    assert counters["action_grade_declines"] == 1
    assert counters["error"] is None


def test_week_fixtures_are_mature():
    """Страховка от гниения фикстур: недели должны быть дозревшими на _AS_OF."""
    from services.trend_policy import MIN_WEEK_AGE_DAYS, week_age_days

    assert week_age_days(_W1, _AS_OF) >= MIN_WEEK_AGE_DAYS
    assert _AS_OF - timedelta(days=7) == _W2 + timedelta(days=14)
    assert datetime.now(timezone.utc).year >= 2026
