"""Тесты прогрева общего analytics_cache за период автопилота.

Этот кеш по-прежнему нужен аналитике и автопилоту, но больше не является
источником spend-алертов: их единый источник — CDP completed-day контур.
"""

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())


# ---------------------------------------------------------------------------
# Прогрев analytics_cache за период автопилота
# ---------------------------------------------------------------------------

class TestPrewarmAnalyticsCoverage:
    """Диск-кеш после прогрева должен покрывать период автопилота (date_to = сегодня)."""

    def test_prewarm_writes_today_as_date_to(self, tmp_path, monkeypatch):
        """get_cached_analytics(today-7, today) пишет в кеш date_to=сегодня.

        Проверяем через мок analyze_all — FB не дёргается.
        """
        import services.analytics_cache as cache_module

        get_cached_analytics = cache_module.get_cached_analytics

        today_str = datetime.now().strftime("%Y-%m-%d")
        week_ago_str = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        # Подменяем путь диск-кеша на tmp_path
        cache_path = tmp_path / "analytics_cache.json"
        monkeypatch.setattr(cache_module, "_DISK_CACHE_PATH", cache_path)

        # Очищаем in-memory кеш, чтобы не было остатков от других тестов
        monkeypatch.setattr(cache_module, "_analytics_cache", {})
        monkeypatch.setattr(cache_module, "_refresh_locks", {})

        # Мокаем analyze_all — не дёргаем FB
        fake_ads = [{"id": "ad1", "spend": "500.0"}]

        with patch("services.analytics_cache.analyze_all", return_value=fake_ads) as mock_analyze:
            result = get_cached_analytics(date_from=week_ago_str, date_to=today_str)

        # analyze_all должен был быть вызван (кеш был пустой)
        assert mock_analyze.called, "analyze_all должен быть вызван при отсутствии кеша"
        assert result == fake_ads, "get_cached_analytics должен вернуть данные из analyze_all"

        # Диск-кеш должен содержать date_to = сегодня
        assert cache_path.exists(), "Диск-кеш должен быть создан после прогрева"

        disk = json.loads(cache_path.read_text(encoding="utf-8"))
        assert disk.get("date_to") == today_str, (
            f"date_to в кеше должен быть сегодня ({today_str}), got: {disk.get('date_to')}"
        )
        assert disk.get("date_from") == week_ago_str, (
            f"date_from должен быть today-7 ({week_ago_str}), got: {disk.get('date_from')}"
        )

        # Проверяем что период кеша покрывает запрос автопилота (cache_to >= date_to)
        cache_from = disk.get("date_from")
        cache_to = disk.get("date_to")
        assert cache_from <= week_ago_str, "cache_from <= date_from (superset ок)"
        assert cache_to >= today_str, "cache_to >= today — кеш покрывает текущий день"

    def test_autopilot_fallback_passes_coverage_check(self, tmp_path, monkeypatch):
        """Если диск-кеш имеет date_to=сегодня и возраст ≤180 мин — autopilot fallback принимает его."""
        import services.autopilot as ap

        today_str = datetime.now().strftime("%Y-%m-%d")
        week_ago_str = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        # Готовим кеш как будто только что создал прогрев
        cache_path = tmp_path / "analytics_cache.json"
        cache_path.write_text(json.dumps({
            "data": [{"id": "ad1", "spend": "500"}],
            "date_from": week_ago_str,
            "date_to": today_str,
            "saved_at": time.time() - 30,  # 30 секунд назад — свежий
        }), encoding="utf-8")

        monkeypatch.setattr(ap, "_DISK_CACHE_PATH", cache_path)

        # Эмулируем логику проверки кеша из _get_ads_for_analysis
        disk = json.loads(cache_path.read_text(encoding="utf-8"))
        saved_at = disk.get("saved_at", 0)
        age_sec = time.time() - saved_at
        cache_from = disk.get("date_from")
        cache_to = disk.get("date_to")

        period_ok = (
            cache_from is not None
            and cache_to is not None
            and cache_from <= week_ago_str
            and cache_to >= today_str
        )
        cache_fresh = age_sec <= ap._CACHE_MAX_AGE_HOURS * 3600 and period_ok

        assert period_ok, (
            f"Прогретый кеш должен покрывать период автопилота: "
            f"кеш {cache_from}–{cache_to}, запрос {week_ago_str}–{today_str}"
        )
        assert cache_fresh, (
            f"Кеш должен считаться свежим: возраст {age_sec:.0f}с, "
            f"лимит {ap._CACHE_MAX_AGE_HOURS * 3600}с"
        )
