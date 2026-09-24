"""
Кеш аналитики (stale-while-revalidate + диск-кеш).
Вынесено из web/app.py механически, без изменения логики (см. docs/specs/ARCH-phase6-engineering.md, T-CACHE-1).

Свежий кеш → мгновенно. Устаревший → отдаём старое + обновляем в фоне.
Пустой кеш → ждём первый запрос (синхронно) либо отдаём диск-фоллбек.
"""

import json
import logging
import sys
import threading
import time
from pathlib import Path

from agent.analyzer import analyze_all

logger = logging.getLogger(__name__)

# Исходные (непропатченные) значения — эталон для сравнения в _resolve_from_web_app,
# чтобы отличить «тест пропатчил локальный globals этого модуля» от «значение как при импорте».
_ORIGINAL_ANALYZE_ALL = analyze_all
_ORIGINAL_DISK_CACHE_PATH = Path(__file__).parent.parent / "data" / "analytics_cache.json"


def _resolve_from_web_app(name: str, local_value, original_value):
    """Резолвит значение с учётом ДВУХ независимых точек патча, которые используют
    существующие тесты:
    1) patch/monkeypatch НА ЭТОМ модуле (services.analytics_cache.<name>) — юнит-тесты сервиса;
    2) patch/monkeypatch на web.app.<name> — интеграционные тесты (test_autopilot,
       test_watchdog_cache_and_prewarm, test_pause_during_refresh), т.к. web.app реэкспортирует
       эти имена и патчит именно свой атрибут.
    Приоритет: если локальный globals этого модуля отличается от значения при импорте —
    значит его патчнули напрямую здесь, используем его. Иначе — смотрим web.app (если он уже
    загружен и там значение отличается от исходного). Иначе — исходное значение."""
    if local_value is not original_value:
        return local_value
    web_app = sys.modules.get("web.app")
    if web_app is not None and hasattr(web_app, name):
        web_app_value = getattr(web_app, name)
        if web_app_value is not original_value:
            return web_app_value
    return local_value


def _resolve_analyze_all():
    return _resolve_from_web_app("analyze_all", analyze_all, _ORIGINAL_ANALYZE_ALL)


def _resolve_disk_cache_path():
    return _resolve_from_web_app("_DISK_CACHE_PATH", _DISK_CACHE_PATH, _ORIGINAL_DISK_CACHE_PATH)


# Stale-while-revalidate кеш: отдаём старые данные мгновенно, обновляем в фоне
_analytics_cache = {}   # {(date_from, date_to): {data, expires}}

_DISK_CACHE_PATH = _ORIGINAL_DISK_CACHE_PATH
_DISK_CACHE_MAX_AGE_SEC = 7 * 24 * 3600  # 7 дней


def _save_disk_cache(data, date_from, date_to):
    disk_cache_path = _resolve_disk_cache_path()
    try:
        disk_cache_path.write_text(json.dumps({
            "data": data, "date_from": date_from, "date_to": date_to,
            "saved_at": time.time()
        }, ensure_ascii=False, default=str))
    except Exception as exc:
        logger.warning("Disk cache save failed: %s", exc)


def _load_disk_cache(max_age: float | None = _DISK_CACHE_MAX_AGE_SEC):
    """Грузит диск-кэш аналитики.
    max_age — макс возраст в секундах для «свежего» кэша. Если None —
    отдаём кэш ЛЮБОГО возраста (аварийный фоллбэк: старые данные лучше 502)."""
    disk_cache_path = _resolve_disk_cache_path()
    try:
        if disk_cache_path.exists():
            cached = json.loads(disk_cache_path.read_text())
            if max_age is None:
                return cached
            age = time.time() - cached.get("saved_at", 0)
            if age < max_age:
                return cached
    except Exception as exc:
        logger.warning("Disk cache load failed: %s", exc)
    return None


def preload_disk_cache_into_memory():
    """При старте — загружаем диск-кеш в память ДАЖЕ если он старый (защита от 502 после рестарта).
    expires в прошлом → данные сразу отдаются + при первом же заходе стартует фоновое обновление."""
    disk = _load_disk_cache(max_age=None)
    if disk and disk.get("data"):
        dk = (disk.get("date_from"), disk.get("date_to"))
        _analytics_cache[dk] = {"data": disk["data"], "expires": time.time() - 1}
        logger.info("Disk cache preloaded (stale-ok): %d ads, period %s - %s", len(disk["data"]), dk[0], dk[1])


_refresh_locks = {}     # {cache_key: True} — защита от двойного рефреша
_cache_lock = threading.Lock()  # синхронизация кеша и refresh_locks


def _refresh_cache(cache_key, date_from, date_to):
    """Фоновое обновление кеша (запускается в daemon thread).
    При ошибке FB API — оставляем старые данные (не убиваем кеш)."""
    try:
        result = _resolve_analyze_all()(date_from, date_to, include_paused=True)
        with _cache_lock:
            _analytics_cache[cache_key] = {"data": result, "expires": time.time() + 900}
        _save_disk_cache(result, date_from, date_to)
    except Exception as exc:
        logger.warning("Фоновое обновление кеша не удалось: %s", exc)
        # Продлеваем старый кеш на 5 минут — не отдаём ошибку пользователю
        with _cache_lock:
            cached = _analytics_cache.get(cache_key)
            if cached:
                cached["expires"] = time.time() + 300
    finally:
        with _cache_lock:
            _refresh_locks.pop(cache_key, None)


def get_cached_analytics(date_from: str = None, date_to: str = None) -> list[dict]:
    """Аналитика с кешированием (stale-while-revalidate).
    Свежий кеш → мгновенно. Устаревший → отдаём старое + обновляем в фоне.
    Пустой кеш → ждём первый запрос."""
    cache_key = (date_from, date_to)

    with _cache_lock:
        cached = _analytics_cache.get(cache_key)
        if cached:
            if time.time() < cached["expires"]:
                return cached["data"]
            # Кеш устарел — отдаём старые данные, обновляем в фоне
            if not _refresh_locks.get(cache_key):
                _refresh_locks[cache_key] = True
                threading.Thread(target=_refresh_cache, args=(cache_key, date_from, date_to), daemon=True).start()
            return cached["data"]

    # Есть ли запасной кеш (любой период или диск)?
    def _fallback_cache():
        with _cache_lock:
            for key, val in _analytics_cache.items():
                if val.get("data"):
                    return val["data"]
        # Аварийный фоллбэк — отдаём диск-кэш ЛЮБОГО возраста (старые данные лучше 502).
        # expires в прошлом → при следующем заходе основная ветка стартует фоновое обновление.
        disk = _load_disk_cache(max_age=None)
        if disk and disk.get("data"):
            with _cache_lock:
                _analytics_cache[cache_key] = {"data": disk["data"], "expires": time.time() - 1}
                # Сразу запускаем фоновое обновление — fallback может вернуться раньше основного потока
                if not _refresh_locks.get(cache_key):
                    _refresh_locks[cache_key] = True
                    threading.Thread(target=_refresh_cache, args=(cache_key, date_from, date_to), daemon=True).start()
            return disk["data"]
        return None

    # Первый запрос — запускаем в фоне, если есть кеш — отдаём сразу
    fallback = _fallback_cache()
    if fallback:
        # Есть кеш — отдаём его, обновляем в фоне (если ещё не запустили в _fallback_cache)
        logger.info("Отдаём кеш, обновление в фоне")
        with _cache_lock:
            if not _refresh_locks.get(cache_key):
                _refresh_locks[cache_key] = True
                threading.Thread(target=_refresh_cache, args=(cache_key, date_from, date_to), daemon=True).start()
        return fallback

    # Нет кеша вообще — ждём первый запрос
    try:
        result = _resolve_analyze_all()(date_from, date_to, include_paused=True)
    except Exception as exc:
        logger.warning("FB API недоступен и нет кеша: %s", exc)
        raise
    with _cache_lock:
        _analytics_cache[cache_key] = {"data": result, "expires": time.time() + 900}
    _save_disk_cache(result, date_from, date_to)
    return result
