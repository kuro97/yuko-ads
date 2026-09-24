"""Регрессии слепых зон автономных пауз.

Пять дыр, из-за которых бот не чистил сам:
1. _load_lead_events кормил хелперы сырыми v4-лидами → R1 «зрелый ноль»
   не видел НИ ОДНОГО лида (0 событий на объявлении, у которого лиды есть).
2. learner собирал только дефолтный кабинет → весь cabinet_b вне creative_kb.
3. PRODB-адсеты не классифицировались discovery → вне adset_map.
4. Класса «семья-слив по нескольким городам» не существовало.
5. Класса «дорогой без выручки» не существовало.
Плюс: lifetime-evidence фабриковал «0 лидов» объявлениям второго кабинета.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import waster_rules_v2
from services.family_waster import family_key, find_family_wasters
from services.no_revenue_waster import find_no_revenue_wasters

_TZ = timezone(timedelta(hours=5))


# ------------------------- 1. v4-лиды в правилах v2 -------------------------

def _raw_v4_lead(ad_id="111", qual=False, service=False, created_days_ago=10):
    created = int((datetime.now(_TZ) - timedelta(days=created_days_ago)).timestamp())
    fields = [
        {"field_id": 902422, "field_name": "fb_ad_id", "values": [{"value": ad_id}]},
    ]
    if qual:
        fields.append(
            {
                "field_id": 804012,
                "field_name": "Квалификация пройдена",
                "values": [{"value": "ДА"}],
            }
        )
    tags = [{"id": 1, "name": "Автосделка"}] if service else []
    return {
        "created_at": created,
        "custom_fields_values": fields,
        "_embedded": {"tags": tags},
    }


def test_load_lead_events_reads_raw_v4_leads(monkeypatch):
    """Регрессия: сырой v4-лид (custom_fields_values) должен читаться."""
    leads = [
        _raw_v4_lead(qual=True),
        _raw_v4_lead(qual=False),
        _raw_v4_lead(ad_id="999"),          # чужой fb_ad_id — мимо
        _raw_v4_lead(service=True),          # автосделка — мимо
    ]
    monkeypatch.setattr(
        "integrations.amo._amo_get",
        lambda endpoint, params=None: {"_embedded": {"leads": leads}},
    )
    events = waster_rules_v2._load_lead_events(
        "111", (datetime.now(_TZ) - timedelta(days=45)).date()
    )
    assert len(events) == 2
    assert sorted(q for _, q in events) == [False, True]


def test_load_lead_events_requests_tags(monkeypatch):
    """Без with=tags сервисный фильтр слеп: рассылки считались бы лидами."""
    captured = {}

    def _fake_get(endpoint, params=None):
        captured.update(params or {})
        return {"_embedded": {"leads": []}}

    monkeypatch.setattr("integrations.amo._amo_get", _fake_get)
    waster_rules_v2._load_lead_events("111", datetime.now(_TZ).date())
    assert "tags" in captured.get("with", "")


# ------------------------- 2. learner мультикабинетный ----------------------

def test_learner_merges_all_offline_accounts(monkeypatch):
    from agent import learner

    calls: list[str | None] = []

    monkeypatch.setattr(learner, "build_adset_map", lambda: {"as1": ("CityA", "L1")})
    monkeypatch.setattr(
        "services.launch_routing.accounts_to_scan",
        lambda: ("152882611033373", "29716040622546856"),
    )

    def _fake_get_all_ads(adset_map):
        from services.fb_token_provider import get_active_account

        ctx = get_active_account()
        calls.append(ctx)
        ad_id = "m1" if ctx is None else "w1"
        return {
            ad_id: {
                "name": f"ad {ad_id}",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
                "created_time": "2026-08-20T00:00:00+0000",
                "city": "CityA",
                "adset_type": "L1",
            }
        }

    monkeypatch.setattr(learner, "get_all_ads", _fake_get_all_ads)
    monkeypatch.setattr(
        learner, "_get_account_video_insights", lambda date_from, date_to: {}
    )

    ads = learner.get_creative_metrics(days=30)
    got_ids = {ad["ad_id"] for ad in ads}
    assert got_ids == {"m1", "w1"}
    # Дефолтный кабинет идёт без контекста, второй — через offline:<id>
    assert calls == [None, "offline:29716040622546856"]
    assert all(ad["effective_status"] == "ACTIVE" for ad in ads)


def test_learner_partial_failure_keeps_default_account(monkeypatch):
    """Отказ второго кабинета не роняет сбор — но дефолтный обязателен."""
    from agent import learner

    monkeypatch.setattr(learner, "build_adset_map", lambda: {})
    monkeypatch.setattr(
        "services.launch_routing.accounts_to_scan",
        lambda: ("152882611033373", "29716040622546856"),
    )

    def _fake_get_all_ads(adset_map):
        from services.fb_token_provider import get_active_account

        if get_active_account() is not None:
            raise RuntimeError("cabinet_b down")
        return {
            "m1": {
                "name": "ad m1",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
                "created_time": "2026-08-20T00:00:00+0000",
                "city": "CityA",
                "adset_type": "L1",
            }
        }

    monkeypatch.setattr(learner, "get_all_ads", _fake_get_all_ads)
    monkeypatch.setattr(
        learner, "_get_account_video_insights", lambda date_from, date_to: {}
    )

    ads = learner.get_creative_metrics(days=30)
    assert {ad["ad_id"] for ad in ads} == {"m1"}


def test_learner_default_account_failure_raises(monkeypatch):
    from agent import learner

    monkeypatch.setattr(learner, "build_adset_map", lambda: {})
    monkeypatch.setattr(
        "services.launch_routing.accounts_to_scan", lambda: ("152882611033373",)
    )

    def _boom(adset_map):
        raise RuntimeError("cabinet_a down")

    monkeypatch.setattr(learner, "get_all_ads", _boom)
    with pytest.raises(RuntimeError):
        learner.get_creative_metrics(days=30)


# ------------------------- 3. PRODB-адсеты в discovery ------------------------

def test_classify_adset_recognizes_prodb():
    from agent.adset_discovery import _classify_adset

    city, atype = _classify_adset("Adset | PRODB | MQL | CityB | v1")
    assert (city, atype) == ("CityB", "PRODB")


def test_classify_adset_l1_l2_unchanged():
    from agent.adset_discovery import _classify_adset

    assert _classify_adset("Adset | L1 | MQL | CityB | v3")[1] == "L1"
    assert _classify_adset("Adset | L2 | Tag | MQL | CityA | v3")[1] == "L2"


# ------------------------- 4. семья-слив ------------------------------------

def _member(ad_id, city, spend, leads, name=None, days=10):
    return {
        "ad_id": ad_id,
        "ad_name": name or f"{city} | Креатив X / Все города [PRODA]",
        "adset_id": f"as-{city}",
        "city": city,
        "spend": spend,
        "leads": leads,
        "days_running": days,
    }


def test_family_key_strips_city_prefix():
    assert family_key("CityA | PRODB Формат 4 / Тема 1.1 [PRODB]") == (
        "prodb формат 4 / тема 1.1 [prodb]"
    )
    # Сегмент с «/» — не городской префикс, имя остаётся целиком
    assert family_key("PRODA / Креатив X / CityA / Все города [PRODA]") == (
        "proda / креатив x / citya / все города [proda]"
    )


def test_family_waster_fires_across_cities():
    ads = [
        _member("a1", "CityA", 90.0, 8),
        _member("a2", "CityB", 80.0, 9),
        _member("young", "CityC", 5.0, 1, days=1),
    ]
    decisions = find_family_wasters(
        ads,
        live_qual_count=lambda ad_id: 0,
        known_age_days=lambda ad: ad.get("days_running"),
    )
    got = {d["ad_id"] for d in decisions}
    # Каждый город под порогом R1 ($150/15 лидов), семья — выше: оба зрелых режутся
    assert got == {"a1", "a2"}
    assert all(d["action"] == "PAUSE" and d["is_family_waster"] for d in decisions)


def test_family_waster_live_qual_saves_whole_family():
    ads = [_member("a1", "CityA", 90.0, 8), _member("a2", "CityB", 80.0, 9)]
    decisions = find_family_wasters(
        ads,
        live_qual_count=lambda ad_id: 1 if ad_id == "a2" else 0,
        known_age_days=lambda ad: 10,
    )
    assert decisions == []


def test_family_waster_amo_down_is_fail_closed():
    ads = [_member("a1", "CityA", 90.0, 8), _member("a2", "CityB", 80.0, 9)]
    decisions = find_family_wasters(
        ads,
        live_qual_count=lambda ad_id: None,
        known_age_days=lambda ad: 10,
    )
    assert decisions == []


def test_family_waster_single_city_not_a_family():
    ads = [_member("a1", "CityA", 200.0, 20), _member("a2", "CityA", 150.0, 16)]
    decisions = find_family_wasters(
        ads, live_qual_count=lambda ad_id: 0, known_age_days=lambda ad: 10
    )
    assert decisions == []


# ------------------------- 5. дорогой без выручки ---------------------------

def _expensive(ad_id="e1", name="Игорь / Креатив Z [PRODA]", spend=900.0, days=20):
    return {
        "ad_id": ad_id,
        "ad_name": name,
        "adset_id": "as1",
        "spend": spend,
        "days_running": days,
    }


def test_no_revenue_waster_fires():
    decisions = find_no_revenue_wasters(
        [_expensive()],
        lead_outcomes=lambda ad_id: (12, 0, 5),
        known_age_days=lambda ad: ad.get("days_running"),
    )
    assert len(decisions) == 1
    assert decisions[0]["is_no_revenue_waster"]


def test_no_revenue_waster_any_sale_saves():
    decisions = find_no_revenue_wasters(
        [_expensive()],
        lead_outcomes=lambda ad_id: (12, 1, 5),
        known_age_days=lambda ad: 20,
    )
    assert decisions == []


def test_no_revenue_waster_shields_installments():
    ads = [
        _expensive("i1", name="Рассрочка банка / Обзор / 3"),
        _expensive("i2", name="Анимация / персонаж рассрочка"),
        _expensive("i3", name="Оплата на 12 месяцев / Обзор"),
    ]
    decisions = find_no_revenue_wasters(
        ads, lead_outcomes=lambda ad_id: (12, 0, 5), known_age_days=lambda ad: 20
    )
    assert decisions == []


def test_no_revenue_waster_needs_mature_leads_and_age():
    ads = [_expensive("y1", days=5), _expensive("m1", days=20)]

    def _outcomes(ad_id):
        return (2, 0, 1)  # зрелых лидов мало

    decisions = find_no_revenue_wasters(
        ads, lead_outcomes=_outcomes, known_age_days=lambda ad: ad.get("days_running")
    )
    assert decisions == []


def test_no_revenue_waster_amo_down_is_fail_closed():
    decisions = find_no_revenue_wasters(
        [_expensive()],
        lead_outcomes=lambda ad_id: None,
        known_age_days=lambda ad: 20,
    )
    assert decisions == []


# ------------------- 6. lifetime-evidence мультикабинетный ------------------

def test_lifetime_evidence_sums_accounts_and_fails_closed(monkeypatch):
    """Лиды суммируются по кабинетам; отказ кабинета = unknown, не ноль."""
    from services import autopilot

    monkeypatch.setattr(
        autopilot, "_evidence_accounts", lambda: ("111", "222"), raising=True
    )
    monkeypatch.setattr(
        "services.fb_token_provider.offline_account_context",
        lambda account_id: None if account_id == "111" else f"offline:{account_id}",
    )
    monkeypatch.setattr("services.fb_token_provider.get_fb_token", lambda: "t")
    monkeypatch.setattr("services.fb_token_provider.get_fb_account_id", lambda: "x")

    class _Resp:
        status_code = 200

        def __init__(self, rows):
            self._rows = rows

        def json(self):
            return {"data": self._rows, "paging": None}

    def _fake_get(url, params=None):
        from services.fb_token_provider import get_active_account

        if get_active_account() is None:
            return _Resp([{"ad_id": "adm", "actions": [{"action_type": "lead", "value": "7"}]}])
        return _Resp([{"ad_id": "adw", "actions": [{"action_type": "lead", "value": "3"}]}])

    monkeypatch.setattr("agent.fb_common._throttled_get", _fake_get)

    result = autopilot._fetch_live_lifetime_lead_evidence(["adm", "adw", "ghost"])
    assert result == {"adm": 7, "adw": 3, "ghost": 0}


def test_lifetime_evidence_account_failure_leaves_unknown(monkeypatch):
    from services import autopilot

    monkeypatch.setattr(autopilot, "_evidence_accounts", lambda: ("111", "222"))
    monkeypatch.setattr(
        "services.fb_token_provider.offline_account_context",
        lambda account_id: None if account_id == "111" else f"offline:{account_id}",
    )
    monkeypatch.setattr("services.fb_token_provider.get_fb_token", lambda: "t")
    monkeypatch.setattr("services.fb_token_provider.get_fb_account_id", lambda: "x")

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"ad_id": "adm", "actions": [{"action_type": "lead", "value": "7"}]}], "paging": None}

    def _fake_get(url, params=None):
        from services.fb_token_provider import get_active_account

        if get_active_account() is None:
            return _Resp()
        raise RuntimeError("cabinet_b insights down")

    monkeypatch.setattr("agent.fb_common._throttled_get", _fake_get)

    result = autopilot._fetch_live_lifetime_lead_evidence(["adm", "adw"])
    # Кабинет 222 упал: НИКОМУ ноль не фабрикуется, всё unknown
    assert result == {"adm": None, "adw": None}


# ------------------- 7. мультикабинетные сборщики KB ------------------------

def test_status_scan_failure_of_any_account_returns_cache(monkeypatch):
    """Отказ одного кабинета = старый кеш целиком, не частичный скан
    (частичный скан у spend_refresh превращается в ложные DELETED)."""
    from agent import analyzer

    analyzer._status_cache["data"] = {"old1": "ACTIVE"}
    analyzer._status_cache["ts"] = 1.0
    monkeypatch.setattr(analyzer, "_status_accounts", lambda: ("111", "222"))
    monkeypatch.setattr(
        "services.fb_token_provider.offline_account_context",
        lambda account_id: None if account_id == "111" else f"offline:{account_id}",
    )

    def _per_account():
        from services.fb_token_provider import get_active_account

        if get_active_account() is None:
            return {"m1": "ACTIVE"}
        return None  # второй кабинет не отдал полный скан

    monkeypatch.setattr(analyzer, "_fetch_account_statuses", _per_account)
    assert analyzer._fetch_statuses_now() == {"old1": "ACTIVE"}


def test_status_scan_merges_accounts(monkeypatch):
    from agent import analyzer

    analyzer._status_cache["data"] = {}
    monkeypatch.setattr(analyzer, "_status_accounts", lambda: ("111", "222"))
    monkeypatch.setattr(
        "services.fb_token_provider.offline_account_context",
        lambda account_id: None if account_id == "111" else f"offline:{account_id}",
    )

    def _per_account():
        from services.fb_token_provider import get_active_account

        if get_active_account() is None:
            return {"m1": "ACTIVE"}
        return {"w1": "PAUSED"}

    monkeypatch.setattr(analyzer, "_fetch_account_statuses", _per_account)
    assert analyzer._fetch_statuses_now() == {"m1": "ACTIVE", "w1": "PAUSED"}


def test_sync_recent_ads_covers_all_accounts(monkeypatch, tmp_path):
    from services import creative_backfill as cb
    from services.creative_intelligence import init_kb

    db = tmp_path / "kb.db"
    init_kb(str(db))
    monkeypatch.setattr(
        "services.creative_intelligence.DB_PATH", str(db), raising=False
    )
    monkeypatch.setattr(
        "services.launch_routing.accounts_to_scan", lambda: ("111", "222")
    )
    monkeypatch.setattr(
        "services.fb_token_provider.offline_account_context",
        lambda account_id: None if account_id == "111" else f"offline:{account_id}",
    )
    monkeypatch.setattr(cb, "build_adset_map", lambda: {})
    monkeypatch.setattr(cb, "_fetch_lifetime_insights", lambda ids: {})
    monkeypatch.setattr(cb, "_fetch_creative_fields_batch", lambda ids: {})

    def _per_account_ads(days=7):
        from services.fb_token_provider import get_active_account

        if get_active_account() is None:
            return [{"id": "m1", "name": "ad m1", "status": "ACTIVE",
                     "effective_status": "ACTIVE",
                     "created_time": "2026-08-30T00:00:00+0000"}]
        return [{"id": "w1", "name": "ad w1", "status": "ACTIVE",
                 "effective_status": "ACTIVE",
                 "created_time": "2026-08-30T00:00:00+0000"}]

    monkeypatch.setattr(cb, "_fetch_recent_ads", _per_account_ads)

    result = cb.sync_recent_ads(days=7)
    assert result["fetched"] == 2
    assert result["upserted"] == 2


# ------------- 8. кабинет объявления в exact-контексте ---------------------

def test_exact_ad_context_carries_account_id(monkeypatch):
    """Регрессия: PAUSE-предложение cabinet_b-объявления создавалось с дефолтным
    кабинетом (cabinet_a) — live-review исполнения вечно падал на
    FB_ADSET_ACCOUNT_NOT_REQUESTED. Контекст обязан нести кабинет объявления."""
    from services import adset_pause_guard as guard

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "111": {
                    "id": "111",
                    "name": "ad",
                    "adset_id": "as1",
                    "account_id": "act_29716040622546856",
                    "status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
            }

    captured = {}

    def _fake_get(url, params=None):
        captured.update(params or {})
        return _Resp()

    monkeypatch.setattr("agent.fb_common._throttled_get", _fake_get)
    monkeypatch.setattr("services.fb_token_provider.get_fb_token", lambda: "t")

    contexts, error = guard.fetch_exact_ad_contexts(["111"])
    assert error is None
    assert contexts["111"]["account_id"] == "29716040622546856"
    assert "account_id" in captured.get("fields", "")
    # Канонический live-контекст поле НЕ несёт — подписи инвентаря не меняются
    assert "account_id" not in guard._canonical_live_context(contexts["111"])


def test_producer_account_id_prefers_context():
    from services.action_producer_gateway import _account_id

    assert _account_id({"account_id": "29716040622546856"}) == "29716040622546856"


# --------- 9. тир-A и цикл оплаты (шторм ложных пауз cabinet_b) --------------

def _tier_a_ad(days_running, ad_id="ta1"):
    return {
        "ad_id": ad_id,
        "ad_name": "CityB | Креатив Y [PRODB]",
        "adset_id": "as1",
        "spend": 1200.0,
        "leads": 60,
        "qual_pct": 20.0,
        "payments": 0,
        "outcomes_matched_at": "2026-09-01 00:00:00",
        "days_running": days_running,
        "cpl": 20.0,
        "ctr": 1.0,
        "impressions": 10000,
    }


def test_tier_a_respects_payment_maturity():
    """5-дневная реклама с нулём оплат — физика цикла (80% за 14 дн.), не слив."""
    from services.decision_policy import score_and_decide

    young, mature = _tier_a_ad(5, "young"), _tier_a_ad(20, "mature")
    by_id = {d["ad_id"]: d for d in score_and_decide([young, mature])}
    assert by_id["young"]["is_confirmed_waster"] is False
    assert by_id["mature"]["is_confirmed_waster"] is True


def test_tier_a_unknown_age_is_not_candidate():
    from services.decision_policy import score_and_decide

    ad = _tier_a_ad(None, "noage")
    ad.pop("days_running")
    result = score_and_decide([ad])[0]
    assert result["is_confirmed_waster"] is False
