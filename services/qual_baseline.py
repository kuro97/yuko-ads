"""Адаптивный порог квалификации для лестницы подъёма Budget Scaler v2.

Идея: порог «качественного» адсета — не фиксированный
процент, а «от месяца к месяцу»: доля X от квал%-аккаунта за последние 30 дней,
зажатая полом/потолком. Так порог сам подстраивается под сезон и качество
трафика, а не устаревает.

Модуль изолированный: используется этапом 2 (средний сигнал лестницы), который
включится отдельным релизом. Сейчас — модуль + тесты.

Источник базы — AMO (integrations.amo.get_leads_window + classify_lead):
квал% = (лиды со статусом «квал» или «оплата») / все лиды окна. Любая ошибка
AMO НЕ бросается наружу — возвращаем None, вызывающий сам решает (без базы
квал-ступень просто выключается, оплатная ступень работает).

Кеш: data/qual_baseline_state.json (TTL 24ч). Протух → пересчёт; пересчёт упал →
отдаём протухший кеш с пометкой stale=True, если он не старше 7 дней, иначе None.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_TZ_LOCAL = timezone(timedelta(hours=5))

_QUAL_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "qual_baseline_state.json"

# Предел «протухший, но ещё годный» кеш при сбое пересчёта (дни)
_STALE_MAX_DAYS = 7


def _now(now: datetime | None = None) -> datetime:
    """Нормализует момент к aware-datetime TZ CityA."""
    moment = now if now is not None else datetime.now(_TZ_LOCAL)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_TZ_LOCAL)
    return moment.astimezone(_TZ_LOCAL)


def compute_account_qual_baseline(days: int = 30, now: datetime | None = None) -> dict | None:
    """Считает квал%-аккаунта из AMO за последние `days` дней.

    Квалифицированным считаем лид со статусом «квал» ИЛИ «оплата»
    (integrations.amo.classify_lead). Возвращает dict или None.

    Returns:
        {"qual_pct": float, "qual_leads": int, "total_leads": int,
         "days": int, "computed_at": iso} — или None при ошибке / отсутствии лидов.
        Исключения не пробрасываются (fail-safe: None).
    """
    try:
        from integrations.amo import get_leads_window, classify_lead

        now_dt = _now(now)
        to_ts = int(now_dt.timestamp())
        from_ts = int((now_dt - timedelta(days=days)).timestamp())

        leads = get_leads_window(from_ts, to_ts)
        total = len(leads)
        if total == 0:
            logger.info("compute_account_qual_baseline: за %d дн лидов нет — базы нет", days)
            return None

        qual = sum(1 for lead in leads if classify_lead(lead) in ("квал", "оплата"))
        qual_pct = qual / total * 100.0
        return {
            "qual_pct": qual_pct,
            "qual_leads": qual,
            "total_leads": total,
            "days": days,
            "computed_at": now_dt.isoformat(),
        }
    except Exception as exc:
        logger.warning("compute_account_qual_baseline: не удалось посчитать базу квала — %s", exc)
        return None


def _load_cache() -> dict | None:
    """Читает кеш базы квала. None если нет/битый."""
    if not _QUAL_STATE_FILE.exists():
        return None
    try:
        data = json.loads(_QUAL_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("qual_baseline: не удалось прочитать кеш — %s", exc)
        return None


def _save_cache(payload: dict) -> None:
    """Атомарно (tmp+rename) сохраняет кеш базы квала."""
    _QUAL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _QUAL_STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_QUAL_STATE_FILE)
    except Exception as exc:
        logger.error("qual_baseline: не удалось сохранить кеш — %s", exc)


def _age_hours(computed_at: str, now_dt: datetime) -> float | None:
    """Возраст кеша в часах по полю computed_at (ISO). None если битое."""
    if not computed_at or not isinstance(computed_at, str):
        return None
    try:
        dt = datetime.fromisoformat(computed_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ_LOCAL)
    return (now_dt - dt).total_seconds() / 3600.0


def get_cached_qual_baseline(ttl_hours: float = 24, now: datetime | None = None) -> dict | None:
    """База квала с кешем TTL `ttl_hours`.

    Свежий кеш → отдаём как есть (stale=False). Протух → пересчёт; успех →
    сохраняем и отдаём (stale=False). Пересчёт упал → протухший кеш с stale=True,
    если он не старше 7 дней; иначе None.
    """
    now_dt = _now(now)
    cache = _load_cache()

    if cache is not None:
        age = _age_hours(cache.get("computed_at"), now_dt)
        if age is not None and age < ttl_hours:
            return {**cache, "stale": False}

    # Кеша нет или протух → пересчитываем
    fresh = compute_account_qual_baseline(days=30, now=now_dt)
    if fresh is not None:
        _save_cache(fresh)
        return {**fresh, "stale": False}

    # Пересчёт не удался — отдаём протухший кеш, если он ещё годен
    if cache is not None:
        age = _age_hours(cache.get("computed_at"), now_dt)
        if age is not None and age <= _STALE_MAX_DAYS * 24:
            logger.info("get_cached_qual_baseline: пересчёт упал — отдаю протухший кеш (stale)")
            return {**cache, "stale": True}

    logger.info("get_cached_qual_baseline: базы квала нет (нет свежего кеша и пересчёт упал)")
    return None


def effective_qual_threshold(cfg_v2: dict, baseline: dict | None) -> tuple[float | None, str]:
    """Итоговый порог квала для отчёта скейлера.

    Приоритет:
      1. qual_override_pct > 0 → фиксированный порог (адаптив игнорируем).
      2. Иначе clamp(qual_base_mult × база, qual_floor_pct, qual_cap_pct).
      3. База недоступна (baseline None / нет qual_pct) → None (квал-ступень выключена).

    Returns:
        (порог_% | None, строка-подпись для отчёта). Вторая строка — человеческая,
        напр. «порог квала 12.3% (0.8× базы 15.4% за 30 дн)».
    """
    cfg_v2 = cfg_v2 or {}
    try:
        override = float(cfg_v2.get("qual_override_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        override = 0.0

    if override > 0:
        return override, f"порог квала {override:.1f}% (задан вручную)"

    if not baseline or baseline.get("qual_pct") is None:
        return None, "порог квала: база квала недоступна — квал-ступень выключена"

    base = float(baseline["qual_pct"])
    mult = float(cfg_v2.get("qual_base_mult", 0.8))
    floor = float(cfg_v2.get("qual_floor_pct", 5.0))
    cap = float(cfg_v2.get("qual_cap_pct", 30.0))

    threshold = min(max(mult * base, floor), cap)
    days = baseline.get("days", 30)
    stale_note = " (данные квала устарели)" if baseline.get("stale") else ""
    label = f"порог квала {threshold:.1f}% ({mult:g}× базы {base:.1f}% за {days} дн){stale_note}"
    return threshold, label
