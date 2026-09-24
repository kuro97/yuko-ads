"""Тесты для services/hypotheses.py — пары winner/loser, LLM, кеш."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from services.hypotheses import (
    build_ad_pairs,
    _extract_ad_summary,
    _build_llm_prompt,
    analyze_pair_with_llm,
    get_cached_analysis,
    save_to_cache,
    build_smart_hypotheses,
    CACHE_TTL,
)


def make_ad(ad_id, cpl, leads, spend, **kwargs):
    return {
        "ad_id": ad_id,
        "ad_name": f"Ad {ad_id}",
        "cpl": cpl,
        "leads": leads,
        "spend": spend,
        "hook_rate": kwargs.get("hook_rate", 10.0),
        "hold_rate": kwargs.get("hold_rate", 5.0),
        "ctr": kwargs.get("ctr", 2.0),
        "frequency": kwargs.get("frequency", 1.5),
        "creative_class": kwargs.get("creative_class", "video"),
        "business_class": kwargs.get("business_class", ""),
        "city": kwargs.get("city", "CityA"),
        "adset_type": kwargs.get("adset_type", "cold"),
    }


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    cache_file = tmp_path / "test_cache.json"
    monkeypatch.setattr("services.hypotheses.CACHE_FILE", cache_file)
    monkeypatch.setattr("services.hypotheses.DATA_DIR", tmp_path)
    return cache_file


# --- _extract_ad_summary ---

def test_extract_ad_summary_all_fields():
    """Все 13 полей корректно извлекаются из словаря объявления."""
    ad = make_ad("w1", cpl=10.0, leads=5, spend=50.0, hook_rate=15.0, hold_rate=8.0,
                 ctr=3.0, frequency=2.0, creative_class="image", business_class="premium",
                 city="CityB", adset_type="warm")
    summary = _extract_ad_summary(ad)
    assert summary["ad_id"] == "w1"
    assert summary["ad_name"] == "Ad w1"
    assert summary["cpl"] == 10.0
    assert summary["leads"] == 5
    assert summary["spend"] == 50.0
    assert summary["hook_rate"] == 15.0
    assert summary["hold_rate"] == 8.0
    assert summary["ctr"] == 3.0
    assert summary["frequency"] == 2.0
    assert summary["creative_class"] == "image"
    assert summary["business_class"] == "premium"
    assert summary["city"] == "CityB"
    assert summary["adset_type"] == "warm"


def test_extract_ad_summary_defaults():
    """При пустом словаре возвращаются дефолтные значения."""
    summary = _extract_ad_summary({})
    assert summary["ad_id"] == ""
    assert summary["cpl"] == 0.0
    assert summary["leads"] == 0


# --- build_ad_pairs ---

def test_build_ad_pairs_basic():
    """5 winners + 5 losers → минимум одна пара с нужными полями."""
    winners = [make_ad(f"w{i}", cpl=float(i), leads=3, spend=30.0) for i in range(1, 6)]
    losers = [make_ad(f"l{i}", cpl=float(i * 10), leads=2, spend=15.0) for i in range(1, 6)]
    pairs = build_ad_pairs(winners + losers)
    assert len(pairs) > 0
    pair = pairs[0]
    for key in ("winner", "loser", "ratio", "priority", "confidence", "hook_diff", "hold_diff"):
        assert key in pair
    assert pair["winner"]["cpl"] < pair["loser"]["cpl"]


def test_build_ad_pairs_empty():
    """Пустой список → пустой результат."""
    assert build_ad_pairs([]) == []


def test_build_ad_pairs_insufficient_data():
    """Одно объявление — пар быть не может."""
    assert build_ad_pairs([make_ad("only1", cpl=10.0, leads=3, spend=20.0)]) == []


def test_build_ad_pairs_priority_high():
    """ratio > 3 → HIGH priority."""
    pairs = build_ad_pairs([
        make_ad("w1", cpl=10.0, leads=3, spend=30.0),
        make_ad("l1", cpl=50.0, leads=2, spend=20.0),
    ])
    assert len(pairs) == 1
    assert pairs[0]["priority"] == "HIGH"
    assert pairs[0]["ratio"] == 5.0


def test_build_ad_pairs_priority_med():
    """ratio > 2 и <= 3 → MED priority."""
    pairs = build_ad_pairs([
        make_ad("w1", cpl=10.0, leads=3, spend=30.0),
        make_ad("l1", cpl=25.0, leads=2, spend=20.0),
    ])
    assert len(pairs) == 1
    assert pairs[0]["priority"] == "MED"


def test_build_ad_pairs_priority_low():
    """ratio > 1 и <= 2 → LOW priority."""
    pairs = build_ad_pairs([
        make_ad("w1", cpl=10.0, leads=3, spend=30.0),
        make_ad("l1", cpl=15.0, leads=2, spend=20.0),
    ])
    assert len(pairs) == 1
    assert pairs[0]["priority"] == "LOW"


def test_build_ad_pairs_confidence_high():
    """Оба >= 5 лидов → confidence == 'высокая'."""
    pairs = build_ad_pairs([
        make_ad("w1", cpl=5.0, leads=6, spend=30.0),
        make_ad("l1", cpl=30.0, leads=7, spend=20.0),
    ])
    assert pairs[0]["confidence"] == "высокая"


def test_build_ad_pairs_confidence_medium():
    """Хотя бы один < 5 лидов → confidence == 'средняя'."""
    pairs = build_ad_pairs([
        make_ad("w1", cpl=5.0, leads=3, spend=30.0),
        make_ad("l1", cpl=30.0, leads=7, spend=20.0),
    ])
    assert pairs[0]["confidence"] == "средняя"


def test_build_ad_pairs_same_ad_skipped():
    """Winner и loser с одинаковым ad_id пропускаются."""
    same_ad = make_ad("dup", cpl=10.0, leads=3, spend=50.0)
    for pair in build_ad_pairs([same_ad]):
        assert pair["winner"]["ad_id"] != pair["loser"]["ad_id"]


def test_build_ad_pairs_ratio_eq1_skipped():
    """Пары с ratio == 1.0 не создаются."""
    pairs = build_ad_pairs([
        make_ad("a1", cpl=10.0, leads=3, spend=30.0),
        make_ad("a2", cpl=10.0, leads=2, spend=20.0),
    ])
    assert pairs == []


def test_build_ad_pairs_loser_filter_spend():
    """Loser с spend < 10 не попадает в losers."""
    pairs = build_ad_pairs([
        make_ad("w1", cpl=5.0, leads=3, spend=30.0),
        make_ad("l1", cpl=50.0, leads=2, spend=5.0),
    ])
    assert pairs == []


def test_build_ad_pairs_winner_filter_leads():
    """Winner с leads < 2 не попадает в winners."""
    pairs = build_ad_pairs([
        make_ad("w1", cpl=5.0, leads=1, spend=30.0),
        make_ad("l1", cpl=50.0, leads=2, spend=20.0),
    ])
    assert pairs == []


def test_build_ad_pairs_max_5_pairs():
    """Создаётся не более 5 пар."""
    winners = [make_ad(f"w{i}", cpl=float(i), leads=3, spend=30.0) for i in range(1, 10)]
    losers = [make_ad(f"l{i}", cpl=float(i * 20), leads=2, spend=15.0) for i in range(1, 10)]
    assert len(build_ad_pairs(winners + losers)) <= 5


# --- _build_llm_prompt ---

def test_build_llm_prompt_format():
    """Промпт содержит имена объявлений, метрики и ratio."""
    w = make_ad("w1", cpl=10.0, leads=5, spend=50.0, hook_rate=15.0, hold_rate=8.0)
    l = make_ad("l1", cpl=40.0, leads=2, spend=20.0, hook_rate=5.0, hold_rate=3.0)
    pair = {
        "winner": _extract_ad_summary(w),
        "loser": _extract_ad_summary(l),
        "ratio": 4.0,
        "priority": "HIGH",
        "confidence": "высокая",
        "hook_diff": 10.0,
        "hold_diff": 5.0,
    }
    prompt = _build_llm_prompt(pair)
    assert "Ad w1" in prompt
    assert "Ad l1" in prompt
    assert "$10.0" in prompt or "10.0" in prompt
    assert "4.0x" in prompt
    assert "Объясни" in prompt


# --- analyze_pair_with_llm ---

def _make_pair():
    w = make_ad("w1", cpl=10.0, leads=5, spend=50.0)
    l = make_ad("l1", cpl=40.0, leads=2, spend=20.0)
    return {
        "winner": _extract_ad_summary(w),
        "loser": _extract_ad_summary(l),
        "ratio": 4.0, "priority": "HIGH", "confidence": "высокая",
        "hook_diff": 0.0, "hold_diff": 0.0,
    }


def test_analyze_pair_no_api_key():
    """Возвращает None, если ANTHROPIC_API_KEY пустая строка."""
    with patch("config.ANTHROPIC_API_KEY", ""):
        assert analyze_pair_with_llm(_make_pair()) is None


def test_analyze_pair_none_api_key():
    """Возвращает None, если ANTHROPIC_API_KEY равен None."""
    with patch("config.ANTHROPIC_API_KEY", None):
        assert analyze_pair_with_llm(_make_pair()) is None


def test_analyze_pair_success():
    """При успешном ответе API возвращает strip()-нутый текст."""
    mock_content = MagicMock()
    mock_content.text = "  Winner лучше из-за высокого hook rate.  "
    mock_response = MagicMock()
    mock_response.content = [mock_content]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("config.ANTHROPIC_API_KEY", "sk-test"), \
         patch("anthropic.Anthropic", return_value=mock_client):
        result = analyze_pair_with_llm(_make_pair())

    assert result == "Winner лучше из-за высокого hook rate."
    mock_client.messages.create.assert_called_once()


def test_analyze_pair_empty_content():
    """Если content пустой — возвращает None."""
    mock_response = MagicMock()
    mock_response.content = []
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("config.ANTHROPIC_API_KEY", "sk-test"), \
         patch("anthropic.Anthropic", return_value=mock_client):
        assert analyze_pair_with_llm(_make_pair()) is None


def test_analyze_pair_timeout():
    """APITimeoutError оборачивается в RuntimeError с 'таймаут'."""
    import anthropic
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = anthropic.APITimeoutError(request=MagicMock())

    with patch("config.ANTHROPIC_API_KEY", "sk-test"), \
         patch("anthropic.Anthropic", return_value=mock_client):
        with pytest.raises(RuntimeError, match="таймаут"):
            analyze_pair_with_llm(_make_pair())


def test_analyze_pair_api_error():
    """Произвольная APIError оборачивается в RuntimeError."""
    import anthropic
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = anthropic.APIError(
        message="bad request", request=MagicMock(), body=None,
    )

    with patch("config.ANTHROPIC_API_KEY", "sk-test"), \
         patch("anthropic.Anthropic", return_value=mock_client):
        with pytest.raises(RuntimeError, match="ошибка"):
            analyze_pair_with_llm(_make_pair())


# --- Кеш ---

def test_save_and_get_cache(tmp_cache):
    """Сохранённый анализ можно получить из кеша."""
    save_to_cache("w1", "l1", "Это тестовый анализ")
    assert get_cached_analysis("w1", "l1") == "Это тестовый анализ"


def test_cache_expired(tmp_cache):
    """Устаревшая запись (timestamp < now - TTL) возвращает None."""
    tmp_cache.write_text(json.dumps({
        "w1__l1": {"analysis": "старый", "timestamp": time.time() - CACHE_TTL - 1}
    }), encoding="utf-8")
    assert get_cached_analysis("w1", "l1") is None


def test_cache_miss(tmp_cache):
    """Несуществующий ключ возвращает None."""
    assert get_cached_analysis("nonexistent", "pair") is None


def test_cache_miss_no_file(tmp_cache):
    """Если файла кеша нет — возвращает None без ошибок."""
    assert not tmp_cache.exists()
    assert get_cached_analysis("w1", "l1") is None


def test_cache_cleanup(tmp_cache):
    """При сохранении удаляются записи старше 7 дней, свежие остаются."""
    max_age = CACHE_TTL * 7
    tmp_cache.write_text(json.dumps({
        "old__pair": {"analysis": "устарело", "timestamp": time.time() - max_age - 1},
        "fresh__pair": {"analysis": "актуально", "timestamp": time.time() - 60},
    }), encoding="utf-8")

    save_to_cache("new_winner", "new_loser", "новый анализ")

    saved = json.loads(tmp_cache.read_text(encoding="utf-8"))
    assert "old__pair" not in saved
    assert "fresh__pair" in saved
    assert "new_winner__new_loser" in saved


def test_cache_overwrite(tmp_cache):
    """Повторное сохранение перезаписывает старое значение."""
    save_to_cache("w1", "l1", "первый")
    save_to_cache("w1", "l1", "второй")
    assert get_cached_analysis("w1", "l1") == "второй"


def test_cache_multiple_keys(tmp_cache):
    """Разные пары хранятся независимо."""
    save_to_cache("w1", "l1", "пара 1")
    save_to_cache("w2", "l2", "пара 2")
    assert get_cached_analysis("w1", "l1") == "пара 1"
    assert get_cached_analysis("w2", "l2") == "пара 2"


# --- build_smart_hypotheses ---

def test_build_smart_hypotheses_no_llm(tmp_cache):
    """Без API-ключа llm_analysis == None, гипотеза строится."""
    ads = [
        make_ad("w1", cpl=5.0, leads=3, spend=30.0),
        make_ad("l1", cpl=25.0, leads=2, spend=20.0),
    ]
    with patch("config.ANTHROPIC_API_KEY", ""):
        hypotheses = build_smart_hypotheses(ads)

    assert len(hypotheses) == 1
    h = hypotheses[0]
    assert h["type"] == "smart"
    assert h["llm_analysis"] is None
    assert h["winner_id"] == "w1"
    assert h["loser_id"] == "l1"


def test_build_smart_hypotheses_with_cache(tmp_cache):
    """Кешированный анализ используется без вызова LLM."""
    ads = [
        make_ad("w1", cpl=5.0, leads=3, spend=30.0),
        make_ad("l1", cpl=25.0, leads=2, spend=20.0),
    ]
    save_to_cache("w1", "l1", "кешированный анализ")

    with patch("config.ANTHROPIC_API_KEY", ""), \
         patch("services.hypotheses.analyze_pair_with_llm") as mock_llm:
        hypotheses = build_smart_hypotheses(ads)

    mock_llm.assert_not_called()
    assert hypotheses[0]["llm_analysis"] == "кешированный анализ"


def test_build_smart_hypotheses_llm_called(tmp_cache):
    """При отсутствии кеша LLM вызывается и результат сохраняется."""
    ads = [
        make_ad("w1", cpl=5.0, leads=3, spend=30.0),
        make_ad("l1", cpl=25.0, leads=2, spend=20.0),
    ]
    with patch("config.ANTHROPIC_API_KEY", "sk-test"), \
         patch("services.hypotheses.analyze_pair_with_llm", return_value="свежий анализ"):
        hypotheses = build_smart_hypotheses(ads)

    assert hypotheses[0]["llm_analysis"] == "свежий анализ"
    assert get_cached_analysis("w1", "l1") == "свежий анализ"


def test_build_smart_hypotheses_llm_error_fallback(tmp_cache):
    """RuntimeError от LLM → llm_analysis == None, гипотеза всё равно создаётся."""
    ads = [
        make_ad("w1", cpl=5.0, leads=3, spend=30.0),
        make_ad("l1", cpl=25.0, leads=2, spend=20.0),
    ]
    with patch("config.ANTHROPIC_API_KEY", "sk-test"), \
         patch("services.hypotheses.analyze_pair_with_llm", side_effect=RuntimeError("ошибка")):
        hypotheses = build_smart_hypotheses(ads)

    assert len(hypotheses) == 1
    assert hypotheses[0]["llm_analysis"] is None


def test_build_smart_hypotheses_empty():
    """Пустой список → пустой список гипотез."""
    with patch("config.ANTHROPIC_API_KEY", ""):
        assert build_smart_hypotheses([]) == []


def test_build_smart_hypotheses_fields(tmp_cache):
    """Каждая гипотеза содержит все обязательные поля."""
    ads = [
        make_ad("w1", cpl=5.0, leads=3, spend=30.0),
        make_ad("l1", cpl=25.0, leads=2, spend=20.0),
    ]
    with patch("config.ANTHROPIC_API_KEY", ""):
        hypotheses = build_smart_hypotheses(ads)

    required = {"type", "title", "description", "priority", "confidence",
                "winner_id", "loser_id", "llm_analysis"}
    for h in hypotheses:
        assert required.issubset(h.keys())
