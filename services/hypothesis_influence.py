"""
Мягкое влияние вердиктов гипотез на приоритет тем в topic_selector.

Подтверждённые комбо угол×город×формат поднимаются в приоритете, опровергнутые
опускаются. «Мёртвые» комбо (много опровержений и ни одного подтверждения)
получают штраф приоритета и уходят в конец списка, но НИКОГДА не исключаются
полностью — бот продолжает исследовать, даже если единственный кандидат под
пробел покрытия оказался «мёртвым» комбо (мягкость, §AC6-7 спеки).

Вердикты учитываются с TTL: старше HYP_TTL_DAYS дней — комбо «реабилитируется»
(рынок меняется, урок устаревает).

См. docs/specs/ARCH-phase4-hypothesist.md §6.3, §8.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from services.creative_intelligence import _get_connection

logger = logging.getLogger(__name__)

# Штраф приоритета для «мёртвых» комбо — уводит их в самый конец списка,
# но не удаляет (см. apply_soft_influence)
_DEAD_PENALTY = -100

# Путь к settings.json — читаем ttl_days/dead_min_refuted напрямую отсюда, а
# не через services.autopilot.get_autopilot_config() (agent.scheduler.load_settings).
# Причина: get_autopilot_config безусловно тянет load_settings, что нарушает
# защищённый контракт ручного пути генерации ТЗ (тест
# tests/test_brief_generator_master_switch.py::test_manual_endpoint_not_gated_by_flag
# требует, чтобы load_settings НИКОГДА не вызывался на этом пути) — topic_selector
# вызывает load_verdict_weights() транзитивно из generate_and_push_briefs. Паттерн
# скопирован из services/topic_selector.py::_hypothesist_enabled.
_SETTINGS_FILE = Path(__file__).resolve().parent.parent / "data" / "settings.json"

# Дефолты — совпадают с AUTOPILOT_DEFAULTS['hypothesist'] в services/autopilot.py
_DEFAULT_TTL_DAYS = 60
_DEFAULT_DEAD_MIN_REFUTED = 2


def _hypothesist_thresholds() -> tuple[int, int]:
    """Читает (ttl_days, dead_min_refuted) напрямую из settings.json.

    Не использует services.autopilot.get_autopilot_config() (см. комментарий
    у _SETTINGS_FILE). При отсутствии/повреждении файла или ключей —
    возвращает дефолты (60 дней TTL, 2 опровержения для «мёртвого» комбо).
    """
    if not _SETTINGS_FILE.exists():
        return _DEFAULT_TTL_DAYS, _DEFAULT_DEAD_MIN_REFUTED
    try:
        data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("hypothesis_influence: settings.json битый — используем дефолты", exc_info=True)
        return _DEFAULT_TTL_DAYS, _DEFAULT_DEAD_MIN_REFUTED

    cfg = (data.get("autopilot") or {}).get("hypothesist") or {}
    ttl_days = cfg.get("ttl_days", _DEFAULT_TTL_DAYS)
    dead_min_refuted = cfg.get("dead_min_refuted", _DEFAULT_DEAD_MIN_REFUTED)
    return ttl_days, dead_min_refuted


def combo_key(angle: str, city: str, ad_format: str) -> str:
    """Нормализованный ключ комбо: lower().strip(), разделитель '::'.

    Пустые части заменяются на '*'. Пример:
    'карусель в cityd' → 'карусель в cityd::cityd::carousel'.
    """
    parts = [angle, city, ad_format]
    normalized = []
    for part in parts:
        value = (part or "").strip().lower()
        normalized.append(value if value else "*")
    return "::".join(normalized)


def load_verdict_weights(now: datetime | None = None) -> dict[str, dict]:
    """Агрегирует вердикты по комбо угол×город×формат с TTL.

    Учитываются гипотезы с verdict_at не старше ttl_days (дефолт 60 дней —
    урок устаревает, рынок меняется). Для каждого combo_key:
      {"confirmed": n, "refuted": n, "score": float, "dead": bool}
    score = confirmed - refuted (для перестановки приоритета).
    dead = refuted >= dead_min_refuted (дефолт 2) AND confirmed == 0.

    Если hypothesist.enabled == False — конфиг всё равно читается только тут
    ради ttl_days/dead_min_refuted; сам гейт enabled применяет вызывающий код
    (apply_soft_influence/topic_selector), поэтому здесь всегда считаем.
    Пороги читаются напрямую из settings.json (_hypothesist_thresholds),
    а не через services.autopilot.get_autopilot_config() — см. комментарий
    у _SETTINGS_FILE (защищённый контракт ручного пути генерации ТЗ).
    """
    ttl_days, dead_min_refuted = _hypothesist_thresholds()

    now = now or datetime.now()
    cutoff = now - timedelta(days=ttl_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT angle, city, ad_format, status
            FROM hypotheses
            WHERE status IN ('confirmed', 'refuted')
              AND verdict_at IS NOT NULL
              AND verdict_at >= ?
            """,
            (cutoff_str,),
        ).fetchall()
    finally:
        conn.close()

    weights: dict[str, dict] = {}
    for row in rows:
        key = combo_key(row["angle"], row["city"], row["ad_format"])
        entry = weights.setdefault(key, {"confirmed": 0, "refuted": 0})
        if row["status"] == "confirmed":
            entry["confirmed"] += 1
        else:
            entry["refuted"] += 1

    for entry in weights.values():
        entry["score"] = entry["confirmed"] - entry["refuted"]
        entry["dead"] = entry["refuted"] >= dead_min_refuted and entry["confirmed"] == 0

    return weights


def is_dead_combo(weights: dict[str, dict], angle: str, city: str, ad_format: str) -> bool:
    """True если комбо помечено dead в weights (см. load_verdict_weights)."""
    key = combo_key(angle, city, ad_format)
    entry = weights.get(key)
    return bool(entry and entry.get("dead"))


def apply_soft_influence(topics: list[dict], weights: dict[str, dict]) -> list[dict]:
    """Мягко переставляет темы по вердиктам (НЕ удаляет).

    Для каждой темы считает combo_key(angle, city, ad_format), берёт
    score/dead из weights. Добавляет в тему поле '_influence_score':
      +score (подтверждённые вверх), dead → штраф -100 (в самый низ, но
      остаётся в списке).
    Возвращает стабильно-отсортированный список: сначала по
    (-_influence_score), затем сохраняя исходный порядок (topic_selector.
    _prioritize уже отсортировал по source/priority).

    Список НИКОГДА не укорачивается — мягкость гарантирована (§8): даже
    если «мёртвое» комбо — единственный кандидат под пробел, оно вернётся
    (последним в списке), а не исчезнет.
    """
    if not topics:
        return []

    scored: list[dict] = []
    for topic in topics:
        key = combo_key(
            topic.get("angle", ""), topic.get("city", ""), topic.get("ad_format", "")
        )
        entry = weights.get(key)

        score = 0.0
        if entry:
            score = float(entry.get("score", 0))
            if entry.get("dead"):
                score += _DEAD_PENALTY

        # Не мутируем исходный dict темы — копия с добавленным полем
        enriched = dict(topic)
        enriched["_influence_score"] = score
        scored.append(enriched)

    # Стабильная сортировка (Python sort стабилен) — при равном score
    # сохраняется исходный порядок (уже расставленный _prioritize)
    scored.sort(key=lambda t: -t["_influence_score"])

    return scored
