"""Тесты чистки слотов адсета (services/slot_cleaner.py).

Главное, что здесь проверяется: чистильщик никогда не трогает работающую
рекламу и никогда не удаляет — только архивирует давно паузнутое.
"""

from datetime import datetime, timedelta, timezone

import pytest

from services import slot_cleaner


_NOW = datetime(2026, 8, 11, 6, 0, tzinfo=timezone(timedelta(hours=5)))


def _ad(ad_id: str, *, status: str, days_ago: float, effective: str | None = None):
    updated = _NOW - timedelta(days=days_ago)
    return {
        "id": ad_id,
        "name": f"ad-{ad_id}",
        "status": status,
        "effective_status": effective or status,
        "updated_time": updated.isoformat(),
    }


# ---------------------------------------------------------------------------
# Отбор кандидатов
# ---------------------------------------------------------------------------

def test_active_ads_are_never_candidates():
    """Работающая реклама не архивируется, сколько бы ей ни было дней."""
    ads = [_ad("1", status="ACTIVE", days_ago=400)]

    assert slot_cleaner.select_candidates(ads, min_paused_days=14, now=_NOW) == []


def test_fresh_pause_is_not_candidate():
    """Паузнутое вчера трогать рано — владелец мог передумать."""
    ads = [_ad("1", status="PAUSED", days_ago=1)]

    assert slot_cleaner.select_candidates(ads, min_paused_days=14, now=_NOW) == []


def test_stale_paused_is_candidate():
    ads = [_ad("1", status="PAUSED", days_ago=30)]

    candidates = slot_cleaner.select_candidates(ads, min_paused_days=14, now=_NOW)

    assert [item["ad_id"] for item in candidates] == ["1"]


def test_adset_paused_ad_is_not_candidate():
    """Объявление стоит из-за паузы адсета — само по себе оно живое."""
    ads = [_ad("1", status="PAUSED", days_ago=90, effective="ADSET_PAUSED")]

    assert slot_cleaner.select_candidates(ads, min_paused_days=14, now=_NOW) == []


def test_already_archived_is_not_candidate():
    ads = [_ad("1", status="ARCHIVED", days_ago=90)]

    assert slot_cleaner.select_candidates(ads, min_paused_days=14, now=_NOW) == []


def test_unreadable_updated_time_is_skipped():
    """Возраст неизвестен — кандидатом не считаем (fail-closed)."""
    ads = [{"id": "1", "name": "x", "status": "PAUSED",
            "effective_status": "PAUSED", "updated_time": "не дата"}]

    assert slot_cleaner.select_candidates(ads, min_paused_days=14, now=_NOW) == []


def test_oldest_go_first():
    ads = [
        _ad("2001", status="PAUSED", days_ago=20),
        _ad("2002", status="PAUSED", days_ago=200),
        _ad("2003", status="PAUSED", days_ago=60),
    ]

    candidates = slot_cleaner.select_candidates(ads, min_paused_days=14, now=_NOW)

    assert [item["ad_id"] for item in candidates] == ["2002", "2003", "2001"]


# ---------------------------------------------------------------------------
# Мастер-ключ
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [True])
def test_master_key_enables_only_on_real_true(value):
    assert slot_cleaner.is_slot_cleaner_enabled({"slot_cleaner": {"enabled": value}})


@pytest.mark.parametrize("value", ["true", 1, "yes", "TRUE", None, 0, False])
def test_master_key_is_fail_closed(value):
    """Мусор в настройках чистку НЕ включает — только JSON-boolean true."""
    assert not slot_cleaner.is_slot_cleaner_enabled({"slot_cleaner": {"enabled": value}})


def test_broken_config_gives_disabled():
    assert not slot_cleaner.is_slot_cleaner_enabled({"slot_cleaner": "мусор"})


@pytest.mark.parametrize("value", [0, -5, "20", True, None])
def test_bad_numeric_setting_falls_back_to_default(value):
    """Мусор в числовой настройке не должен ослаблять критерий."""
    config = {"min_paused_days": value}

    assert slot_cleaner._positive_int(config, "min_paused_days") == 14


# ---------------------------------------------------------------------------
# Перечень боевых адсетов
# ---------------------------------------------------------------------------

def test_only_live_adsets_are_cleaned(monkeypatch):
    """Ключ old из discover_adsets в чистку не попадает."""
    monkeypatch.setattr(slot_cleaner, "_discover_adsets", lambda: {
        "leadgen": {"CityA": {"L2": "111", "L1": "222"}},
        "mql": {"CityB": "333"},
        "old": {"CityA": {"L2": ["999"], "L1": ["888"]}},
    })

    adsets = slot_cleaner.live_adset_ids()

    assert set(adsets) == {"111", "222", "333"}


def test_broken_discovery_gives_empty_list(monkeypatch):
    """Перечень не собрался — лучше не почистить, чем почистить не там."""
    def boom():
        raise RuntimeError("кабинет недоступен")

    monkeypatch.setattr(slot_cleaner, "_discover_adsets", boom)

    assert slot_cleaner.live_adset_ids() == {}


# ---------------------------------------------------------------------------
# Прогон
# ---------------------------------------------------------------------------

def _stub_adsets(monkeypatch, ads_by_adset):
    monkeypatch.setattr(
        slot_cleaner, "live_adset_ids",
        lambda: {adset_id: f"адсет {adset_id}" for adset_id in ads_by_adset},
    )
    monkeypatch.setattr(
        slot_cleaner, "_fetch_ads", lambda adset_id: ads_by_adset[adset_id]
    )


def test_dry_run_archives_nothing(monkeypatch):
    _stub_adsets(monkeypatch, {"111": [_ad("1", status="PAUSED", days_ago=90)]})
    archived: list[str] = []
    monkeypatch.setattr(slot_cleaner, "_archive_ad", lambda ad_id: archived.append(ad_id))

    result = slot_cleaner.run_slot_cleanup(dry_run=True, now=_NOW, cfg={})

    assert result["candidates_total"] == 1
    assert result["archived_total"] == 0
    assert archived == []


def test_active_run_without_master_key_degrades_to_plan(monkeypatch):
    """Боевой вызов при выключенном ключе не архивирует ничего."""
    _stub_adsets(monkeypatch, {"111": [_ad("1", status="PAUSED", days_ago=90)]})
    archived: list[str] = []
    monkeypatch.setattr(slot_cleaner, "_archive_ad", lambda ad_id: archived.append(ad_id))

    result = slot_cleaner.run_slot_cleanup(
        dry_run=False, now=_NOW, cfg={"slot_cleaner": {"enabled": False}}
    )

    assert archived == []
    assert result["dry_run"] is True
    assert "enabled=false" in result["skipped_reason"]


def test_active_run_archives_candidates(monkeypatch):
    _stub_adsets(monkeypatch, {"111": [
        _ad("3001", status="PAUSED", days_ago=90),
        _ad("3002", status="ACTIVE", days_ago=90),
    ]})
    archived: list[str] = []
    monkeypatch.setattr(slot_cleaner, "_archive_ad", lambda ad_id: archived.append(ad_id))

    result = slot_cleaner.run_slot_cleanup(
        dry_run=False, now=_NOW, cfg={"slot_cleaner": {"enabled": True}}
    )

    assert archived == ["3001"]
    assert result["archived_total"] == 1


def test_run_cap_limits_archived_count(monkeypatch):
    _stub_adsets(monkeypatch, {
        "111": [_ad(str(i), status="PAUSED", days_ago=90 + i) for i in range(10)],
    })
    archived: list[str] = []
    monkeypatch.setattr(slot_cleaner, "_archive_ad", lambda ad_id: archived.append(ad_id))

    result = slot_cleaner.run_slot_cleanup(
        dry_run=False, now=_NOW,
        cfg={"slot_cleaner": {"enabled": True, "max_archive_per_run": 3}},
    )

    assert len(archived) == 3
    assert result["archived_total"] == 3


def test_cap_is_shared_across_adsets(monkeypatch):
    """Потолок прогона общий, а не по адсету."""
    _stub_adsets(monkeypatch, {
        "111": [_ad("4001", status="PAUSED", days_ago=90)],
        "222": [_ad("4002", status="PAUSED", days_ago=90)],
        "333": [_ad("4003", status="PAUSED", days_ago=90)],
    })
    archived: list[str] = []
    monkeypatch.setattr(slot_cleaner, "_archive_ad", lambda ad_id: archived.append(ad_id))

    slot_cleaner.run_slot_cleanup(
        dry_run=False, now=_NOW,
        cfg={"slot_cleaner": {"enabled": True, "max_archive_per_run": 2}},
    )

    assert len(archived) == 2


def test_one_broken_adset_does_not_stop_the_run(monkeypatch):
    def fetch(adset_id):
        if adset_id == "111":
            raise RuntimeError("Facebook отказал")
        return [_ad("5001", status="PAUSED", days_ago=90)]

    monkeypatch.setattr(slot_cleaner, "live_adset_ids", lambda: {"111": "битый", "222": "живой"})
    monkeypatch.setattr(slot_cleaner, "_fetch_ads", fetch)
    archived: list[str] = []
    monkeypatch.setattr(slot_cleaner, "_archive_ad", lambda ad_id: archived.append(ad_id))

    result = slot_cleaner.run_slot_cleanup(
        dry_run=False, now=_NOW, cfg={"slot_cleaner": {"enabled": True}}
    )

    assert archived == ["5001"]
    assert len(result["errors"]) == 1


def test_failed_archive_does_not_stop_the_rest(monkeypatch):
    _stub_adsets(monkeypatch, {"111": [
        _ad("6001", status="PAUSED", days_ago=200),
        _ad("6002", status="PAUSED", days_ago=100),
    ]})
    archived: list[str] = []

    def archive(ad_id):
        if ad_id == "6001":
            raise RuntimeError("нет прав")
        archived.append(ad_id)

    monkeypatch.setattr(slot_cleaner, "_archive_ad", archive)

    result = slot_cleaner.run_slot_cleanup(
        dry_run=False, now=_NOW, cfg={"slot_cleaner": {"enabled": True}}
    )

    assert archived == ["6002"]
    assert result["archived_total"] == 1
    assert len(result["errors"]) == 1


def test_error_text_never_leaks_the_token(monkeypatch):
    """Ошибка FB несёт URL с access_token, а отчёт уходит в Telegram."""
    _stub_adsets(monkeypatch, {"111": [_ad("9001", status="PAUSED", days_ago=90)]})

    def archive(ad_id):
        raise RuntimeError(
            "403 for url: https://graph.facebook.com/v21.0/9001"
            "?access_token=EAAsecret123&status=ARCHIVED"
        )

    monkeypatch.setattr(slot_cleaner, "_archive_ad", archive)

    result = slot_cleaner.run_slot_cleanup(
        dry_run=False, now=_NOW, cfg={"slot_cleaner": {"enabled": True}}
    )
    report = slot_cleaner.format_report(result)

    assert "EAAsecret123" not in " ".join(result["errors"])
    assert "EAAsecret123" not in report


def test_every_days_falls_back_on_garbage():
    assert slot_cleaner.cleanup_every_days({"slot_cleaner": {"every_days": "три"}}) == 3
    assert slot_cleaner.cleanup_every_days({"slot_cleaner": {"every_days": 7}}) == 7


def test_occupied_slots_ignore_archived():
    ads = [
        _ad("1", status="ACTIVE", days_ago=1),
        _ad("2", status="PAUSED", days_ago=1),
        _ad("3", status="ARCHIVED", days_ago=1),
    ]

    assert slot_cleaner._occupied_slots(ads) == 2


def test_report_shows_freed_slots(monkeypatch):
    _stub_adsets(monkeypatch, {"111": [
        _ad("7001", status="PAUSED", days_ago=90),
        _ad("7002", status="ACTIVE", days_ago=5),
    ]})
    monkeypatch.setattr(slot_cleaner, "_archive_ad", lambda ad_id: None)

    result = slot_cleaner.run_slot_cleanup(
        dry_run=False, now=_NOW, cfg={"slot_cleaner": {"enabled": True}}
    )
    report = slot_cleaner.format_report(result)

    assert "освобождено 1" in report
    assert "49 из 50" in report


def test_report_when_nothing_to_clean(monkeypatch):
    _stub_adsets(monkeypatch, {"111": [_ad("8001", status="ACTIVE", days_ago=5)]})

    result = slot_cleaner.run_slot_cleanup(dry_run=True, now=_NOW, cfg={})

    assert "Нечего архивировать" in slot_cleaner.format_report(result)


def test_no_adsets_means_no_run(monkeypatch):
    monkeypatch.setattr(slot_cleaner, "live_adset_ids", lambda: {})

    result = slot_cleaner.run_slot_cleanup(dry_run=False, now=_NOW,
                                           cfg={"slot_cleaner": {"enabled": True}})

    assert result["ran"] is False
    assert result["archived_total"] == 0
