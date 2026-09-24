"""Тест: POST /api/analytics/{id}/pause отвечает быстро пока /api/funnel висит в analyze_all.

Воспроизводит баг ДО фикса:
  /api/funnel вызывал get_cached_analytics() синхронно внутри async-хендлера
  → холодный ключ → analyze_all(sleep 2) крутится ПРЯМО на event loop
  → event loop заморожен на 2 сек
  → pause ждёт ~2 сек вместо ~0.

Фикс: /api/funnel переведён на `await _cached_analytics_async(...)` (выделенный пул потоков)
  → analyze_all уходит в пул, event loop свободен
  → pause отвечает < 1 сек.

Механика теста:
  create_task(funnel_url) — холодный ключ + _load_disk_cache=None → попадёт в analyze_all.
  Сразу после create_task ставим t0 и делаем await pause.
  При await pause event loop передаёт управление funnel_task.
  На базе (синхронный вызов): funnel_task блокирует loop на 2с → pause ждёт → elapsed ~2с.
  С фиксом (пул потоков): funnel_task сразу отпускает loop → pause мгновенно → elapsed <1с.
"""

import asyncio
import time
import uuid

import httpx
import pytest
from httpx import ASGITransport

import web.app as app_module
from web.app import app
from tests.conftest import TEST_API_KEY
from services.adset_pause_guard import PauseOutcome
from tests.gateway_test_helpers import proposal_outcome


def _slow_analyze_all(*args, **kwargs):
    """Имитирует медленный FB API: time.sleep(2) — отпускает GIL, но не event loop
    (если вызван синхронно в async-хендлере — замораживает event loop на 2 сек)."""
    time.sleep(2)
    return []


@pytest.fixture(autouse=True)
def _patch_all(monkeypatch):
    """Патчим все внешние зависимости."""
    # analyze_all — медленный FB API (2 сек)
    monkeypatch.setattr(app_module, "analyze_all", _slow_analyze_all)

    # safety guard — мгновенный подтверждённый успех
    monkeypatch.setattr(
        app_module,
        "safe_pause_ad",
        lambda ad_id, source: PauseOutcome(
            True, "paused", ad_id, "100", ("replacement",)
        ),
    )

    # decisions_repo.save_decision — no-op (не ходим в SQLite)
    monkeypatch.setattr(app_module.decisions_repo, "save_decision", lambda *a, **kw: None)

    # notify — no-op (не пишем в Supabase/TG)
    monkeypatch.setattr(app_module, "notify", lambda *a, **kw: None)
    # producer-граница — мгновенно созданное предложение владельцу (approval-first:
    # эндпоинт не паузит сам, поэтому «мгновенный успех» здесь = 202 proposal_created).
    monkeypatch.setattr(
        "services.action_producer_gateway.execute_pause",
        lambda *args, **kwargs: proposal_outcome(),
    )

    # _load_disk_cache → None: нет disk-fallback, холодный ключ 2099 реально пойдёт в analyze_all
    monkeypatch.setattr(app_module, "_load_disk_cache", lambda *a, **kw: None)

    # Чистим кэш — гарантируем холодный старт
    with app_module._cache_lock:
        app_module._analytics_cache.clear()
        app_module._refresh_locks.clear()

    yield

    # Чистим после теста
    with app_module._cache_lock:
        app_module._analytics_cache.clear()
        app_module._refresh_locks.clear()


@pytest.mark.asyncio
async def test_pause_responds_fast_during_funnel_refresh():
    """Pause отвечает < 1 сек пока /api/funnel выполняет холодный запрос (analyze_all 2 сек).

    На базе без фикса: funnel синхронно вызывает analyze_all на event loop
    → loop заморожен → pause ждёт ~2 сек → тест ПАДАЕТ.

    С фиксом (_cached_analytics_async через пул потоков):
    → loop свободен → pause < 1 сек → тест ПРОХОДИТ.
    """
    loop = asyncio.get_event_loop()
    api_key_headers = {"X-API-Key": TEST_API_KEY}

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:

        # Холодный ключ 2099 + _load_disk_cache=None → funnel_task дойдёт до analyze_all(sleep 2).
        # Запускаем task БЕЗ await — он встанет в очередь event loop.
        funnel_task = asyncio.create_task(
            client.get(
                "/api/funnel?date_from=2099-01-01&date_to=2099-01-07",
                headers=api_key_headers,
            )
        )

        # Сразу замеряем и отправляем pause.
        # await pause передаёт управление event loop → funnel_task получает ход.
        # На базе: funnel_task блокирует loop на 2с (синхронный sleep в хендлере).
        # С фиксом: funnel_task уходит в пул потоков и немедленно отдаёт loop назад.
        t0 = loop.time()
        pause_resp = await client.post(
            "/api/analytics/123/pause",
            headers={
                **api_key_headers,
                    "Idempotency-Key": str(uuid.uuid4()),
            },
            json={},
        )
        elapsed = loop.time() - t0

        # Ждём завершения funnel — чтобы не оставлять висящих корутин
        await funnel_task

    # --- Assertions ---
    # 202 proposal_created — штатный ответ approval-first эндпоинта: важно, что он
    # пришёл БЫСТРО, а не что реклама отключена (её отключит одобрение владельца).
    assert pause_resp.status_code == 202, (
        f"Pause вернул {pause_resp.status_code}: {pause_resp.text}"
    )
    assert pause_resp.json()["status"] == "proposal_created"
    assert elapsed < 1.0, (
        f"Pause занял {elapsed:.2f}с (> 1.0с) — event loop заблокирован!\n"
        "Скорее всего get_cached_analytics вызывается синхронно в /api/funnel "
        "вместо await _cached_analytics_async(...)."
    )
