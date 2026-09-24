"""Тесты устойчивой загрузки лидов: период режется на недельные окна.

Дефект: месяц грузился одним запросом-пагинацией, и одна
страница, которую AMO не отдал за 60с даже после 3 ретраев, роняла весь расчёт
в 502 «Источники недоступны».

Лечение: период идёт недельными окнами. Сбой окна не отменяет остальные —
отдаём что есть и честно помечаем ответ как неполный (partial + gaps).
Полный отказ (не пришло ни одного окна) по-прежнему 502, чтобы нули не выдать
за реальность.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from web.sources_routes import _cache, _compute_sources


@pytest.fixture(autouse=True)
def _clear_sources_cache():
    _cache.clear()
    yield
    _cache.clear()


def _lead(lead_id, created_at):
    return {
        "id": lead_id, "name": "Сделка", "source_id": None, "custom_fields": [],
        "tags": [], "status_id": 1, "price": 0, "created_at": created_at,
    }


def _no_fb():
    return patch("web.app._cached_analytics_async", new_callable=AsyncMock, return_value=[])


@pytest.mark.asyncio
async def test_month_is_loaded_in_weekly_windows():
    """Месяц не грузится одним куском — окон должно быть несколько."""
    calls = []

    def fake_window(from_ts, to_ts):
        calls.append((from_ts, to_ts))
        return []

    with patch("web.sources_routes.get_leads_window", side_effect=fake_window), _no_fb():
        await _compute_sources("2026-07-01", "2026-07-31")

    assert len(calls) >= 4, f"июль должен грузиться окнами, а не одним запросом: {len(calls)}"
    # Окна не пересекаются и идут подряд — иначе будут дубли лидов
    for (_, prev_to), (next_from, _) in zip(calls, calls[1:]):
        assert next_from == prev_to + 1, "окна пересекаются или между ними дыра"


@pytest.mark.asyncio
async def test_one_failed_window_keeps_the_rest():
    """Одно упавшее окно не рушит месяц: отдаём остальные и помечаем partial."""
    calls = {"n": 0}

    def flaky_window(from_ts, to_ts):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TimeoutError("Read timed out")
        return [_lead(calls["n"], from_ts + 3600)]

    with patch("web.sources_routes.get_leads_window", side_effect=flaky_window), _no_fb():
        result = await _compute_sources("2026-07-01", "2026-07-31")

    assert result["total"]["leads"] >= 3, "лиды из уцелевших окон должны остаться"
    assert result["partial"] is True, "неполный ответ обязан признаться, что он неполный"
    assert len(result["gaps"]) == 1, "пропущенный диапазон должен быть указан"


@pytest.mark.asyncio
async def test_full_answer_is_not_marked_partial():
    """Всё загрузилось — никаких предупреждений."""
    with patch("web.sources_routes.get_leads_window",
               side_effect=lambda f, t: [_lead(f, f + 3600)]), _no_fb():
        result = await _compute_sources("2026-07-01", "2026-07-31")

    assert result["partial"] is False
    assert result["gaps"] == []


@pytest.mark.asyncio
async def test_total_failure_still_502():
    """Ни одного окна не пришло — это 502, а не «ноль лидов»."""
    with patch("web.sources_routes.get_leads_window", side_effect=TimeoutError("AMO down")), _no_fb():
        with pytest.raises(HTTPException) as e:
            await _compute_sources("2026-07-01", "2026-07-31")
    assert e.value.status_code == 502


@pytest.mark.asyncio
async def test_partial_result_is_not_cached():
    """Неполный ответ не должен залипнуть в кеше на 15 минут."""
    calls = {"n": 0}

    def flaky_window(from_ts, to_ts):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TimeoutError("Read timed out")
        return [_lead(calls["n"], from_ts + 3600)]

    with patch("web.sources_routes.get_leads_window", side_effect=flaky_window), _no_fb():
        await _compute_sources("2026-07-01", "2026-07-31")

    assert not _cache, "частичный результат в кеш не кладём — следующий запрос должен попробовать снова"
