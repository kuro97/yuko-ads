"""
Тесты поведения _load_disk_cache при разных возрастах кэша.
Проверяем: протухший (>7 дней) кэш с max_age=604800 → None,
           тот же кэш с max_age=None → данные (аварийный фоллбэк).
"""

import json
import sys
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock

# Мокаем google.genai до импорта web.app — пакет отсутствует в dev-окружении
if "google.genai" not in sys.modules:
    sys.modules["google"] = MagicMock()
    sys.modules["google.genai"] = MagicMock()


# --- вспомогательная функция (копия логики из web/app.py, тестируем напрямую) ---

def _load_disk_cache_impl(path: Path, max_age):
    """Реализация _load_disk_cache для изолированного тестирования."""
    try:
        if path.exists():
            cached = json.loads(path.read_text())
            if max_age is None:
                return cached
            age = time.time() - cached.get("saved_at", 0)
            if age < max_age:
                return cached
    except Exception:
        pass
    return None


# --- фикстура: временный файл кэша ---

@pytest.fixture
def stale_cache_file(tmp_path):
    """Создаёт файл кэша с возрастом 8 дней (старше 7-дневного лимита)."""
    cache_path = tmp_path / "analytics_cache.json"
    eight_days_ago = time.time() - 8 * 86400
    payload = {
        "data": [{"id": "ad_1", "name": "Тест реклама"}],
        "date_from": "2026-05-01",
        "date_to": "2026-05-31",
        "saved_at": eight_days_ago,
    }
    cache_path.write_text(json.dumps(payload))
    return cache_path


@pytest.fixture
def fresh_cache_file(tmp_path):
    """Создаёт файл кэша с возрастом 1 час (свежий)."""
    cache_path = tmp_path / "analytics_cache.json"
    one_hour_ago = time.time() - 3600
    payload = {
        "data": [{"id": "ad_2", "name": "Свежая реклама"}],
        "date_from": "2026-05-01",
        "date_to": "2026-05-31",
        "saved_at": one_hour_ago,
    }
    cache_path.write_text(json.dumps(payload))
    return cache_path


# --- тесты ---

class TestLoadDiskCache:
    """Тесты _load_disk_cache с разными значениями max_age."""

    def test_stale_cache_with_max_age_returns_none(self, stale_cache_file):
        """Кэш возраста 8 дней при max_age=604800 (7 дней) → None."""
        result = _load_disk_cache_impl(stale_cache_file, max_age=604800)
        assert result is None, "Устаревший кэш не должен возвращаться при max_age=7 дней"

    def test_stale_cache_with_max_age_none_returns_data(self, stale_cache_file):
        """Кэш возраста 8 дней при max_age=None → данные (аварийный фоллбэк, лучше 502)."""
        result = _load_disk_cache_impl(stale_cache_file, max_age=None)
        assert result is not None, "При max_age=None должны вернуться данные любого возраста"
        assert result.get("data") == [{"id": "ad_1", "name": "Тест реклама"}]

    def test_fresh_cache_with_max_age_returns_data(self, fresh_cache_file):
        """Свежий кэш (1 час) при max_age=604800 → данные."""
        result = _load_disk_cache_impl(fresh_cache_file, max_age=604800)
        assert result is not None
        assert result.get("data") == [{"id": "ad_2", "name": "Свежая реклама"}]

    def test_missing_file_returns_none(self, tmp_path):
        """Отсутствующий файл → None при любом max_age."""
        missing = tmp_path / "nonexistent.json"
        assert _load_disk_cache_impl(missing, max_age=604800) is None
        assert _load_disk_cache_impl(missing, max_age=None) is None

    def test_corrupted_file_returns_none(self, tmp_path):
        """Повреждённый JSON → None (не падает с исключением)."""
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("это не json {{{")
        assert _load_disk_cache_impl(bad_file, max_age=None) is None
        assert _load_disk_cache_impl(bad_file, max_age=604800) is None

    def test_max_age_boundary_exact_seven_days(self, tmp_path):
        """Возраст ровно 7 дней (604800 сек) → None (граница не включается)."""
        cache_path = tmp_path / "analytics_cache.json"
        exactly_seven_days_ago = time.time() - 604800
        payload = {
            "data": [{"id": "ad_boundary"}],
            "saved_at": exactly_seven_days_ago,
        }
        cache_path.write_text(json.dumps(payload))
        # age == max_age → условие age < max_age не выполняется
        result = _load_disk_cache_impl(cache_path, max_age=604800)
        assert result is None

    def test_max_age_none_always_returns_regardless_of_age(self, tmp_path):
        """max_age=None → возвращает данные независимо от возраста (даже год назад)."""
        cache_path = tmp_path / "analytics_cache.json"
        one_year_ago = time.time() - 365 * 86400
        payload = {"data": [{"id": "very_old"}], "saved_at": one_year_ago}
        cache_path.write_text(json.dumps(payload))
        result = _load_disk_cache_impl(cache_path, max_age=None)
        assert result is not None
        assert result["data"] == [{"id": "very_old"}]


# --- интеграционный тест: проверяем что web.app импортирует и _load_disk_cache работает ---

class TestWebAppDiskCacheIntegration:
    """Проверяем _load_disk_cache напрямую из web.app через monkeypatch."""

    def test_web_app_load_disk_cache_stale_max_age_none(self, tmp_path, monkeypatch):
        """web.app._load_disk_cache(max_age=None) возвращает данные для 8-дневного кэша."""
        import web.app as app_module

        cache_path = tmp_path / "analytics_cache.json"
        eight_days_ago = time.time() - 8 * 86400
        payload = {
            "data": [{"id": "real_ad"}],
            "date_from": "2026-05-01",
            "date_to": "2026-05-31",
            "saved_at": eight_days_ago,
        }
        cache_path.write_text(json.dumps(payload))
        monkeypatch.setattr(app_module, "_DISK_CACHE_PATH", cache_path)

        result = app_module._load_disk_cache(max_age=None)
        assert result is not None
        assert result["data"] == [{"id": "real_ad"}]

    def test_web_app_load_disk_cache_stale_default_returns_none(self, tmp_path, monkeypatch):
        """web.app._load_disk_cache() (дефолт max_age=7 дней) → None для 8-дневного кэша."""
        import web.app as app_module

        cache_path = tmp_path / "analytics_cache.json"
        eight_days_ago = time.time() - 8 * 86400
        payload = {
            "data": [{"id": "real_ad"}],
            "saved_at": eight_days_ago,
        }
        cache_path.write_text(json.dumps(payload))
        monkeypatch.setattr(app_module, "_DISK_CACHE_PATH", cache_path)

        result = app_module._load_disk_cache()
        assert result is None
