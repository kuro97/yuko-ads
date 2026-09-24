"""
Тесты монитора покрытия рекламой (services/coverage_monitor.py).

Мокаем только _fetch_ads_from_local_db и send_telegram.
Никаких FB/AMO-вызовов.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

_TZ = timezone(timedelta(hours=5))  # CityA


# ---------------------------------------------------------------------------
# Вспомогательные данные
# ---------------------------------------------------------------------------

def _make_ad(city: str, adset_type: str, ad_id: str = "ad1") -> dict:
    """Минимальный ad в формате _fetch_ads_from_local_db."""
    return {
        "ad_id": ad_id,
        "ad_name": f"{city} {adset_type}",
        "city": city,
        "adset_type": adset_type,
        "adset_id": "",
        "spend": 100.0,
        "leads": 2,
        "qual_pct": None,
        "romi": None,
        "cpl": 50.0,
        "ctr": 1.0,
        "hook_rate": None,
        "impressions": 1000,
        "video_p25": 0,
        "video_p100": 0,
        "video_views_3s": 0,
        "days_running": 5,
        "effective_status": "ACTIVE",
    }


# ---------------------------------------------------------------------------
# Тест 1: analyze_coverage — группа с 0 → empty
# ---------------------------------------------------------------------------

def test_analyze_coverage_empty_group():
    """Если нет активных для CityD/L2 — попадает в empty."""
    # Все города/типы есть кроме CityD/L2
    ads = []
    for city in ["CityA", "CityB", "CityC", "CityE", "Онлайн"]:
        for adset_type in ["L2", "L1"]:
            ads.append(_make_ad(city, adset_type, f"{city}-{adset_type}"))
    ads.append(_make_ad("CityD", "L1", "cityd-l1"))
    # CityD/L2 — 0 объявлений (нет в списке)

    with patch("services.shadow_report._fetch_ads_from_local_db", return_value=ads):
        import services.coverage_monitor as cm
        result = cm.analyze_coverage(min_per_group=2)

    empty_keys = [(e["city"], e["adset_type"]) for e in result["empty"]]
    assert ("CityD", "L2") in empty_keys, "CityD/L2 должен быть в empty (0 активных)"


# ---------------------------------------------------------------------------
# Тест 2: analyze_coverage — 1 объявление при пороге 2 → thin
# ---------------------------------------------------------------------------

def test_analyze_coverage_thin_group():
    """Группа с 1 активным при min_per_group=2 попадает в thin, не в empty."""
    # Только одно объявление для CityA/L1
    ads = [_make_ad("CityA", "L1", "citya-l1-1")]
    # Остальные группы — 2+ объявления (не важно, пусть пусто → empty, нам интересен thin)

    with patch("services.shadow_report._fetch_ads_from_local_db", return_value=ads):
        import services.coverage_monitor as cm
        result = cm.analyze_coverage(min_per_group=2)

    thin_keys = [(e["city"], e["adset_type"]) for e in result["thin"]]
    empty_keys = [(e["city"], e["adset_type"]) for e in result["empty"]]

    assert ("CityA", "L1") in thin_keys, "CityA/L1 (1 ад) должен быть в thin"
    assert ("CityA", "L1") not in empty_keys, "CityA/L1 не должен быть в empty"


# ---------------------------------------------------------------------------
# Тест 3: analyze_coverage — 3 объявления при пороге 2 → ok
# ---------------------------------------------------------------------------

def test_analyze_coverage_ok_group():
    """Группа с 3 активными при min_per_group=2 попадает в ok_count."""
    # 3 объявления для одной группы
    ads = [
        _make_ad("CityA", "L2", "a1"),
        _make_ad("CityA", "L2", "a2"),
        _make_ad("CityA", "L2", "a3"),
    ]

    with patch("services.shadow_report._fetch_ads_from_local_db", return_value=ads):
        import services.coverage_monitor as cm
        result = cm.analyze_coverage(min_per_group=2)

    thin_keys = [(e["city"], e["adset_type"]) for e in result["thin"]]
    empty_keys = [(e["city"], e["adset_type"]) for e in result["empty"]]

    assert ("CityA", "L2") not in thin_keys, "CityA/L2 (3 ада) не должен быть в thin"
    assert ("CityA", "L2") not in empty_keys, "CityA/L2 (3 ада) не должен быть в empty"
    assert result["ok_count"] >= 1, "ok_count должен быть >= 1"


# ---------------------------------------------------------------------------
# Тест 4: format_coverage_report — не падает на пустых данных
# ---------------------------------------------------------------------------

def test_format_coverage_report_empty_data():
    """format_coverage_report не падает на пустом словаре."""
    import services.coverage_monitor as cm

    text = cm.format_coverage_report({})
    assert isinstance(text, str), "Результат должен быть строкой"
    assert len(text) > 0, "Результат не должен быть пустой строкой"


# ---------------------------------------------------------------------------
# Тест 5: format_coverage_report — «всё норм» когда empty и thin пусты
# ---------------------------------------------------------------------------

def test_format_coverage_report_all_ok():
    """Если empty и thin пустые — форматтер показывает «покрытие в норме»."""
    import services.coverage_monitor as cm

    data = {"thin": [], "empty": [], "ok_count": 10, "by_group": {}, "generated_at": "2025-01-01"}
    text = cm.format_coverage_report(data)

    assert "норм" in text.lower() or "норме" in text.lower(), \
        "Текст должен содержать 'норм' при отсутствии проблем"
    assert "❗" not in text, "Не должно быть ❗ при отсутствии проблем"
    assert "⚠️" not in text, "Не должно быть ⚠️ при отсутствии проблем"


# ---------------------------------------------------------------------------
# Тест 6: format_coverage_report — отображает empty (❗) и thin (⚠️)
# ---------------------------------------------------------------------------

def test_format_coverage_report_with_problems():
    """Если есть empty и thin — форматтер включает соответствующие эмодзи."""
    import services.coverage_monitor as cm

    data = {
        "thin": [{"city": "CityA", "adset_type": "L1", "count": 1}],
        "empty": [{"city": "CityD", "adset_type": "L2", "count": 0}],
        "ok_count": 8,
        "by_group": {},
        "generated_at": "2025-01-01",
    }
    text = cm.format_coverage_report(data)

    assert "❗" in text, "Должен быть ❗ для empty групп"
    assert "⚠️" in text, "Должен быть ⚠️ для thin групп"
    assert "CityD" in text, "Должен упоминать CityD"
    assert "CityA" in text, "Должен упоминать CityA"


# ---------------------------------------------------------------------------
# Тест 7: гейт should_send_coverage_report — час 9 → True; другой час → False
# ---------------------------------------------------------------------------

def test_should_send_coverage_report_hour_gate():
    """Гейт должен вернуть True только при час==9 CityA и новой дате."""
    import services.coverage_monitor as cm

    now_9 = datetime(2025, 6, 25, 9, 30, tzinfo=_TZ)
    now_10 = datetime(2025, 6, 25, 10, 0, tzinfo=_TZ)
    now_8 = datetime(2025, 6, 25, 8, 59, tzinfo=_TZ)

    assert cm.should_send_coverage_report(now_9, None) is True, \
        "Час 9, last_sent=None → должен разрешить отправку"
    assert cm.should_send_coverage_report(now_10, None) is False, \
        "Час 10 → должен заблокировать"
    assert cm.should_send_coverage_report(now_8, None) is False, \
        "Час 8 → должен заблокировать"


# ---------------------------------------------------------------------------
# Тест 8: гейт should_send_coverage_report — раз в день
# ---------------------------------------------------------------------------

def test_should_send_coverage_report_once_per_day():
    """Гейт должен заблокировать повторную отправку в тот же день."""
    import services.coverage_monitor as cm

    now_9 = datetime(2025, 6, 25, 9, 15, tzinfo=_TZ)
    today_str = "2025-06-25"
    yesterday_str = "2025-06-24"

    # Уже отправлено сегодня — блокируем
    assert cm.should_send_coverage_report(now_9, today_str) is False, \
        "Повторная отправка в тот же день должна быть заблокирована"

    # Вчера отправляли — сегодня разрешаем
    assert cm.should_send_coverage_report(now_9, yesterday_str) is True, \
        "Если last_sent=вчера, сегодня должна разрешаться отправка"


# ---------------------------------------------------------------------------
# Тест: отчётный блок покрытия смотрит на ЖИВЫЕ группы адсетов
# ---------------------------------------------------------------------------
#
# Случай из эксплуатации: блок рапортовал «CityC/L2 — 0», хотя в ver4-адсете
# 50048565123529 крутилась живая ACTIVE-реклама. Причина — источник: локальная
# creative_kb не знает новых ручных адсетов владельца (adset_id там вообще нет).

_LIVE_ACCOUNT = "152882611033373"


def _adset(adset_id: str, status: str = "ACTIVE"):
    from services.coverage_repository import CoverageAdset

    return CoverageAdset(adset_id=adset_id, status=status)


class _FakeDirectory:
    """Живой каталог групп кабинета."""

    def __init__(self, groups, error=None):
        self.groups = dict(groups)
        self.error = error

    def list_group_adsets(self, account_id):
        if self.error is not None:
            raise self.error
        return self.groups


class _FakeInventory:
    """Инвентарь объявлений: сколько ACTIVE в каждом адсете."""

    def __init__(self, active_by_adset):
        self.active_by_adset = dict(active_by_adset)

    def fetch_page(self, scope, after):
        from services.coverage_repository import InventoryPage

        rows = []
        for adset in scope.adsets:
            for index in range(self.active_by_adset.get(adset.adset_id, 0)):
                rows.append(
                    {
                        "id": f"{adset.adset_id}-ad{index}",
                        "account_id": f"act_{scope.account_id}",
                        "adset_id": adset.adset_id,
                        "status": "ACTIVE",
                        "effective_status": "ACTIVE",
                    }
                )
        return InventoryPage(rows=tuple(rows), has_next=False, next_cursor=None)


def _all_groups(extra=None):
    """Все группы стража (расщеплённые города × L2/L1 + PRODB всех городов карты) с одним адсетом."""
    from services.coverage_monitor import TRACKED_GROUPS

    groups = {}
    for city, adset_type in TRACKED_GROUPS:
        groups[(city, adset_type)] = (_adset(f"{city}-{adset_type}"),)
    groups.update(extra or {})
    return groups


def test_live_overview_sees_new_manual_adset_local_db_does_not():
    """Ручной адсет владельца с живым ACTIVE не попадает в empty."""
    from services.coverage_guard import live_coverage_overview

    groups = _all_groups({("CityC", "L2"): (_adset("50048565123529"),)})
    active = {adset[0].adset_id: 2 for adset in groups.values()}
    active["50048565123529"] = 1  # один живой ACTIVE в новом ручном адсете

    with patch("config.FB_ACCOUNT_ID", _LIVE_ACCOUNT):
        data = live_coverage_overview(
            client=_FakeInventory(active),
            directory=_FakeDirectory(groups),
            now=datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc),
        )

    assert data["source"] == "live"
    assert data["fetch_complete"] is True
    assert data["by_group"]["CityC/L2"] == 1
    assert all(entry["city"] != "CityC" for entry in data["empty"])
    assert {"city": "CityC", "adset_type": "L2", "count": 1} in data["thin"]
    # PRODB-группы под наблюдением и в норме при полном покрытии.
    assert data["by_group"]["CityF/PRODB"] == 2
    assert data["empty"] == []
    assert data["unknown"] == []

    # А локальная база на тех же данных видит ноль — ровно прод-претензия.
    from services.coverage_monitor import analyze_coverage

    with patch("services.shadow_report._fetch_ads_from_local_db", return_value=[]):
        local = analyze_coverage()
    assert {"city": "CityC", "adset_type": "L2", "count": 0} in local["empty"]


def test_live_overview_reports_unreadable_group_as_unknown_not_zero():
    """Нечитаемая группа — UNKNOWN, не «0 активных» и не «в норме»."""
    from services.coverage_guard import live_coverage_overview
    from services.coverage_monitor import format_coverage_report

    groups = _all_groups()
    groups[("CityE", "L1")] = ()  # состав не обнаружен → NO_ADSETS_DISCOVERED
    active = {
        adset[0].adset_id: 3 for adset in groups.values() if adset
    }

    with patch("config.FB_ACCOUNT_ID", _LIVE_ACCOUNT):
        data = live_coverage_overview(
            client=_FakeInventory(active),
            directory=_FakeDirectory(groups),
            now=datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc),
        )

    assert data["fetch_complete"] is False
    assert {"city": "CityE", "adset_type": "L1", "count": None} in data["unknown"]
    assert data["empty"] == []

    text = format_coverage_report(data)
    assert "❓ CityE/L1 — инвентарь не прочитан" in text
    assert "Покрытие в норме" not in text


def test_report_block_uses_live_source_when_available():
    """collect_coverage_overview идёт в живой каталог, а не в локальную БД."""
    from services import coverage_monitor

    live = {
        "thin": [], "empty": [], "unknown": [], "ok_count": 10,
        "by_group": {}, "generated_at": "2026-07-28T21:00:00+05:00",
        "source": "live", "fetch_complete": True,
    }
    with (
        patch("services.coverage_guard.live_coverage_overview", return_value=live),
        patch("services.shadow_report._fetch_ads_from_local_db") as local_db,
    ):
        data = coverage_monitor.collect_coverage_overview()

    assert data["source"] == "live"
    local_db.assert_not_called()
    assert "локальной базе" not in coverage_monitor.format_coverage_report(data)


def test_report_block_falls_back_to_local_db_with_visible_marker():
    """Live недоступен → локальная база, но пометка в тексте обязательна."""
    from services import coverage_monitor

    with (
        patch(
            "services.coverage_guard.live_coverage_overview",
            side_effect=RuntimeError("FB недоступен"),
        ),
        patch("services.shadow_report._fetch_ads_from_local_db", return_value=[]),
    ):
        data = coverage_monitor.collect_coverage_overview()

    assert data["source"] == "local"
    text = coverage_monitor.format_coverage_report(data)
    assert "<i>Данные по локальной базе (может отставать)</i>" in text
    # Формат самого блока не изменился — заголовок и строки групп на месте.
    assert text.startswith("📊 <b>Покрытие рекламой</b>")
    assert "❗ CityE/L2 — 0 активных" in text
    # PRODB-группы тоже под наблюдением локального анализа.
    assert "❗ CityF/PRODB — 0 активных" in text


def test_analyze_coverage_tracks_prodb_groups():
    """PRODB-объявления считаются в свою группу (город, PRODB), а CityF — только PRODB."""
    import services.coverage_monitor as cm

    ads = [
        _make_ad("CityA", "PRODB", "cta-prodb-1"),
        _make_ad("CityA", "PRODB", "cta-prodb-2"),
        _make_ad("CityF", "PRODB", "ctf-prodb-1"),
        # PRODA-пара CityF не под наблюдением — в группы не попадает.
        _make_ad("CityF", "L2", "ctf-l2-1"),
    ]

    with patch("services.shadow_report._fetch_ads_from_local_db", return_value=ads):
        result = cm.analyze_coverage(min_per_group=2)

    assert result["by_group"]["CityA/PRODB"] == 2
    assert result["by_group"]["CityF/PRODB"] == 1
    assert "CityF/L2" not in result["by_group"]
    assert {"city": "CityF", "adset_type": "PRODB", "count": 1} in result["thin"]
    assert ("CityA", "PRODB") not in [(e["city"], e["adset_type"]) for e in result["empty"]]
    assert cm.is_tracked_group("CityF", "PRODB") is True
    assert cm.is_tracked_group("CityF", "L2") is False
    assert len(cm.TRACKED_GROUPS) == 16
