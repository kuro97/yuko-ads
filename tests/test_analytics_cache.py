"""
Юнит-тесты вынесенного кеша аналитики (services/analytics_cache.py, T-CACHE-1).
Лёгкие smoke-тесты модуля напрямую — не через web.app (те сценарии уже
покрыты test_disk_cache_fallback.py / test_watchdog_cache_and_prewarm.py / test_pause_during_refresh.py).
"""

import json
import threading
import time
from unittest.mock import patch

import services.analytics_cache as cache_module


class _JoinableThread(threading.Thread):
    """Обычный threading.Thread, но конструктор складывает созданный объект в
    _CREATED_THREADS — тест может дождаться (join) реального фонового рефреша
    ПОСЛЕ выхода из `with patch(...)`, не полагаясь на polling с фиксированным
    таймаутом (тот был источником гонки: на медленной/загруженной машине daemon-поток
    мог не успеть уложиться в дедлайн и «утекал» в _analytics_cache уже после того,
    как тест его почистил — ловилось только при полном прогоне tests/)."""

    _CREATED_THREADS: list["_JoinableThread"] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _JoinableThread._CREATED_THREADS.append(self)


def _join_background_refreshes():
    """Дожидается завершения всех фоновых потоков _refresh_cache, запущенных с момента
    последнего вызова, и очищает реестр."""
    for thread in _JoinableThread._CREATED_THREADS:
        thread.join(timeout=5)
    _JoinableThread._CREATED_THREADS.clear()


def _clear_cache():
    """Очищаем in-memory состояние модуля между тестами (глобальные словари сервиса)."""
    _join_background_refreshes()
    with cache_module._cache_lock:
        cache_module._analytics_cache.clear()
        cache_module._refresh_locks.clear()


def test_get_cached_analytics_fresh_cache_no_analyze_all_call(monkeypatch, tmp_path):
    """Свежий (непросроченный) cache_key -> мгновенный возврат без вызова analyze_all."""
    _clear_cache()
    monkeypatch.setattr(cache_module, "_DISK_CACHE_PATH", tmp_path / "cache.json")

    cache_key = ("2026-01-01", "2026-01-07")
    fake_data = [{"id": "ad1", "spend": "100"}]
    with cache_module._cache_lock:
        cache_module._analytics_cache[cache_key] = {"data": fake_data, "expires": time.time() + 900}

    with patch("services.analytics_cache.analyze_all") as mock_analyze:
        result = cache_module.get_cached_analytics(date_from="2026-01-01", date_to="2026-01-07")

    assert result == fake_data
    mock_analyze.assert_not_called()
    _clear_cache()


def test_get_cached_analytics_stale_returns_old_data_and_refreshes_in_background(monkeypatch, tmp_path):
    """Просроченный cache_key -> отдаём старые данные сразу, обновление уходит в фоновый поток."""
    _clear_cache()
    monkeypatch.setattr(cache_module, "_DISK_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(cache_module.threading, "Thread", _JoinableThread)

    cache_key = ("2026-01-01", "2026-01-07")
    old_data = [{"id": "ad_old"}]
    with cache_module._cache_lock:
        cache_module._analytics_cache[cache_key] = {"data": old_data, "expires": time.time() - 1}

    with patch("services.analytics_cache.analyze_all", return_value=[{"id": "ad_new"}]):
        result = cache_module.get_cached_analytics(date_from="2026-01-01", date_to="2026-01-07")
        # Дожидаемся фонового рефреша, пока мок analyze_all ещё активен (иначе поток может
        # успеть выполниться уже после выхода из with patch и полезть в реальный FB API).
        _join_background_refreshes()

    # Возврат — мгновенно старые данные (фоновый рефреш не блокирует ответ)
    assert result == old_data
    _clear_cache()


def test_get_cached_analytics_empty_falls_back_to_disk_cache(monkeypatch, tmp_path):
    """Нет in-memory кеша, но есть диск-кеш -> отдаём диск-данные мгновенно, фоновое
    обновление уходит в отдельный daemon-поток (дожидаемся его внутри мока analyze_all,
    чтобы не полезть в реальный FB API после выхода из with patch)."""
    _clear_cache()
    disk_path = tmp_path / "cache.json"
    disk_path.write_text(json.dumps({
        "data": [{"id": "disk_ad"}],
        "date_from": "2026-01-01",
        "date_to": "2026-01-07",
        "saved_at": time.time(),
    }), encoding="utf-8")
    monkeypatch.setattr(cache_module, "_DISK_CACHE_PATH", disk_path)
    monkeypatch.setattr(cache_module.threading, "Thread", _JoinableThread)

    with patch("services.analytics_cache.analyze_all", return_value=[{"id": "ad_new"}]):
        result = cache_module.get_cached_analytics(date_from="2099-01-01", date_to="2099-01-07")
        _join_background_refreshes()

    assert result == [{"id": "disk_ad"}]
    _clear_cache()


def test_get_cached_analytics_empty_cache_and_fb_error_reraises(monkeypatch, tmp_path):
    """Нет ни in-memory, ни диск-кеша, FB API кидает ошибку -> re-raise (не глотаем)."""
    _clear_cache()
    monkeypatch.setattr(cache_module, "_DISK_CACHE_PATH", tmp_path / "nonexistent.json")

    with patch("services.analytics_cache.analyze_all", side_effect=RuntimeError("FB down")):
        try:
            cache_module.get_cached_analytics(date_from="2099-01-01", date_to="2099-01-07")
            assert False, "должно было упасть с RuntimeError"
        except RuntimeError as exc:
            assert str(exc) == "FB down"
    _clear_cache()


def test_load_disk_cache_missing_file_returns_none(tmp_path):
    """Диск-кеш отсутствует -> None."""
    m = cache_module
    old_path = m._DISK_CACHE_PATH
    try:
        m._DISK_CACHE_PATH = tmp_path / "no_such_file.json"
        assert m._load_disk_cache(max_age=None) is None
        assert m._load_disk_cache() is None
    finally:
        m._DISK_CACHE_PATH = old_path


def test_save_disk_cache_then_load_roundtrip(tmp_path):
    """_save_disk_cache пишет файл, _load_disk_cache(max_age=None) читает его обратно."""
    m = cache_module
    old_path = m._DISK_CACHE_PATH
    try:
        m._DISK_CACHE_PATH = tmp_path / "roundtrip.json"
        m._save_disk_cache([{"id": "ad1"}], "2026-01-01", "2026-01-07")

        result = m._load_disk_cache(max_age=None)
        assert result is not None
        assert result["data"] == [{"id": "ad1"}]
        assert result["date_from"] == "2026-01-01"
        assert result["date_to"] == "2026-01-07"
    finally:
        m._DISK_CACHE_PATH = old_path


def test_preload_disk_cache_into_memory_populates_analytics_cache(tmp_path):
    """preload_disk_cache_into_memory() при наличии диск-кеша кладёт данные в _analytics_cache
    с уже истёкшим expires (stale-ok — сразу отдаётся, фоновое обновление на первом заходе)."""
    _clear_cache()
    m = cache_module
    old_path = m._DISK_CACHE_PATH
    try:
        m._DISK_CACHE_PATH = tmp_path / "preload.json"
        m._DISK_CACHE_PATH.write_text(json.dumps({
            "data": [{"id": "preloaded_ad"}],
            "date_from": "2026-01-01",
            "date_to": "2026-01-07",
            "saved_at": time.time() - 999999,  # старый, но preload грузит ЛЮБОЙ возраст
        }), encoding="utf-8")

        m.preload_disk_cache_into_memory()

        cache_key = ("2026-01-01", "2026-01-07")
        with m._cache_lock:
            cached = m._analytics_cache.get(cache_key)
        assert cached is not None
        assert cached["data"] == [{"id": "preloaded_ad"}]
        assert cached["expires"] < time.time()  # уже просрочен -> stale-ok
    finally:
        m._DISK_CACHE_PATH = old_path
        _clear_cache()


def test_preload_disk_cache_into_memory_no_file_does_nothing(tmp_path):
    """Нет диск-кеша -> preload не падает и не создаёт записей."""
    _clear_cache()
    m = cache_module
    old_path = m._DISK_CACHE_PATH
    try:
        m._DISK_CACHE_PATH = tmp_path / "no_such_preload.json"
        m.preload_disk_cache_into_memory()
        with m._cache_lock:
            assert m._analytics_cache == {}
    finally:
        m._DISK_CACHE_PATH = old_path
        _clear_cache()
