"""Тесты services/early_kill.py — ранний стоп: правило A «ноль заявок» и правило B «зрелый ноль».

Чистые оценки — без моков. Прогон — на временной KB (init_kb) с замоканными
FB/AMO/Telegram/стражем ОП. Инвариант: ни одной мутации рекламы ни в одном режиме.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from services import early_kill
from services.cpl_reference import CplReference
from services.early_kill import (
    EARLY_KILL_DEFAULTS,
    compute_threshold,
    early_kill_config,
    evaluate_candidate,
    evaluate_rule_b,
    evaluate_rule_c,
    evaluate_rule_s,
    is_excluded_from_rule_b,
    resolve_multipliers,
    rule_b_mode,
    rule_mode,
    run_early_kill,
    runtime_mode,
)
from services.op_load_guard import OpLoadVerdict

_NOW = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
_CAB_B = "29716040622546856"
_CABINET_A = "152882611033373"
_CFG = dict(EARLY_KILL_DEFAULTS)


# ---------------------------------------------------------------------------
# Правило A — чистая логика
# ---------------------------------------------------------------------------

def test_default_multipliers_for_unknown_account_and_cabinet_a_override():
    assert resolve_multipliers(_CFG, _CAB_B) == (Decimal("3.0"), Decimal("4.5"))
    assert resolve_multipliers(_CFG, f"act_{_CABINET_A}") == (Decimal("4.0"), Decimal("6.0"))


def test_one_lead_multiplier_never_below_no_lead():
    cfg = {**_CFG, "multiplier_no_lead": 5.0, "multiplier_one_lead": 2.0}
    assert resolve_multipliers(cfg, _CAB_B) == (Decimal("5.0"), Decimal("5.0"))


def test_threshold_clamped_by_floor_and_cap_and_scaled_for_one_lead():
    kw = {"floor_usd": Decimal("8"), "cap_usd": Decimal("80")}
    assert compute_threshold(Decimal("6.30"), Decimal("3"), **kw) == Decimal("18.90")
    assert compute_threshold(Decimal("1.00"), Decimal("3"), **kw) == Decimal("8.00")
    assert compute_threshold(Decimal("50.00"), Decimal("3"), **kw) == Decimal("80.00")
    assert compute_threshold(Decimal("1.00"), Decimal("4.5"), tolerance=1, **kw) == Decimal("12.00")
    assert compute_threshold(Decimal("50.00"), Decimal("4.5"), tolerance=1, **kw) == Decimal("120.00")


def _eval(spend, leads, cpl="6.30", account=_CAB_B, mode="shadow", cfg=None):
    return evaluate_candidate(
        account_id=account,
        spend=Decimal(spend) if spend is not None else None,
        amo_leads=leads,
        cpl_ref=Decimal(cpl) if cpl is not None else None,
        cfg_block=cfg or _CFG,
        mode=mode,
    )


def test_zero_leads_above_threshold_is_pause():
    v = _eval("19.00", 0)
    assert v.decision == "PAUSE" and v.reason == "spend_cpl_threshold_reached"
    assert v.threshold_usd == Decimal("18.90") and v.multiplier == Decimal("3.0")


def test_zero_leads_below_threshold_waits():
    v = _eval("18.50", 0)
    assert v.decision == "WAIT" and v.reason == "spend_below_threshold"


def test_one_lead_uses_higher_multiplier():
    assert _eval("28.00", 1).decision == "WAIT"
    v = _eval("28.35", 1)
    assert v.decision == "PAUSE" and v.reason == "spend_cpl_threshold_reached:one_lead_tolerated"
    assert v.tolerance == 1 and v.multiplier == Decimal("4.5")


def test_two_leads_keep_regardless_of_spend():
    v = _eval("500.00", 2)
    assert v.decision == "KEEP" and v.reason == "leads_present"


def test_unknown_amo_leads_waits():
    assert _eval("500.00", None).reason == "amo_leads_unknown"


def test_no_cpl_reference_waits():
    assert _eval("500.00", 0, cpl=None).reason == "no_cpl_reference"


def test_missing_spend_is_incomplete_evidence():
    v = _eval(None, 0)
    assert v.decision == "WAIT" and v.reason == "insufficient_lifetime_evidence"


def test_mode_off_never_pauses():
    assert _eval("500.00", 0, mode="off").reason == "contour_off"


def test_cabinet_a_multiplier_applies_per_account():
    assert _eval("33.00", 0, cpl="8.40", account=_CABINET_A).decision == "WAIT"
    assert _eval("33.60", 0, cpl="8.40", account=_CABINET_A).decision == "PAUSE"


def test_runtime_modes_per_rule():
    """Волна 2c (решение владельца): у каждого правила свой режим, active разрешён всем."""
    assert runtime_mode({"mode": "active"}) == "active"
    assert runtime_mode({"mode": "shadow"}) == "shadow"
    assert runtime_mode({"mode": "off"}) == "off"
    assert runtime_mode({"mode": "garbage"}) == "off"
    assert rule_b_mode({"b_mode": "active"}) == "active"
    assert rule_b_mode({"b_mode": "shadow", "b_enabled": False}) == "off"
    assert rule_b_mode({"b_mode": "garbage"}) == "off"
    assert rule_mode({"c_mode": "active"}, "C") == "active"
    assert rule_mode({"s_mode": "weird"}, "S") == "off"
    assert rule_mode({}, "C") == "off"


def test_exclusion_markers_match_ad_or_adset_name():
    cfg = {**_CFG, "b_exclude_name_markers": ["intl"]}
    assert is_excluded_from_rule_b(("intl_line | Вариант 4", "Owner | L1 | intl_line"), cfg)
    assert is_excluded_from_rule_b(("CityA | тест", "Owner | L1 | cityb | MQL | intl_line | v2"), cfg)
    assert not is_excluded_from_rule_b(("CityA | тест", "Owner | L1 | SO CityA"), cfg)
    assert not is_excluded_from_rule_b(("intl_line", ""), {**_CFG, "b_exclude_name_markers": []})


def test_config_merges_defaults_and_sanitizes_mode():
    cfg = early_kill_config({"early_kill": {"mode": "weird", "max_per_day": 3}})
    assert cfg["mode"] == "off" and cfg["max_per_day"] == 3
    assert cfg["floor_usd"] == 8.0 and cfg["b_min_mature_leads"] == 3


def test_autopilot_config_exposes_early_kill_block(monkeypatch):
    from services.autopilot import AUTOPILOT_DEFAULTS, get_autopilot_config

    assert AUTOPILOT_DEFAULTS["early_kill"] == EARLY_KILL_DEFAULTS
    monkeypatch.setattr(
        "agent.scheduler.load_settings",
        lambda: {"autopilot": {"early_kill": {"mode": "nonsense", "unknown": 1, "max_per_day": 4}}},
    )
    cfg = get_autopilot_config()
    assert cfg["early_kill"]["mode"] == "off"
    assert cfg["early_kill"]["max_per_day"] == 4
    assert "unknown" not in cfg["early_kill"]


# ---------------------------------------------------------------------------
# Правило B — чистая логика
# ---------------------------------------------------------------------------

def _eval_b(spend, leads, mature, quals, *, age=100.0, cpl="10.00", op_ok=True, mode="shadow", cfg=None, account=_CAB_B):
    return evaluate_rule_b(
        account_id=account, age_hours=age,
        spend=Decimal(spend) if spend is not None else None,
        amo_leads=leads, mature_leads=mature, quals=quals,
        cpl_ref=Decimal(cpl) if cpl is not None else None,
        cfg_block=cfg or _CFG, mode=mode, op_ok=op_ok,
    )


def test_b_maturity_hours_setting_moves_age_gate():
    """Срок зрелости B — настройка (например, 48 ч), дефолт прежние 72 ч."""
    cfg48 = {**_CFG, "b_maturity_hours": 48}
    assert _eval_b("500.00", 5, 3, 0, age=50.0).reason == "age_below_72h"
    assert _eval_b("500.00", 5, 3, 0, age=47.9, cfg=cfg48).reason == "age_below_48h"
    v = _eval_b("500.00", 5, 3, 0, age=50.0, cfg=cfg48)
    assert v.decision == "PAUSE" and v.reason == "mature_zero_quals"
    # мусор и выход за границы не ломают правило
    assert early_kill.b_maturity_hours({"b_maturity_hours": "x"}) == 72
    assert early_kill.b_maturity_hours({"b_maturity_hours": 1}) == 24


def test_amo_summary_b_hours_affects_lifetime_mature_only():
    """Зрелость для B считается по b_hours, окно C остаётся на 72 часах."""
    now = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    records = [(now - timedelta(hours=h), False) for h in (30, 50, 60, 80)]
    with patch("services.waster_rules_v2._load_lead_records", return_value=records):
        s72 = early_kill._fetch_amo_summary("1", now - timedelta(days=5), now=now)
        s48 = early_kill._fetch_amo_summary("1", now - timedelta(days=5), now=now, b_hours=48)
    assert (s72["mature"], s72["mature_14d"]) == (1, 1)
    assert (s48["mature"], s48["mature_14d"]) == (3, 1)


def test_b_mature_zero_quals_above_threshold_is_pause():
    # порог = max($25, 2 × $10) = $20 → $25
    v = _eval_b("25.00", 4, 3, 0)
    assert v.decision == "PAUSE" and v.reason == "mature_zero_quals"
    assert v.threshold_usd == Decimal("25") and v.multiplier == Decimal("2.0")
    assert _eval_b("24.99", 4, 3, 0).reason == "spend_below_threshold"


def test_b_threshold_follows_cpl_when_higher_than_floor():
    # 2 × $18.05 = $36.10 > $25
    v = _eval_b("36.00", 5, 3, 0, cpl="18.05")
    assert v.decision == "WAIT" and v.threshold_usd == Decimal("36.10")
    assert _eval_b("36.10", 5, 3, 0, cpl="18.05").decision == "PAUSE"


def test_b_threshold_without_cpl_uses_floor_only():
    v = _eval_b("25.00", 3, 3, 0, cpl=None)
    assert v.decision == "PAUSE" and v.threshold_usd == Decimal("25")


def test_b_any_qual_keeps_forever():
    v = _eval_b("500.00", 10, 8, 1)
    assert v.decision == "KEEP" and v.reason == "quals_present"


_LADDER_CFG = {**_CFG, "b_ladder": [[8, 0], [10, 1], [16, 2], [20, 3]], "b_tail_norm_share": 0.75,
               "b_ladder_accounts": {"152882611033373": [[8, 0], [12, 1], [16, 2], [20, 3]]}}


@pytest.mark.parametrize("mature,quals,decision,reason", [
    (7, 0, "WAIT", "mature_below_min"),      # до первой ступени не дошло
    (8, 0, "PAUSE", "mature_zero_quals"),    # 8 заявок, квалов ноль
    (9, 1, "KEEP", "quals_present"),         # один квал на 9 — ещё живёт
    (10, 1, "PAUSE", "ladder_low_quals"),    # «фартануло» с одним квалом — ловит вторая ступень
    (15, 2, "KEEP", "quals_present"),
    (16, 2, "PAUSE", "ladder_low_quals"),
    (20, 3, "PAUSE", "ladder_low_quals"),
    (24, 4, "KEEP", "quals_present"),        # 4 из 24 = 17% — норма
    (60, 5, "PAUSE", "ladder_tail_rate"),    # 8% при хвосте 12% (0.75 × норма 16%)
    (60, 9, "KEEP", "quals_present"),        # 15% — выше хвоста
])
def test_b_ladder_steps_and_tail(mature, quals, decision, reason):
    v = _eval_b("500.00", mature, mature, quals, cfg=_LADDER_CFG)
    assert (v.decision, v.reason) == (decision, reason)


def test_b_ladder_per_account_and_floating_tail():
    # cabinet_a: вторая ступень 12, а не 10
    assert _eval_b("500.00", 10, 10, 1, cfg=_LADDER_CFG, account=_CABINET_A).decision == "KEEP"
    assert _eval_b("500.00", 12, 12, 1, cfg=_LADDER_CFG, account=_CABINET_A).reason == "ladder_low_quals"
    # норма кабинета упала до 10% → хвост 7.5%: 5 из 60 (8.3%) уже не режется
    v = evaluate_rule_b(account_id=_CAB_B, age_hours=100.0, spend=Decimal("500"), amo_leads=60, mature_leads=60, quals=5,
                        cpl_ref=Decimal("10"), cfg_block=_LADDER_CFG, mode="shadow", qual_norm=Decimal("0.10"))
    assert v.decision == "KEEP"
    assert early_kill.ladder_tail_rate(_LADDER_CFG, None) == Decimal("0.1200")
    assert early_kill.ladder_is_on(_LADDER_CFG, _CAB_B) and not early_kill.ladder_is_on(_CFG, _CAB_B)


def test_b_ladder_respects_spend_threshold_and_op_guard():
    assert _eval_b("10.00", 10, 10, 1, cfg=_LADDER_CFG).reason == "spend_below_threshold"
    assert _eval_b("500.00", 10, 10, 1, cfg=_LADDER_CFG, op_ok=False).reason == "op_load_guard"


def test_c_ladder_judges_window_from_first_step():
    kw = dict(spend_14d=Decimal("120"), cpq_ref=Decimal("50"), mode="shadow", account_id=_CAB_B)
    v = early_kill.evaluate_rule_c(leads_14d=12, mature_14d=10, quals_14d=1, cfg_block=_LADDER_CFG, **kw)
    assert (v.decision, v.reason) == ("PAUSE", "ladder_low_quals")
    # без лестницы прежний порог объёма 30 зрелых
    v_old = early_kill.evaluate_rule_c(leads_14d=12, mature_14d=10, quals_14d=1, cfg_block=_CFG, **kw)
    assert (v_old.decision, v_old.reason) == ("WAIT", "mature_below_min")
    # расход окна ниже минимума — лестница окна молчит
    v_low = early_kill.evaluate_rule_c(leads_14d=12, mature_14d=10, quals_14d=1, cfg_block=_LADDER_CFG,
                                       **{**kw, "spend_14d": Decimal("10")})
    assert v_low.decision == "WAIT"


_PRICE_CFG = {**_CFG, "c_price_mode": "account_norm", "c_cpq_norm_mult": 1.2, "c_price_min_spend_mult": 2.0,
              "c_price_min_mature": 5}


@pytest.mark.parametrize("spend,mature,quals,norm,decision,reason", [
    ("239.00", 10, 1, "100", "WAIT", "spend_below_threshold"),   # судим с двух планок: 2 × $120
    ("240.00", 10, 1, "100", "PAUSE", "quality_expensive"),      # квал $240 при планке $120
    ("240.00", 10, 2, "100", "KEEP", "quality_ok"),              # квал $120 — ровно планка
    ("240.00", 4, 0, "100", "WAIT", "mature_below_min"),         # меньше 5 зрелых заявок
    ("300.00", 6, 0, "100", "PAUSE", "quality_expensive"),       # ноль квалов = бесконечная цена
    ("400.00", 10, 3, "150", "KEEP", "quality_ok"),              # норма выросла до $150 → планка $180, квал $133 ок
    ("400.00", 10, 3, "100", "PAUSE", "quality_expensive"),      # тот же квал $133 при норме $100 — дорого
    ("240.00", 10, 1, None, "PAUSE", "quality_expensive"),       # нормы нет → фолбэк $100
])
def test_c_price_by_account_norm(spend, mature, quals, norm, decision, reason):
    v = early_kill.evaluate_rule_c(
        spend_14d=Decimal(spend), leads_14d=mature, mature_14d=mature, quals_14d=quals,
        cpq_ref=Decimal(norm) if norm else None, cfg_block=_PRICE_CFG, mode="active", account_id=_CAB_B)
    assert (v.decision, v.reason) == (decision, reason)
    if decision == "PAUSE" and norm == "100":
        assert v.threshold_usd == Decimal("120.00")


def test_c_price_plank_never_drops_below_floor():
    """Норма по активным занижена ($58) → планка не $70, а нижняя граница $120: квал по $72 не режется."""
    kw = dict(leads_14d=33, mature_14d=11, cfg_block=_PRICE_CFG, mode="active", account_id=_CAB_B, cpq_ref=Decimal("58"))
    ok = early_kill.evaluate_rule_c(spend_14d=Decimal("300"), quals_14d=4, **kw)
    assert (ok.decision, ok.threshold_usd) == ("KEEP", Decimal("120.00"))
    bad = early_kill.evaluate_rule_c(spend_14d=Decimal("631"), quals_14d=3, **kw)
    assert (bad.decision, bad.reason) == ("PAUSE", "quality_expensive")
    assert early_kill.evaluate_rule_c(spend_14d=Decimal("200"), quals_14d=1, **kw).reason == "spend_below_threshold"


def test_c_price_by_norm_respects_op_guard_and_old_mode_untouched():
    kw = dict(spend_14d=Decimal("240"), leads_14d=10, mature_14d=10, quals_14d=1, cpq_ref=Decimal("100"),
              mode="active", account_id=_CAB_B)
    assert early_kill.evaluate_rule_c(cfg_block=_PRICE_CFG, op_ok=False, **kw).reason == "op_load_guard"
    assert early_kill.evaluate_rule_c(cfg_block=_CFG, **kw).reason == "mature_below_min"  # прежний режим: нужно 30 зрелых


def test_b_young_ad_not_applicable():
    assert _eval_b("500.00", 5, 3, 0, age=71.9).reason == "age_below_72h"


def test_b_no_leads_is_rule_a_territory():
    assert _eval_b("500.00", 0, 0, 0).reason == "no_leads_rule_a"


def test_b_unknown_amo_waits():
    assert _eval_b("500.00", None, None, None).reason == "amo_leads_unknown"


def test_b_mature_below_min_waits():
    v = _eval_b("500.00", 5, 2, 0)
    assert v.decision == "WAIT" and v.reason == "mature_below_min"


def test_b_small_branch_one_or_two_leads():
    # 1–2 заявки, 0 квалов, порог = clamp(3 × $10, $12, $120) = $30
    v = _eval_b("30.00", 2, 1, 0)
    assert v.decision == "PAUSE" and v.reason == "small_leads_zero_quals" and v.threshold_usd == Decimal("30.00")
    assert _eval_b("29.99", 1, 0, 0).reason == "spend_below_threshold"
    assert _eval_b("500.00", 2, 0, 0, cpl=None).reason == "no_cpl_reference"


def test_b_op_guard_turns_pause_into_wait():
    v = _eval_b("25.00", 4, 3, 0, op_ok=False)
    assert v.decision == "WAIT" and v.reason == "op_load_guard"
    assert _eval_b("30.00", 2, 1, 0, op_ok=False).reason == "op_load_guard"


def test_b_disabled_or_mode_off():
    assert _eval_b("25.00", 4, 3, 0, mode="off").reason == "contour_off"
    assert _eval_b("25.00", 4, 3, 0, cfg={**_CFG, "b_enabled": False}).reason == "contour_off"


def test_b_missing_spend_is_incomplete_evidence():
    assert _eval_b(None, 4, 3, 0).reason == "insufficient_lifetime_evidence"


# ---------------------------------------------------------------------------
# Прогон на временной KB
# ---------------------------------------------------------------------------

@pytest.fixture
def kb(tmp_path):
    from services.creative_intelligence import init_kb

    path = str(tmp_path / "kb.db")
    init_kb(db_path=path)
    return path


def _ad(ad_id, name="CityA | тест", age_hours=10.0):
    return {
        "ad_id": ad_id, "name": name, "adset_id": "adset1",
        "created": _NOW - timedelta(hours=age_hours), "age_hours": age_hours,
    }


def _ref(cpl="6.30", account=_CAB_B):
    return CplReference(
        account_id=account, cpl_usd=cpl, spend_usd=630.0, leads=100, window_days=14,
        since="2026-09-01", until="2026-09-14", computed_at=_NOW.isoformat(), source="fb_account_window",
    )


class _Ctx:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


_OK = OpLoadVerdict(True, "ok", 0.4, 0.4, 50, _NOW.isoformat())
_OVERLOADED = OpLoadVerdict(False, "overloaded", 0.1, 0.4, 50, _NOW.isoformat())


def _run(kb, *, ads, lifetime, amo, cfg_override=None, refs=None, accounts=(_CAB_B,), kill_switch=False,
         op_verdict=_OK, now=_NOW, window=None, trigger="test"):
    """Прогон с моками. amo: {ad_id: {leads, mature, quals[, leads_14d, mature_14d, quals_14d]}} или None.

    window: {ad_id: {spend, impressions, fb_leads}} за 14 дней (суточный проход, trigger="daily").
    Возвращает (report, send_telegram_mock, amo_mock, mutation_mock).
    """
    autopilot_cfg = {"kill_switch": kill_switch, "early_kill": {**EARLY_KILL_DEFAULTS, **(cfg_override or {})}}
    refs = refs or {a: _ref(account=a) for a in accounts}
    ads_fn = ads if callable(ads) else (lambda account, **kw: ads.get(account, []))
    window = window or {}

    def _amo(ad_id, created, now, b_hours=72):
        s = amo.get(ad_id)
        if s is None:
            return None
        return {"leads_14d": s["leads"], "mature_14d": s["mature"], "quals_14d": s["quals"], **s}

    with patch("services.autopilot.get_autopilot_config", return_value=autopilot_cfg), \
         patch("services.launch_routing.accounts_to_scan", return_value=tuple(accounts)), \
         patch("services.fb_token_provider.fb_account", return_value=_Ctx()), \
         patch("services.fb_token_provider.offline_account_context", return_value=None), \
         patch.object(early_kill, "_fetch_young_active_ads", side_effect=ads_fn), \
         patch.object(early_kill, "_fetch_live_lifetime", side_effect=lambda ids: {i: lifetime.get(i) for i in ids}), \
         patch.object(early_kill, "_fetch_window_stats", side_effect=lambda ids, now: {i: window.get(i) for i in ids}), \
         patch("services.cpl_reference.load_cpl_references", return_value=refs), \
         patch.object(early_kill, "_fetch_amo_summary", side_effect=_amo) as amo_mock, \
         patch("services.op_load_guard.check_op_load", return_value=op_verdict), \
         patch("services.notifications.send_telegram", return_value=True) as tg, \
         patch("integrations.facebook_ads_mutation_transport.set_ad_status") as mutation:
        report = run_early_kill(now=now, trigger=trigger)
    return report, tg, amo_mock, mutation


def _rows(kb, ad_id=None, rule=None):
    conn = sqlite3.connect(kb)
    try:
        sql = "SELECT ad_id, decision, reason, amo_leads, threshold_usd, spend_usd, rule, mature_leads, quals FROM early_kill_evaluations"
        conds, args = [], []
        if ad_id:
            conds.append("ad_id = ?")
            args.append(ad_id)
        if rule:
            conds.append("rule = ?")
            args.append(rule)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        return conn.execute(sql + " ORDER BY id", args).fetchall()
    finally:
        conn.close()


def _state(kb, ad_id):
    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM early_kill_ad_state WHERE ad_id = ?", (ad_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def test_init_kb_creates_tables(kb):
    conn = sqlite3.connect(kb)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(early_kill_evaluations)")]
    state_cols = [r[1] for r in conn.execute("PRAGMA table_info(early_kill_ad_state)")]
    conn.close()
    assert {"ad_id", "decision", "reason", "rule", "mature_leads", "quals"} <= set(cols)
    assert {"ad_id", "keep_forever", "last_amo_at", "amo_quals"} <= set(state_cols)


def test_all_rules_off_skips_without_touching_anything(kb):
    report, tg, amo, mutation = _run(
        kb, ads={_CAB_B: [_ad("a1")]}, lifetime={}, amo={},
        cfg_override={"mode": "off", "b_mode": "off", "c_mode": "off", "s_mode": "off"},
    )
    assert report["ran"] is False and report["skipped"] == "mode_off"
    assert _rows(kb) == [] and not tg.called and not amo.called and not mutation.called


def test_rule_a_off_keeps_rule_b_running(kb):
    ads = {_CAB_B: [_ad("mature", age_hours=100.0)]}
    lifetime = {"mature": {"spend": 40.0, "impressions": 1, "fb_leads": 4}}
    amo = {"mature": {"leads": 4, "mature": 3, "quals": 0}}
    report, *_ = _run(kb, ads=ads, lifetime=lifetime, amo=amo, cfg_override={"mode": "off"})
    assert report["ran"] is True and report["mode"] == "off" and report["b_mode"] == "shadow"
    assert [r[6] for r in _rows(kb)] == ["B"] and report["would_pause_b"] == ["mature"]


def test_kill_switch_skips(kb):
    report, *_ = _run(kb, ads={_CAB_B: [_ad("a1")]}, lifetime={}, amo={}, kill_switch=True)
    assert report["skipped"] == "kill_switch" and _rows(kb) == []


def test_rule_a_shadow_run_records_and_queries_amo_only_above_threshold(kb):
    ads = {_CAB_B: [_ad("cheap"), _ad("hot"), _ad("alive"), _ad("dark")]}
    lifetime = {
        "cheap": {"spend": 5.0, "impressions": 500, "fb_leads": 0},     # ниже порога → AMO не спрашиваем
        "hot": {"spend": 25.0, "impressions": 4000, "fb_leads": 0},     # выше порога, 0 заявок → PAUSE
        "alive": {"spend": 40.0, "impressions": 6000, "fb_leads": 3},   # выше порога, 3 заявки → KEEP
        "dark": None,                                                    # батч упал → WAIT
    }
    amo = {"hot": {"leads": 0, "mature": 0, "quals": 0}, "alive": {"leads": 3, "mature": 0, "quals": 0}}
    report, tg, amo_mock, mutation = _run(kb, ads=ads, lifetime=lifetime, amo=amo)

    assert report["ran"] is True and report["mode"] == "shadow"
    assert report["candidates"] == 4 and report["recorded"] == 4  # все моложе 72 ч → только правило A
    assert report["amo_queries"] == 2
    assert sorted(c.args[0] for c in amo_mock.call_args_list) == ["alive", "hot"]
    assert report["would_pause"] == ["hot"] and report["would_pause_a"] == ["hot"]

    by_ad = {r[0]: r for r in _rows(kb)}
    assert by_ad["cheap"][1:4] == ("WAIT", "spend_below_threshold", None)
    assert by_ad["hot"][1:4] == ("PAUSE", "spend_cpl_threshold_reached", 0) and by_ad["hot"][4] == 18.9
    assert by_ad["alive"][1:4] == ("KEEP", "leads_present", 3)
    assert by_ad["dark"][1:3] == ("WAIT", "insufficient_lifetime_evidence")
    assert all(r[6] == "A" for r in _rows(kb))

    tg.assert_called_once()
    text = tg.call_args.args[0]
    assert "поймал бы 1" in text and "$25.00" in text and "$18.90" in text and "Ничего не выключено" in text
    assert not mutation.called


def test_rule_b_evaluated_from_72h_with_two_rows(kb):
    ads = {_CAB_B: [_ad("mature", age_hours=100.0)]}
    lifetime = {"mature": {"spend": 40.0, "impressions": 5000, "fb_leads": 4}}
    amo = {"mature": {"leads": 4, "mature": 3, "quals": 0}}
    report, tg, amo_mock, _ = _run(kb, ads=ads, lifetime=lifetime, amo=amo)

    assert amo_mock.call_count == 1  # один запрос обслужил оба правила
    rows = {r[6]: r for r in _rows(kb, "mature")}
    assert rows["A"][1:3] == ("KEEP", "leads_present")
    assert rows["B"][1:3] == ("PAUSE", "mature_zero_quals") and rows["B"][7:9] == (3, 0)
    assert report["would_pause_b"] == ["mature"] and report["would_pause_a"] == []
    text = tg.call_args.args[0]
    assert "заявки есть, квалов нет" in text and "зрелых 3" in text


def test_rule_b_small_branch_in_run(kb):
    ads = {_CAB_B: [_ad("tiny", age_hours=80.0)]}
    lifetime = {"tiny": {"spend": 20.0, "impressions": 900, "fb_leads": 1}}
    amo = {"tiny": {"leads": 1, "mature": 1, "quals": 0}}
    report, *_ = _run(kb, ads=ads, lifetime=lifetime, amo=amo)
    rows = {r[6]: r for r in _rows(kb, "tiny")}
    # A: одна заявка → порог 6.30×4.5=28.35 → WAIT; B мелкая ветка: 3×6.30=18.90 ≤ 20 → PAUSE
    assert rows["A"][1] == "WAIT"
    assert rows["B"][1:3] == ("PAUSE", "small_leads_zero_quals")
    assert report["would_pause_b"] == ["tiny"]


def test_qual_marks_keep_forever_and_skips_amo_next_run(kb):
    ads = {_CAB_B: [_ad("good", age_hours=100.0)]}
    lifetime = {"good": {"spend": 60.0, "impressions": 5000, "fb_leads": 5}}
    _, _, amo1, _ = _run(kb, ads=ads, lifetime=lifetime, amo={"good": {"leads": 5, "mature": 3, "quals": 2}})
    assert amo1.call_count == 1
    st = _state(kb, "good")
    assert st["keep_forever"] == 1 and st["amo_quals"] == 2 and st["first_qual_seen_at"]

    report, tg, amo2, _ = _run(kb, ads=ads, lifetime=lifetime, amo={}, now=_NOW + timedelta(hours=2))
    assert amo2.call_count == 0 and report["amo_cached"] == 1
    rows = {r[6]: r for r in _rows(kb, "good")[-2:]}
    assert rows["A"][1:3] == ("KEEP", "quals_present") and rows["B"][1:3] == ("KEEP", "quals_present")
    assert not tg.called


def test_ladder_removes_first_qual_immunity(kb):
    """С лестницей объявление с квалом судится дальше: 1 квал на 10 зрелых → PAUSE, AMO спрашивается заново."""
    ladder = {"b_ladder": [[8, 0], [10, 1]], "b_tail_norm_share": 0.75}
    ads = {_CAB_B: [_ad("lucky", age_hours=100.0)]}
    lifetime = {"lucky": {"spend": 200.0, "impressions": 5000, "fb_leads": 9}}
    _run(kb, ads=ads, lifetime=lifetime, amo={"lucky": {"leads": 9, "mature": 9, "quals": 1}}, cfg_override=ladder)
    assert _state(kb, "lucky")["keep_forever"] == 1
    assert {r[6]: r for r in _rows(kb, "lucky")}["B"][1:3] == ("KEEP", "quals_present")

    _, _, amo2, _ = _run(kb, ads=ads, lifetime=lifetime, amo={"lucky": {"leads": 11, "mature": 10, "quals": 1}},
                         cfg_override=ladder, now=_NOW + timedelta(hours=2))
    assert amo2.call_count == 1  # иммунитет снят — кэш «квал есть» не подменяет свежий запрос
    assert {r[6]: r for r in _rows(kb, "lucky")[-2:]}["B"][1:3] == ("PAUSE", "ladder_low_quals")


def test_fresh_amo_state_reused_within_ttl(kb):
    ads = {_CAB_B: [_ad("hot", age_hours=10.0)]}
    lifetime = {"hot": {"spend": 25.0, "impressions": 1, "fb_leads": 0}}
    _run(kb, ads=ads, lifetime=lifetime, amo={"hot": {"leads": 0, "mature": 0, "quals": 0}})
    report, _, amo2, _ = _run(kb, ads=ads, lifetime=lifetime, amo={}, now=_NOW + timedelta(minutes=20))
    assert amo2.call_count == 0 and report["amo_cached"] == 1
    assert _rows(kb, "hot")[-1][1] == "PAUSE"  # кэш ≤ 50 мин даёт те же нули
    report3, _, amo3, _ = _run(kb, ads=ads, lifetime=lifetime, amo={"hot": {"leads": 1, "mature": 0, "quals": 0}},
                               now=_NOW + timedelta(minutes=70))
    assert amo3.call_count == 1 and _rows(kb, "hot")[-1][1] == "WAIT"  # свежая заявка → порог ×4.5


def test_op_guard_blocks_only_rule_b(kb):
    ads = {_CAB_B: [_ad("zero", age_hours=100.0), _ad("mature", age_hours=100.0)]}
    lifetime = {
        "zero": {"spend": 25.0, "impressions": 1, "fb_leads": 0},
        "mature": {"spend": 40.0, "impressions": 1, "fb_leads": 4},
    }
    amo = {"zero": {"leads": 0, "mature": 0, "quals": 0}, "mature": {"leads": 4, "mature": 3, "quals": 0}}
    report, *_ = _run(kb, ads=ads, lifetime=lifetime, amo=amo, op_verdict=_OVERLOADED)
    assert report["op_guard"]["reason"] == "overloaded"
    rows_zero = {r[6]: r for r in _rows(kb, "zero")}
    rows_mature = {r[6]: r for r in _rows(kb, "mature")}
    assert rows_zero["A"][1] == "PAUSE"                                  # правило A стража не знает
    assert rows_mature["B"][1:3] == ("WAIT", "op_load_guard")
    assert report["would_pause"] == ["zero"]


def test_op_guard_disabled_skips_check(kb):
    ads = {_CAB_B: [_ad("mature", age_hours=100.0)]}
    lifetime = {"mature": {"spend": 40.0, "impressions": 1, "fb_leads": 4}}
    amo = {"mature": {"leads": 4, "mature": 3, "quals": 0}}
    report, *_ = _run(kb, ads=ads, lifetime=lifetime, amo=amo, cfg_override={"op_guard_enabled": False},
                      op_verdict=_OVERLOADED)  # страж отдал бы «завал», но он выключен → не вызывается
    assert report["op_guard"] is None and report["would_pause_b"] == ["mature"]


def test_shadow_notifies_once_per_ad_and_rule_across_runs(kb):
    ads = {_CAB_B: [_ad("hot")]}
    lifetime = {"hot": {"spend": 25.0, "impressions": 1, "fb_leads": 0}}
    amo = {"hot": {"leads": 0, "mature": 0, "quals": 0}}
    _, tg1, *_ = _run(kb, ads=ads, lifetime=lifetime, amo=amo)
    report2, tg2, *_ = _run(kb, ads=ads, lifetime=lifetime, amo=amo, now=_NOW + timedelta(hours=1))
    assert tg1.call_count == 1 and tg2.call_count == 0
    assert [r[1] for r in _rows(kb, "hot")] == ["PAUSE", "PAUSE"]
    assert report2["would_pause"] == ["hot"]


def test_shadow_daily_cap_does_not_rewrite_journal(kb):
    """Лимит дня — только на исполнение: в тени все PAUSE остаются PAUSE."""
    ads = {_CAB_B: [_ad("h1"), _ad("h2"), _ad("h3")]}
    lifetime = {i: {"spend": 30.0, "impressions": 1, "fb_leads": 0} for i in ("h1", "h2", "h3")}
    amo = {i: {"leads": 0, "mature": 0, "quals": 0} for i in ("h1", "h2", "h3")}
    report, tg, *_ = _run(kb, ads=ads, lifetime=lifetime, amo=amo, cfg_override={"max_per_day": 2})
    assert [r[1] for r in _rows(kb)] == ["PAUSE", "PAUSE", "PAUSE"]
    assert _actions(kb) == {"h1": "shadow", "h2": "shadow", "h3": "shadow"}
    assert len(report["would_pause"]) == 3 and "поймал бы 3" in tg.call_args.args[0]


def test_rule_b_excluded_by_marker_in_run(kb):
    ads = {_CAB_B: [_ad("intl", name="intl_line | Вариант 4 с ценой v2", age_hours=100.0)]}
    lifetime = {"intl": {"spend": 200.0, "impressions": 1, "fb_leads": 6}}
    amo = {"intl": {"leads": 6, "mature": 3, "quals": 0}}
    report, tg, amo_mock, _ = _run(kb, ads=ads, lifetime=lifetime, amo=amo)
    rows = {r[6]: r for r in _rows(kb, "intl")}
    assert rows["B"][1:3] == ("WAIT", "excluded_by_marker")
    assert report["would_pause_b"] == [] and not tg.called
    # правило A по intl_line работает: расход выше порога → AMO спрошен, 6 заявок → KEEP
    assert amo_mock.call_count == 1 and rows["A"][1:3] == ("KEEP", "leads_present")


def test_amo_unavailable_waits_not_pauses(kb):
    ads = {_CAB_B: [_ad("hot", age_hours=100.0)]}
    lifetime = {"hot": {"spend": 25.0, "impressions": 1, "fb_leads": 2}}
    report, tg, *_ = _run(kb, ads=ads, lifetime=lifetime, amo={})  # _fetch_amo_summary → None
    rows = {r[6]: r for r in _rows(kb, "hot")}
    assert rows["A"][1:3] == ("WAIT", "amo_leads_unknown") and rows["B"][1:3] == ("WAIT", "amo_leads_unknown")
    assert report["would_pause"] == [] and not tg.called and _state(kb, "hot") is None


def test_run_without_cpl_reference_waits_and_skips_amo(kb):
    ads = {_CAB_B: [_ad("hot")]}
    lifetime = {"hot": {"spend": 250.0, "impressions": 1, "fb_leads": 0}}
    no_ref = CplReference(
        account_id=_CAB_B, cpl_usd=None, spend_usd=10.0, leads=2, window_days=14,
        since="2026-09-01", until="2026-09-14", computed_at=_NOW.isoformat(), source="insufficient_leads",
    )
    report, tg, amo, _ = _run(kb, ads=ads, lifetime=lifetime, amo={"hot": {"leads": 0, "mature": 0, "quals": 0}}, refs={_CAB_B: no_ref})
    assert _rows(kb, "hot")[0][1:3] == ("WAIT", "no_cpl_reference")
    assert not amo.called and not tg.called


def test_failed_account_does_not_stop_other_accounts(kb):
    def _ads(account, **kw):
        if account == _CABINET_A:
            raise RuntimeError("FB down")
        return [_ad("hot")]

    lifetime = {"hot": {"spend": 25.0, "impressions": 1, "fb_leads": 0}}
    report, tg, *_ = _run(kb, ads=_ads, lifetime=lifetime, amo={"hot": {"leads": 0, "mature": 0, "quals": 0}}, accounts=(_CABINET_A, _CAB_B))
    assert report["ran"] is True and report["failed_accounts"] == [_CABINET_A]
    assert report["would_pause"] == ["hot"] and tg.call_count == 1


# ---------------------------------------------------------------------------
# Бой правила A (mode=active): предложение + самоодобрение, без прямой мутации
# ---------------------------------------------------------------------------

class _Receipt:
    def __init__(self, proposal_id, deduplicated=False):
        self.proposal_id = proposal_id
        self.deduplicated = deduplicated


class _Outcome:
    def __init__(self, receipt, action="PROPOSAL_CREATED", reason=None):
        self.receipt = receipt
        self.action = action
        self.reason = reason


def _run_active(kb, *, ads, lifetime, amo, propose=None, approve=True, cfg_override=None, now=_NOW,
                window=None, trigger="test"):
    """Прогон в бою с замоканным гейтвеем и самоодобрением. Возвращает (report, моки)."""
    from services.action_producer_gateway import ProducerActionError  # noqa: F401 — тип нужен side_effect'ам

    counter = {"n": 0}

    def _propose(ad_id, **kw):
        counter["n"] += 1
        if propose is not None:
            result = propose(ad_id, **kw)
            if isinstance(result, Exception):
                raise result
            return result
        return _Outcome(_Receipt(f"prop-{ad_id}"))

    patches = {
        "propose": patch("services.action_producer_gateway.propose_pause", side_effect=_propose),
        "approve": patch("services.autonomous_pause.approve_early_kill", side_effect=lambda pid, **kw: approve),
        "journal": patch("services.autonomous_pause.record_autonomous_pause"),
        "buttons": patch("services.autopilot._build_pause_undo_buttons", return_value=[]),
        "send": patch("services.telegram_bot.send_with_buttons", return_value=True),
    }
    with patches["propose"] as p, patches["approve"] as a, patches["journal"] as j, \
         patches["buttons"], patches["send"] as s:
        report, tg, amo_mock, mutation = _run(
            kb, ads=ads, lifetime=lifetime, amo=amo, cfg_override={"mode": "active", **(cfg_override or {})}, now=now,
            window=window, trigger=trigger,
        )
    return report, {"propose": p, "approve": a, "journal": j, "send": s, "tg": tg, "mutation": mutation}


def _actions(kb):
    conn = sqlite3.connect(kb)
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT ad_id, action FROM early_kill_evaluations WHERE decision = 'PAUSE' ORDER BY id"
        )}
    finally:
        conn.close()


def test_active_rule_a_proposes_and_self_approves_without_direct_mutation(kb):
    ads = {_CAB_B: [_ad("hot", name="CityE | ФОМО"), _ad("cheap")]}
    lifetime = {"hot": {"spend": 91.0, "impressions": 1, "fb_leads": 0}, "cheap": {"spend": 3.0, "impressions": 1, "fb_leads": 0}}
    amo = {"hot": {"leads": 0, "mature": 0, "quals": 0}}
    report, m = _run_active(kb, ads=ads, lifetime=lifetime, amo=amo)

    assert report["mode"] == "active" and report["executed"] == ["hot"] and report["approved"] == 1
    assert m["propose"].call_count == 1
    kwargs = m["propose"].call_args.kwargs
    assert kwargs["reason_code"] == "EARLY_KILL_SPEND_CPL" and kwargs["scope"] == "early-kill:A:hot"
    assert kwargs["decision"].spend_usd == 91.0 and kwargs["decision"].leads == 0
    assert "ранний стоп" in kwargs["decision"].business_reason
    m["approve"].assert_called_once()
    assert m["approve"].call_args.args[0] == "prop-hot" and m["approve"].call_args.kwargs["rule"] == "A"
    m["journal"].assert_called_once()
    assert m["send"].call_count == 1 and "выключаю 1" in m["send"].call_args.args[0]
    assert not m["mutation"].called and not m["tg"].called  # «поймал бы» в бою не шлём
    assert _actions(kb) == {"hot": "approved"}
    conn = sqlite3.connect(kb)
    assert conn.execute("SELECT proposal_id FROM early_kill_evaluations WHERE ad_id='hot'").fetchone()[0] == "prop-hot"
    conn.close()


def test_active_rule_b_stays_shadow_while_a_executes(kb):
    ads = {_CAB_B: [_ad("zero", age_hours=100.0), _ad("mature", age_hours=100.0)]}
    lifetime = {"zero": {"spend": 40.0, "impressions": 1, "fb_leads": 0}, "mature": {"spend": 40.0, "impressions": 1, "fb_leads": 4}}
    amo = {"zero": {"leads": 0, "mature": 0, "quals": 0}, "mature": {"leads": 4, "mature": 3, "quals": 0}}
    report, m = _run_active(kb, ads=ads, lifetime=lifetime, amo=amo)
    assert report["executed"] == ["zero"] and report["would_pause_b"] == ["mature"]
    assert m["propose"].call_count == 1  # только A
    assert m["tg"].call_count == 1 and "заявки есть, квалов нет" in m["tg"].call_args.args[0]  # B — «поймал бы»
    actions = _actions(kb)
    assert actions["zero"] == "approved" and actions["mature"] == "shadow"


def test_active_daily_cap_limits_execution_only(kb):
    ads = {_CAB_B: [_ad(f"h{i}") for i in range(3)]}
    lifetime = {f"h{i}": {"spend": 30.0 + i, "impressions": 1, "fb_leads": 0} for i in range(3)}
    amo = {f"h{i}": {"leads": 0, "mature": 0, "quals": 0} for i in range(3)}
    report, m = _run_active(kb, ads=ads, lifetime=lifetime, amo=amo, cfg_override={"max_per_day": 2})
    assert report["approved"] == 2 and report["capped"] == 1
    actions = _actions(kb)
    assert sorted(actions.values()) == ["approved", "approved", "capped"]
    assert actions["h0"] == "capped"  # самый дешёвый уступает место
    assert [r[1] for r in _rows(kb)] == ["PAUSE", "PAUSE", "PAUSE"]

    # следующий час: лимит дня уже выбран, новый кандидат ждёт
    report2, m2 = _run_active(
        kb, ads={_CAB_B: [_ad("h9")]}, lifetime={"h9": {"spend": 50.0, "impressions": 1, "fb_leads": 0}},
        amo={"h9": {"leads": 0, "mature": 0, "quals": 0}}, cfg_override={"max_per_day": 2}, now=_NOW + timedelta(hours=1),
    )
    assert report2["approved"] == 0 and _actions(kb)["h9"] == "capped" and m2["propose"].call_count == 0


def test_active_previously_approved_ad_is_not_reproposed(kb):
    ads = {_CAB_B: [_ad("hot")]}
    lifetime = {"hot": {"spend": 40.0, "impressions": 1, "fb_leads": 0}}
    amo = {"hot": {"leads": 0, "mature": 0, "quals": 0}}
    _run_active(kb, ads=ads, lifetime=lifetime, amo=amo)
    report2, m2 = _run_active(kb, ads=ads, lifetime=lifetime, amo=amo, now=_NOW + timedelta(hours=1))
    assert m2["propose"].call_count == 0 and report2["approved"] == 0
    assert [a for _, a in sorted(
        sqlite3.connect(kb).execute("SELECT id, action FROM early_kill_evaluations WHERE ad_id='hot'").fetchall()
    )] == ["approved", "dedup"]


def test_active_gateway_refusal_and_dedup_receipt_are_recorded(kb):
    from services.action_producer_gateway import ProducerActionError

    ads = {_CAB_B: [_ad("last"), _ad("dup")]}
    lifetime = {i: {"spend": 40.0, "impressions": 1, "fb_leads": 0} for i in ("last", "dup")}
    amo = {i: {"leads": 0, "mature": 0, "quals": 0} for i in ("last", "dup")}

    def _propose(ad_id, **kw):
        if ad_id == "last":
            return ProducerActionError("LAST_EFFECTIVE_ACTIVE")
        return _Outcome(_Receipt("prop-dup", deduplicated=True))

    report, m = _run_active(kb, ads=ads, lifetime=lifetime, amo=amo, propose=_propose)
    actions = _actions(kb)
    assert actions["last"] == "error:LAST_EFFECTIVE_ACTIVE" and actions["dup"] == "dedup"
    assert report["approved"] == 0 and not m["approve"].called and not m["send"].called


def test_fb_leads_without_amo_never_pauses(kb):
    """FB видит лиды, AMO — нет: заявки потеряла связка, а не реклама. Ждём, не паузим."""
    ads = {_CAB_B: [_ad("lost")]}
    lifetime = {"lost": {"spend": 60.0, "impressions": 1, "fb_leads": 3}}
    amo = {"lost": {"leads": 0, "mature": 0, "quals": 0}}
    report, m = _run_active(kb, ads=ads, lifetime=lifetime, amo=amo)
    assert _rows(kb, "lost")[0][1:3] == ("WAIT", "fb_leads_without_amo")
    assert report["approved"] == 0 and m["propose"].call_count == 0


def test_active_approval_refused_leaves_ordinary_proposal(kb):
    ads = {_CAB_B: [_ad("hot")]}
    lifetime = {"hot": {"spend": 40.0, "impressions": 1, "fb_leads": 0}}
    amo = {"hot": {"leads": 0, "mature": 0, "quals": 0}}
    report, m = _run_active(kb, ads=ads, lifetime=lifetime, amo=amo, approve=False)
    assert _actions(kb) == {"hot": "proposed_only"} and report["approved"] == 0
    assert not m["journal"].called and not m["send"].called


# ---------------------------------------------------------------------------
# Волна 2c: правило C «качество на объёме», правило S «голодные», бой B
# ---------------------------------------------------------------------------

def _eval_c(spend, leads, mature, quals, *, cpq="52", mode="shadow", op_ok=True, cfg=None):
    return evaluate_rule_c(
        spend_14d=Decimal(spend) if spend is not None else None,
        leads_14d=leads, mature_14d=mature, quals_14d=quals,
        cpq_ref=Decimal(cpq) if cpq is not None else None, cfg_block=cfg or _CFG, mode=mode, op_ok=op_ok,
    )


def test_c_low_quality_and_expensive_and_ok():
    assert _eval_c("500", 40, 35, 3).reason == "quality_low"                 # 7,5% < 10%
    v = _eval_c("600", 40, 35, 5)                                             # 12,5%, $120/квал > 2×52
    assert v.decision == "PAUSE" and v.reason == "quality_expensive" and v.threshold_usd == Decimal("104.00")
    assert _eval_c("400", 40, 35, 5).reason == "quality_ok"                   # $80/квал ≤ $104
    assert _eval_c("400", 40, 35, 5, cpq=None).reason == "quality_ok"         # без эталона — только по квалу
    assert _eval_c("500", 40, 35, 0).reason == "quality_low"                  # ноль квалов на объёме


def test_c_needs_volume_and_evidence_and_respects_guard():
    assert _eval_c("500", 40, 29, 0).reason == "mature_below_min"
    assert _eval_c("500", None, None, None).reason == "amo_leads_unknown"
    assert _eval_c(None, 40, 35, 0).reason == "insufficient_window_evidence"
    assert _eval_c("500", 40, 35, 0, op_ok=False).reason == "op_load_guard"
    assert _eval_c("500", 40, 35, 0, mode="off").reason == "contour_off"


def _eval_s(age_h, spend, fb_leads, adset_active, *, cfg=None, mode="shadow"):
    return evaluate_rule_s(
        age_hours=age_h, spend_14d=Decimal(spend) if spend is not None else None, fb_leads=fb_leads,
        adset_active=adset_active, cfg_block=cfg or _CFG, mode=mode,
    )


def test_s_starving_only_when_old_cheap_leadless_and_crowded():
    assert _eval_s(100, "3.00", 0, 30).reason == "starving"
    assert _eval_s(50, "3.00", 0, 30).reason == "too_young"
    assert _eval_s(100, "3.00", 1, 30).reason == "leads_present"
    assert _eval_s(100, "15.00", 0, 30).reason == "spending"
    assert _eval_s(100, "3.00", 0, 10).reason == "adset_not_crowded"
    assert _eval_s(100, None, 0, 30).reason == "insufficient_window_evidence"


def test_daily_pass_evaluates_c_and_s_and_hourly_does_not(kb):
    ads = {_CAB_B: [_ad("old_bad", age_hours=500.0), _ad("hungry", age_hours=200.0)] + [_ad(f"f{i}", age_hours=300.0) for i in range(14)]}
    lifetime = {"old_bad": {"spend": 900.0, "impressions": 1, "fb_leads": 60}, "hungry": {"spend": 2.0, "impressions": 10, "fb_leads": 0}}
    lifetime.update({f"f{i}": {"spend": 50.0, "impressions": 1, "fb_leads": 5} for i in range(14)})
    window = {"old_bad": {"spend": 500.0, "impressions": 1, "fb_leads": 40}, "hungry": {"spend": 2.0, "impressions": 10, "fb_leads": 0}}
    window.update({f"f{i}": {"spend": 30.0, "impressions": 1, "fb_leads": 3} for i in range(14)})
    amo = {"old_bad": {"leads": 40, "mature": 35, "quals": 2}}
    # обычный час: C/S не считаются, старые объявления даже не кандидаты (их отдаёт FB только в суточном проходе)
    report, tg, amo_mock, _ = _run(kb, ads=ads, lifetime=lifetime, amo=amo, window=window)
    assert report["daily"] is False and report["would_pause_c"] == [] and report["would_pause_s"] == []
    # суточный проход
    report, tg, amo_mock, _ = _run(kb, ads=ads, lifetime=lifetime, amo=amo, window=window, trigger="daily")
    assert report["daily"] is True
    assert report["would_pause_c"] == ["old_bad"] and report["would_pause_s"] == ["hungry"]
    assert report["cpq_ref"][_CAB_B] == 50.0  # выборки <5 → фолбэк
    rows = {r[6]: r for r in _rows(kb, "old_bad")}
    assert rows["C"][1:3] == ("PAUSE", "quality_low") and rows["C"][5] == 500.0  # spend_usd = окно
    assert {r[6]: r for r in _rows(kb, "hungry")}["S"][1:3] == ("PAUSE", "starving")
    assert "квалы дорогие" in tg.call_args.args[0] and "голодные" in tg.call_args.args[0]
    # тот же день второй раз — суточный слот уже отработал
    report3, *_ = _run(kb, ads=ads, lifetime=lifetime, amo=amo, window=window, now=_NOW + timedelta(hours=1))
    assert report3["daily"] is False


def test_daily_slot_gate_by_hour_and_once_per_day(kb):
    from services.early_kill import _is_daily_slot, _save_daily_state

    cfg = {**_CFG, "daily_hour": 10}
    ten = datetime(2026, 9, 18, 5, 5, tzinfo=timezone.utc)   # 10:05 по локальному времени
    assert _is_daily_slot(ten, cfg) is True
    assert _is_daily_slot(ten + timedelta(hours=1), cfg) is False
    _save_daily_state({"last_date": "2026-09-18"})
    assert _is_daily_slot(ten, cfg) is False
    assert _is_daily_slot(ten, cfg, forced=True) is True


def test_active_rule_b_executes_when_enabled(kb):
    ads = {_CAB_B: [_ad("mature", age_hours=100.0)]}
    lifetime = {"mature": {"spend": 40.0, "impressions": 1, "fb_leads": 16}}
    amo = {"mature": {"leads": 16, "mature": 15, "quals": 0}}
    report, m = _run_active(kb, ads=ads, lifetime=lifetime, amo=amo,
                            cfg_override={"b_mode": "active", "b_min_mature_leads": 15})
    assert report["executed"] == ["mature"]
    assert m["propose"].call_args.kwargs["reason_code"] == "EARLY_KILL_MATURE_ZERO"
    assert m["approve"].call_args.kwargs["rule"] == "B"
    assert "заявки есть, квалов нет" in m["send"].call_args.args[0]


def test_active_rule_s_keeps_five_active_and_own_daily_cap(kb):
    ads = {_CAB_B: [_ad(f"h{i}", age_hours=200.0) for i in range(16)]}
    lifetime = {f"h{i}": {"spend": 1.0, "impressions": 5, "fb_leads": 0} for i in range(16)}
    window = dict(lifetime)
    report, m = _run_active(kb, ads=ads, lifetime=lifetime, amo={}, window=window, trigger="daily",
                            cfg_override={"mode": "off", "b_mode": "off", "c_mode": "off", "s_mode": "active",
                                          "s_max_per_day": 8, "s_adset_min_active": 15})
    assert len(report["would_pause_s"]) == 16
    assert report["approved"] == 8  # свой лимит S, адсет остаётся с 8 ≥ 5
    actions = list(_actions(kb).values())
    assert actions.count("approved") == 8 and actions.count("capped") == 8
    assert m["propose"].call_args.kwargs["reason_code"] == "EARLY_KILL_STARVING"


def test_run_never_raises(kb):
    with patch("services.autopilot.get_autopilot_config", side_effect=RuntimeError("boom")):
        report = run_early_kill(now=_NOW)
    assert report["ran"] is False and report["errors"]
