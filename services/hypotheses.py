"""Умные гипотезы — сравнение пар winner/loser + LLM анализ."""

import json
import time
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_FILE = DATA_DIR / "hypotheses_cache.json"
CACHE_TTL = 86400  # 24 часа


def _extract_ad_summary(ad: dict) -> dict:
    """Извлекает ключевые метрики объявления для пары."""
    return {
        "ad_id": ad.get("ad_id", ""),
        "ad_name": ad.get("ad_name", ""),
        "cpl": ad.get("cpl", 0.0),
        "leads": ad.get("leads", 0),
        "spend": ad.get("spend", 0.0),
        "hook_rate": ad.get("hook_rate", 0.0),
        "hold_rate": ad.get("hold_rate", 0.0),
        "ctr": ad.get("ctr", 0.0),
        "frequency": ad.get("frequency", 0.0),
        "creative_class": ad.get("creative_class", ""),
        "business_class": ad.get("business_class", ""),
        "city": ad.get("city", ""),
        "adset_type": ad.get("adset_type", ""),
    }


def build_ad_pairs(ads: list[dict]) -> list[dict]:
    """Создаёт пары winner vs loser для сравнения.

    Winners: top-5 по CPL (leads >= 2, cpl > 0)
    Losers: bottom-5 по CPL (spend >= 10, leads >= 1, cpl > 0)

    Пара создаётся только если winner != loser и ratio > 1.
    priority: HIGH (ratio > 3), MED (ratio > 2), LOW.
    confidence: «высокая» если у обоих >= 5 лидов, иначе «средняя».
    """
    # Top-5 winners — лучшие по CPL
    winners = sorted(
        [a for a in ads if a.get("leads", 0) >= 2 and a.get("cpl", 0) > 0],
        key=lambda x: x["cpl"],
    )[:5]

    # Bottom-5 losers — худшие по CPL
    losers = sorted(
        [a for a in ads if a.get("leads", 0) >= 1 and a.get("spend", 0) >= 10 and a.get("cpl", 0) > 0],
        key=lambda x: -x["cpl"],
    )[:5]

    pairs: list[dict] = []
    for i in range(min(len(winners), len(losers))):
        w, l = winners[i], losers[i]
        if w["ad_id"] == l["ad_id"]:
            continue

        ratio = round(l["cpl"] / w["cpl"], 1) if w["cpl"] > 0 else 0.0
        if ratio <= 1:
            continue

        priority = "HIGH" if ratio > 3 else ("MED" if ratio > 2 else "LOW")
        confidence = "высокая" if w.get("leads", 0) >= 5 and l.get("leads", 0) >= 5 else "средняя"
        hook_diff = round(w.get("hook_rate", 0.0) - l.get("hook_rate", 0.0), 1)
        hold_diff = round(w.get("hold_rate", 0.0) - l.get("hold_rate", 0.0), 1)

        pairs.append({
            "winner": _extract_ad_summary(w),
            "loser": _extract_ad_summary(l),
            "ratio": ratio,
            "priority": priority,
            "confidence": confidence,
            "hook_diff": hook_diff,
            "hold_diff": hold_diff,
        })

    return pairs


def _build_llm_prompt(pair: dict) -> str:
    """Формирует промпт для Claude на основе пары."""
    w = pair["winner"]
    l = pair["loser"]
    return (
        "Сравни два объявления:\n\n"
        f"🏆 Winner: {w['ad_name']}\n"
        f"- CPL: ${w['cpl']} ({w['leads']} лидов, расход ${w['spend']})\n"
        f"- Hook Rate: {w['hook_rate']}%, Hold Rate: {w['hold_rate']}%\n"
        f"- CTR: {w['ctr']}%, Частота: {w['frequency']}\n"
        f"- Класс: {w['creative_class']}, Город: {w['city']}\n\n"
        f"❌ Loser: {l['ad_name']}\n"
        f"- CPL: ${l['cpl']} ({l['leads']} лидов, расход ${l['spend']})\n"
        f"- Hook Rate: {l['hook_rate']}%, Hold Rate: {l['hold_rate']}%\n"
        f"- CTR: {l['ctr']}%, Частота: {l['frequency']}\n"
        f"- Класс: {l['creative_class']}, Город: {l['city']}\n\n"
        f"Разница CPL: {pair['ratio']}x, Hook: {pair['hook_diff']:+}%, Hold: {pair['hold_diff']:+}%\n\n"
        "Объясни:\n"
        "1. Почему winner лучше (какие метрики решают)\n"
        "2. Что конкретно сделать с loser (одно действие)"
    )


LLM_SYSTEM_PROMPT = (
    "Ты — аналитик рекламы. Анализируешь пары объявлений Facebook Ads. "
    "Отвечай на русском. Кратко: 2-4 предложения. Без вступлений."
)


def analyze_pair_with_llm(pair: dict) -> Optional[str]:
    """Анализирует пару через Claude Haiku. 2-4 предложения.

    Возвращает None если ANTHROPIC_API_KEY не задан.
    Raises RuntimeError при ошибке API.
    """
    import config

    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        return None

    import anthropic

    model = getattr(config, "CLAUDE_HAIKU_MODEL", "claude-haiku-4-5")
    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_llm_prompt(pair)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=LLM_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APITimeoutError:
        raise RuntimeError("Claude API таймаут")
    except anthropic.APIError as e:
        raise RuntimeError(f"Claude API ошибка: {e}")

    if response.content:
        return response.content[0].text.strip()
    return None


def _load_cache() -> dict:
    """Загружает кеш LLM-ответов из файла."""
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    """Сохраняет кеш LLM-ответов. Удаляет записи старше 7 дней."""
    now = time.time()
    max_age = CACHE_TTL * 7  # 7 дней
    cleaned = {k: v for k, v in cache.items() if now - v.get("timestamp", 0) < max_age}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")


def _cache_key(pair: dict) -> str:
    """Ключ кеша: winner_id__loser_id."""
    return f"{pair['winner']['ad_id']}__{pair['loser']['ad_id']}"


def get_cached_analysis(winner_id: str, loser_id: str) -> Optional[str]:
    """Возвращает кешированный LLM-анализ или None если кеш устарел."""
    cache = _load_cache()
    key = f"{winner_id}__{loser_id}"
    entry = cache.get(key)
    if entry and time.time() - entry.get("timestamp", 0) < CACHE_TTL:
        return entry["analysis"]
    return None


def save_to_cache(winner_id: str, loser_id: str, analysis: str) -> None:
    """Сохраняет LLM-анализ в кеш с timestamp."""
    cache = _load_cache()
    cache[f"{winner_id}__{loser_id}"] = {
        "analysis": analysis,
        "timestamp": time.time(),
    }
    _save_cache(cache)


def build_smart_hypotheses(ads: list[dict]) -> list[dict]:
    """Полный пайплайн: пары -> LLM анализ (с кешем) -> гипотезы.

    Для каждой пары:
    1. Проверяет кеш
    2. Если нет — вызывает LLM (если ключ есть)
    3. Формирует гипотезу с полем llm_analysis
    """
    pairs = build_ad_pairs(ads)
    hypotheses: list[dict] = []

    for pair in pairs:
        w = pair["winner"]
        l = pair["loser"]
        ratio = pair["ratio"]
        hook_diff = pair["hook_diff"]
        hold_diff = pair["hold_diff"]

        # LLM анализ с кешем
        llm_analysis = get_cached_analysis(w["ad_id"], l["ad_id"])
        if llm_analysis is None:
            try:
                llm_analysis = analyze_pair_with_llm(pair)
                if llm_analysis:
                    save_to_cache(w["ad_id"], l["ad_id"], llm_analysis)
            except RuntimeError:
                llm_analysis = None  # fallback — без LLM

        description = (
            f"🏆 {w['ad_name'][:50]}\n"
            f"CPL ${w['cpl']}, {w['leads']} лидов, Hook {w['hook_rate']}%, "
            f"Hold {w['hold_rate']}%, расход ${w['spend']}\n\n"
            f"❌ {l['ad_name'][:50]}\n"
            f"CPL ${l['cpl']}, {l['leads']} лидов, Hook {l['hook_rate']}%, "
            f"Hold {l['hold_rate']}%, расход ${l['spend']}\n\n"
            f"Разница: CPL в {ratio}x раз, Hook {'+' if hook_diff >= 0 else ''}{hook_diff}%, "
            f"Hold {'+' if hold_diff >= 0 else ''}{hold_diff}%"
        )

        hypotheses.append({
            "type": "smart",
            "title": f"CPL ${w['cpl']} vs ${l['cpl']} — что делает winner лучше?",
            "description": description,
            "priority": pair["priority"],
            "confidence": pair["confidence"],
            "winner_id": w["ad_id"],
            "loser_id": l["ad_id"],
            "llm_analysis": llm_analysis,
        })

    return hypotheses
