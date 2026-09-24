"""
Decision Policy — «мозг решений» автопилота.

Принимает список объявлений с метриками, возвращает рекомендацию по каждому:
  {ad_id, ad_name, adset_id, action, score, reasons: [строки на русском]}

Модуль ТОЛЬКО считает и объясняет. Никаких действий с FB API не выполняет.

Архитектура сигналов:
  A. Scale-скоринг (день 3-4, ранние сигналы)  → score 0..9
  B. Outcome-правила (зрелые объявления с данными)
  C. Guardrail: никогда не PAUSE последнюю рекламу в adset
"""

import math
import re
import statistics
from typing import Optional

from services.formatting import fmt_money, pluralize_leads


# ---------------------------------------------------------------------------
# Источник-каскад оплат (Шаг B, ARCH-cdp-payments §6.5): переключатель
# autopilot.cdp.payments_source решает, откуда брать «факт оплаты».
#   "amo"/"shadow" (дефолт) — старое поведение 1:1: ad.get("payments").
#   "erp"                    — payments_effective(ad) = max(AMO, ERP), fail-closed.
# ---------------------------------------------------------------------------

def _eff_payments(ad: dict, use_erp: bool) -> int | None:
    """Возвращает эффективный сигнал оплат под переключателем payments_source.

    use_erp=False (amo/shadow) — старое поведение: ad.get("payments") как есть.
    use_erp=True (erp)          — payments_effective(ad): max(payments_amo, payments_erp),
                                 None только если оба источника None (§6.4).
    """
    if not use_erp:
        return ad.get("payments")
    from services.cdp_payments import payments_effective
    return payments_effective(ad)

# ---------------------------------------------------------------------------
# Ключевые слова тем — пример эвристики; настройте под свои данные
# ---------------------------------------------------------------------------

# Темы с эмоцией/оффером — в этом примере считаются сигналом в пользу SCALE
_EMOTION_KEYWORDS = [
    "отзыв", "клиент", "тиктокер", "блогер", "страх", "боль",
    "рассрочк", "на 12 месяцев", "цена", "скидк",
]

# Тема-вето на SCALE: объявления с этим словом в названии политика не
# масштабирует. Значение — условный пример; задайте своё под свои данные.
_VETO_KEYWORD = "бонус"


def _has_emotion_theme(ad_name: str) -> bool:
    """Возвращает True, если название содержит хотя бы одно ключевое слово оффера/эмоции."""
    name_lower = ad_name.lower()
    return any(kw in name_lower for kw in _EMOTION_KEYWORDS)


def _has_veto_theme(ad_name: str) -> bool:
    """Возвращает True, если название содержит тему-вето _VETO_KEYWORD (вето на SCALE)."""
    return _VETO_KEYWORD in ad_name.lower()


# ---------------------------------------------------------------------------
# Группировка по «пиринговой группе» (city + adset_type)
# ---------------------------------------------------------------------------

def _peer_group_key(ad: dict) -> tuple:
    """Ключ пиринговой группы для относительного сравнения сигналов hook/ctr/cpl."""
    return (
        ad.get("city") or "",
        ad.get("adset_type") or "",
    )


def _is_video_ad(ad: dict) -> bool:
    """Считаем объявление видео, если есть хотя бы одно видео-поле с ненулевым значением."""
    video_fields = ("video_views_3s", "video_p25", "video_p50", "video_p75", "video_p100")
    return any((ad.get(f) or 0) > 0 for f in video_fields)


# ---------------------------------------------------------------------------
# Медианы внутри переданного списка
# ---------------------------------------------------------------------------

def _compute_peer_medians(ads: list[dict]) -> dict:
    """Считает медианы hook_rate, ctr, cpl внутри каждой пиринговой группы.

    Также считает глобальные медианы для видео-сигнала (video_p25_rate и video_completion).
    Возвращает:
    {
        peer_key: {"hook_rate": float|None, "ctr": float|None, "cpl": float|None},
        ...
        "_video": {"p25_rate_median": float|None, "completion_ratio_median": float|None}
    }
    """
    # Группируем по пиринговой группе
    groups: dict[tuple, list] = {}
    for ad in ads:
        key = _peer_group_key(ad)
        groups.setdefault(key, []).append(ad)

    medians: dict = {}
    for key, group in groups.items():
        def _med(values):
            filtered = [v for v in values if v is not None and v > 0]
            return statistics.median(filtered) if filtered else None

        medians[key] = {
            "hook_rate": _med([ad.get("hook_rate") for ad in group]),
            "ctr": _med([ad.get("ctr") for ad in group]),
            # CPL: НИЖЕ медианы — плохо (значит дешёвый трафик = низкое качество)
            # поэтому считаем медиану CPL для сигнала «CPL не ниже медианы»
            "cpl": _med([float(ad.get("cpl") or 0) for ad in group]),
        }

    # Глобальные видео-медианы (только видео-объявления)
    video_ads = [ad for ad in ads if _is_video_ad(ad)]
    video_p25_rates = []
    video_completion_ratios = []

    for ad in video_ads:
        imp = int(ad.get("impressions") or 0)
        p25 = int(ad.get("video_p25") or 0)
        p100 = int(ad.get("video_p100") or 0)
        v3s = int(ad.get("video_views_3s") or 0)

        # Доля досмотревших 25% — используем video_p25 если есть, иначе video_views_3s как прокси
        if imp > 0:
            if p25 > 0:
                p25_rate = p25 / imp
            elif v3s > 0:
                p25_rate = v3s / imp
            else:
                p25_rate = None
        else:
            p25_rate = None

        # Коэффициент полного досмотра: video_p100 / video_p25 (если p25 > 0)
        if p25 > 0 and p100 >= 0:
            completion_ratio = p100 / p25
        elif v3s > 0 and p100 >= 0:
            # fallback: p100/v3s
            completion_ratio = p100 / v3s if v3s > 0 else None
        else:
            completion_ratio = None

        if p25_rate is not None:
            video_p25_rates.append(p25_rate)
        if completion_ratio is not None:
            video_completion_ratios.append(completion_ratio)

    def _med_raw(values):
        filtered = [v for v in values if v is not None]
        return statistics.median(filtered) if filtered else None

    medians["_video"] = {
        "p25_rate_median": _med_raw(video_p25_rates),
        "completion_ratio_median": _med_raw(video_completion_ratios),
    }

    return medians


# ---------------------------------------------------------------------------
# Ранний лид-сигнал (сигнал A: +3)
# ---------------------------------------------------------------------------

def _has_early_lead(ad: dict) -> bool:
    """Есть ли хотя бы 1 лид к 3-му дню от запуска.

    Ищет поле early_leads (предагрегированное число лидов за день 0-3) или
    daily_leads (список {day, leads}) или lead_day1/lead_day2/lead_day3.
    Если данных нет — возвращает False (не штрафуем за отсутствие поля).
    """
    # Вариант 1: поле early_leads (sum leads за день ≤3)
    early = _parse_nonnegative_int(ad.get("early_leads"))
    if early is not None:
        return early >= 1

    # Вариант 2: daily_leads = [{day_since_launch, leads}, ...]
    daily = ad.get("daily_leads")
    if isinstance(daily, list) and daily:
        for row in daily:
            if not isinstance(row, dict):
                continue
            day = _parse_nonnegative_int(row.get("day_since_launch"))
            if day is None:
                day = _parse_nonnegative_int(row.get("day"))
            leads = _parse_nonnegative_int(row.get("leads"))
            if day is not None and leads is not None and day <= 3 and leads >= 1:
                return True
        return False

    # Вариант 3: нет ранних данных, но общий leads > 0 и days_running <= 3
    days = _known_age_days(ad)
    leads = _parse_nonnegative_int(ad.get("leads"))
    if days is not None and leads is not None and days <= 3 and leads >= 1:
        return True

    return False


# ---------------------------------------------------------------------------
# Видео-сигнал «широкий вход, неглубокий хвост» (сигнал A: +2)
# ---------------------------------------------------------------------------

def _video_wide_entry_shallow_tail(ad: dict, video_medians: dict) -> bool:
    """
    Сигнал только для видео-объявлений:
    высокая доля досмотревших 25% (выше медианы) И низкий коэф полного досмотра (ниже медианы).

    Это означает: объявление хорошо «цепляет» в начале (много людей дошло до 25%),
    но досматривают до конца меньше чем в среднем — типичный паттерн широкого охвата.
    """
    if not _is_video_ad(ad):
        return False

    p25_med = video_medians.get("p25_rate_median")
    comp_med = video_medians.get("completion_ratio_median")

    # Нет данных для сравнения — не даём бонус
    if p25_med is None or comp_med is None:
        return False

    imp = int(ad.get("impressions") or 0)
    p25 = int(ad.get("video_p25") or 0)
    p100 = int(ad.get("video_p100") or 0)
    v3s = int(ad.get("video_views_3s") or 0)

    if imp <= 0:
        return False

    # Доля досмотревших 25%
    if p25 > 0:
        p25_rate = p25 / imp
    elif v3s > 0:
        p25_rate = v3s / imp
    else:
        return False

    # Коэффициент полного досмотра
    denominator = p25 if p25 > 0 else v3s
    if denominator <= 0:
        return False
    completion_ratio = p100 / denominator

    return p25_rate > p25_med and completion_ratio < comp_med


# ---------------------------------------------------------------------------
# Guardian: дефолты для ранних сигналов (день 1-3) и wasted_no_crm.
# Дублируются с services.guardian.GUARDIAN_DEFAULTS — единственный источник правды
# там (services/guardian.py), здесь только дефолты на случай если thresholds не
# передали guardian-ключи (чтобы decision_policy был самодостаточен и тестируем
# без импорта guardian).
# ---------------------------------------------------------------------------

_GUARDIAN_RULE_DEFAULTS: dict = {
    # Ранние сигналы (день 1-3)
    "early_dry_run": True,
    "early_min_age_hours": 24,
    "early_min_spend": 15.0,
    "early_day1_zero_spend": 25.0,
    "early_cpl_mult": 3.0,
    "early_day23_min_spend": 20.0,
    "early_qual_override_pct": 15.0,
    # Остаточная дыра wasted_no_crm
    "wnc_dry_run": True,
    "wnc_min_spend": 150.0,
    "wnc_min_leads": 10,
    "wnc_min_days": 3,
}


def _parse_nonnegative_int(value: object) -> int | None:
    """Строго и без потери читает целое >= 0; некорректное значение даёт None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0 or not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not re.fullmatch(r"\+?\d+", stripped):
            return None
        try:
            return int(stripped)
        except (ValueError, OverflowError):
            return None
    return None


def _known_age_days(ad: dict) -> int | None:
    """Возвращает минимальный известный неотрицательный возраст объявления."""
    candidates = [
        parsed
        for key in ("day_since_launch", "days_running")
        if (parsed := _parse_nonnegative_int(ad.get(key))) is not None
    ]
    return min(candidates) if candidates else None


def _is_zero_leads_after_3d(ad: dict) -> bool:
    """Проверяет точный lifetime-ноль после трёх полных дней с запуска."""
    age_days = _known_age_days(ad)
    if age_days is None or age_days < 3:
        return False
    return _parse_nonnegative_int(ad.get("leads")) == 0


def _day_since_launch(ad: dict) -> int:
    """Возвращает «возраст» объявления в днях — минимум из day_since_launch и days_running.

    Оба поля — прокси возраста объявления, но могут расходиться (агрегация по-разному
    считает день 0). Берём минимум как более консервативную (раннюю) оценку — так
    early-правила не пропустят молодое объявление, если одно из полей запаздывает.
    """
    age_days = _known_age_days(ad)
    if age_days is None:
        return 99  # нет данных о возрасте — считаем «старое», ранние правила не тронут
    return age_days


def _city_cpl_median(ads: list[dict]) -> dict[str, float]:
    """Медиана CPL активных объявлений по городу (только cpl > 0).

    Используется для сравнения «CPL объявления Х выше медианы города в N раз» в
    правиле early_waster (день 2-3). Город без ни одного cpl>0 — не попадает в
    словарь (вызывающий код должен трактовать отсутствие ключа как «нет медианы»).
    """
    by_city: dict[str, list[float]] = {}
    for ad in ads:
        city = ad.get("city") or ""
        cpl = float(ad.get("cpl") or 0)
        if city and cpl > 0:
            by_city.setdefault(city, []).append(cpl)

    return {city: statistics.median(values) for city, values in by_city.items() if values}


def _is_early_waster(
    ad: dict,
    city_cpl_medians: dict[str, float],
    t: dict,
    use_erp_payments: bool = False,
) -> bool:
    """Ранний слив (день 1-3): базовые защиты + хотя бы одна аномалия. См. §6.2 спеки.

    use_erp_payments — источник-каскад оплат (§6.5 ARCH-cdp-payments) для
    quality-override: False (amo/shadow, дефолт) = ad.get("payments") как раньше;
    True (erp) = payments_effective(ad) = max(AMO, ERP).
    """
    day = _day_since_launch(ad)
    days_running = _parse_nonnegative_int(ad.get("days_running")) or 0
    day_since_launch = _parse_nonnegative_int(ad.get("day_since_launch")) or 0
    spend = float(ad.get("spend") or 0)
    leads = _parse_nonnegative_int(ad.get("leads")) or 0
    cpl = float(ad.get("cpl") or 0)

    # --- Базовые защиты ---
    if day > 3:
        return False
    if spend < float(t["early_min_spend"]):
        return False
    # Возраст: не трогаем объявления, ещё не пережившие learning-фазу первого дня.
    # Прокси возраста >= early_min_age_hours: days_running>=1 ИЛИ day_since_launch>=1.
    # Если оба 0 — объявление запущено сегодня, моложе суток.
    if days_running == 0 and day_since_launch == 0:
        return False

    # --- Аномалии (хотя бы одна) ---

    # День 1: потратили заметно, но 0 лидов
    if day <= 1 and spend > float(t["early_day1_zero_spend"]) and leads == 0:
        return True

    # День 2-3: CPL заметно (× mult) выше медианы города
    if 2 <= day <= 3 and spend >= float(t["early_day23_min_spend"]) and cpl > 0:
        city = ad.get("city") or ""
        city_median = city_cpl_medians.get(city)
        if city_median is not None and cpl > float(t["early_cpl_mult"]) * city_median:
            # Quality-override: дорогой лид — ещё не значит слив, если он окупается
            # или квалифицируется (политика: при хорошем квале и ROMI
            # дорогой лид допустим). Проверяем в порядке силы сигнала:
            # 1) реальные деньги (payments) — безусловный override, конфиг не нужен;
            # 2) qual_pct >= порога — качественный лид, даже если дорогой.
            payments = _eff_payments(ad, use_erp_payments)
            if payments is not None and payments > 0:
                return False
            qual_pct = ad.get("qual_pct")
            if qual_pct is not None and qual_pct >= float(t["early_qual_override_pct"]):
                return False
            return True

    return False


def _is_wasted_no_crm(ad: dict, t: dict) -> bool:
    """Дыра: расход+лиды есть, но 0 матчей AMO >= N дней (сломана связка CRM). См. §6.2."""
    spend = float(ad.get("spend") or 0)
    leads = _parse_nonnegative_int(ad.get("leads")) or 0
    matched = ad.get("outcomes_matched_at")  # None = сверка ни разу не проходила
    days = _known_age_days(ad) or 0
    return (
        spend > float(t["wnc_min_spend"])
        and leads >= int(t["wnc_min_leads"])
        and matched is None
        and days >= int(t["wnc_min_days"])
    )


def _cpl_vs_median_phrase(cpl: float | None, cpl_med: float | None) -> str:
    """Человеческая фраза сравнения CPL рекламы с медианой её группы.

    Возвращает "" если данных нет или разница незначительна (<30%). Иначе —
    «CPL $8.2 — дороже медианы группы $4.2» (оба реальных числа, без жаргона
    «×N раз»). Деньги — через fmt_money (единый формат денег).
    """
    if not cpl or cpl <= 0 or not cpl_med or cpl_med <= 0:
        return ""
    if cpl < 1.3 * cpl_med:
        return ""
    return f"CPL {fmt_money(cpl, '$')} — дороже медианы группы {fmt_money(cpl_med, '$')}"


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------

def _trend_flags(
    status: Optional[str] = None,
    *,
    reinforced: bool = False,
    vetoed_scale: bool = False,
    shadow: bool = False,
) -> dict:
    """Поля тренда в результате решения. Форма одинакова при любом исходе."""
    return {
        "trend_status": status,
        "trend_reinforced": reinforced,
        "trend_vetoed_scale": vetoed_scale,
        "trend_shadow": shadow,
    }


def _apply_trend_weight(
    *,
    trend_ctx,
    adset_id: str,
    action: str,
    business_reason: str,
    reasons: list[str],
    is_tier_b_diagnostic: bool,
    spend: float,
    qual_pct,
) -> tuple[str, str, dict]:
    """Добавляет вес тренда к уже принятому решению. Мутирует только reasons.

    Правила (ровно два применения тренда, см. services/trend_gate.py):
      (а) SCALE + action-grade ПАДЕНИЕ адсета → KEEP. Тренд снимает подъём, но
          сам подъёма не даёт: РОСТ здесь не рассматривается вовсе.
      (б) ПАДЕНИЕ добавляется к паузе ТОЛЬКО если объявление и без тренда
          выглядит слабым:
            • action == "PAUSE" — пауза уже обоснована, тренд её усиливает
              (исход не меняется, меняется объяснение и флаг);
            • тир B (сверка прошла, 0 оплат, расход выше порога, квал ниже
              порога) — единственный случай, где тренд ПЕРЕВОДИТ исход в PAUSE.
              Тир B и до волны 3 считался слабым, ему не хватало ровно
              доказательства зрелости — его и даёт подтверждённое трёхнедельное
              падение квала на адсете.
          Во всех остальных случаях тренд остаётся диагностикой: паузу сам по
          себе он не порождает.

    В режиме shadow ни один исход не меняется — только строка в reasons с
    пометкой «тень: не применено» и флаг trend_shadow.

    Returns:
        (action, business_reason, trend_flags)
    """
    if trend_ctx is None or not adset_id:
        return action, business_reason, _trend_flags()

    verdict = trend_ctx.verdict_for(adset_id)
    status = verdict.status.value if verdict is not None else None

    decline = trend_ctx.decline_for(adset_id)
    if decline is None:
        # МАЛО ДАННЫХ / ПЛАТО / РОСТ / уровень объявления — ничего не меняем.
        return action, business_reason, _trend_flags(status)

    phrase = trend_ctx.describe(decline)
    is_active = trend_ctx.is_active

    if action == "SCALE":
        if not is_active:
            reasons.append(trend_ctx.mark_shadow(f"вето подъёма: {phrase}"))
            return action, business_reason, _trend_flags(status, shadow=True)
        reasons.append(f"вето подъёма: {phrase}")
        return "KEEP", business_reason, _trend_flags(status, vetoed_scale=True)

    weak_enough = action == "PAUSE" or is_tier_b_diagnostic
    if not weak_enough:
        reasons.append(
            f"diagnostic: {phrase}; сам по себе тренд паузу не даёт"
        )
        return action, business_reason, _trend_flags(status)

    if not is_active:
        reasons.append(trend_ctx.mark_shadow(f"вес к паузе: {phrase}"))
        return action, business_reason, _trend_flags(status, shadow=True)

    reasons.append(f"вес к паузе: {phrase}")
    if action != "PAUSE":
        action = "PAUSE"
        qual_text = f"{float(qual_pct):.0f}%" if qual_pct is not None else "нет данных"
        business_reason = (
            f"квал {qual_text} при расходе {fmt_money(spend, '$')} и 0 оплат; {phrase}"
        )
    return action, business_reason, _trend_flags(status, reinforced=True)


def score_and_decide(
    ads: list[dict],
    thresholds: Optional[dict] = None,
    trend_ctx=None,
) -> list[dict]:
    """Выдаёт рекомендацию по каждому объявлению.

    Параметры:
        ads       — список объявлений с метриками (dict). Ожидаемые ключи:
                    ad_id / id, ad_name / name, adset_id, city, adset_type,
                    spend, leads, qual_pct, romi, cpl, ctr, hook_rate, hold_rate,
                    impressions, video_views_3s, video_p25, video_p50, video_p75, video_p100,
                    days_running, early_leads (опц.), daily_leads (опц.)
        thresholds — переопределение порогов (опционально)
        trend_ctx  — снимок тренда недельных когорт на прогон
                     (services.trend_gate.TrendContext) или None. Модуль
                     остаётся чистым: контекст читает вызывающий (автопилот),
                     здесь только применяется. None = тренд не участвует, все
                     исходы ровно такие же, как до волны 3.

    Возвращает:
        список dict: {ad_id, ad_name, adset_id, action, score, reasons: [str]}
        action: "SCALE" | "KEEP" | "PAUSE"

    Важно: ничего не паузит и не запускает — только считает.
    """
    t = {
        # Минимальный скор для SCALE
        "scale_min_score": 5,
        # Минимальный qual_pct для KEEP (не PAUSE)
        "min_qual_pct": 10.0,
        # Порог ROMI для явного KEEP (даже без qual)
        "min_romi": 100.0,
        # Guardian: ранние сигналы (день 1-3) + wasted_no_crm — дефолты,
        # переопределяются через thresholds (Guardian передаёт свой конфиг).
        **_GUARDIAN_RULE_DEFAULTS,
        **(thresholds or {}),
    }

    if not ads:
        return []

    # Источник-каскад оплат (§6.5 ARCH-cdp-payments): thresholds — это блок
    # "thresholds" автопилота (Guardian передаёт combined_thresholds), а не весь
    # autopilot-конфиг — поэтому payments_source читаем напрямую из autopilot.cdp.
    from services.autopilot import get_autopilot_config
    _cdp_cfg = (get_autopilot_config() or {}).get("cdp") or {}
    _payments_source = _cdp_cfg.get("payments_source", "shadow")
    _use_erp_payments = _payments_source == "erp"

    # Нормализуем ключи: поддерживаем и "id" и "ad_id", "name" и "ad_name"
    normalized: list[dict] = []
    for ad in ads:
        norm = dict(ad)
        if "ad_id" not in norm:
            norm["ad_id"] = norm.get("id") or ""
        if "ad_name" not in norm:
            norm["ad_name"] = norm.get("name") or ""
        if "adset_id" not in norm:
            norm["adset_id"] = norm.get("adset_id") or ""
        # Age/leads приходят из нескольких внешних контуров. Нормализуем их
        # строго до legacy-скоринга, чтобы malformed значение не уронило batch.
        for key in ("leads", "days_running", "day_since_launch"):
            if key in norm:
                norm[key] = _parse_nonnegative_int(norm[key])
        normalized.append(norm)

    # Считаем медианы для относительного сравнения
    peer_medians = _compute_peer_medians(normalized)
    video_medians = peer_medians.get("_video", {})
    # Guardian: медиана CPL по городу — для правила early_waster (день 2-3)
    city_cpl_medians = _city_cpl_median(normalized)

    # --- Шаг 1: портфельный ранг через apply_portfolio_decisions ---
    # Переиспользуем существующую функцию — добавляем временные поля
    # которые она ожидает: effective_status, recommendation, reason
    for ad in normalized:
        ad.setdefault("effective_status", "ACTIVE")
        ad.setdefault("recommendation", "ЖДАТЬ")
        ad.setdefault("reason", "")

    from agent.analyzer import apply_portfolio_decisions
    apply_portfolio_decisions(normalized)

    # Собираем portfolio_action по ad_id для использования в правилах части B
    portfolio_actions: dict[str, str] = {
        ad["ad_id"]: ad.get("recommendation", "ЖДАТЬ")
        for ad in normalized
    }

    # --- Шаг 2: считаем score и action для каждого ---
    results: list[dict] = []
    for ad in normalized:
        ad_id = ad["ad_id"]
        ad_name = ad["ad_name"]
        adset_id = ad.get("adset_id") or ""
        score = 0
        reasons: list[str] = []

        # --- Часть A: Scale-скоринг (ранние сигналы) ---

        # ВЕТО: тема-вето (_VETO_KEYWORD) блокирует SCALE
        veto = _has_veto_theme(ad_name)
        if veto:
            reasons.append(f"вето: тема «{_VETO_KEYWORD}» (тема-вето политики)")

        if not veto:
            # +3: есть ≥1 лид к 3-му дню
            if _has_early_lead(ad):
                score += 3
                reasons.append("+3: есть лид в первые 3 дня")

            # +2: видео «широкий вход + неглубокий хвост»
            if _video_wide_entry_shallow_tail(ad, video_medians):
                score += 2
                reasons.append("+2: видео — высокая доля 25% досмотра, низкий полный досмотр (широкий вход)")
            elif _is_video_ad(ad):
                reasons.append("видео: нет паттерна «широкий вход» (или нет данных для сравнения)")

            # +2: hook_rate ИЛИ ctr выше медианы пиринговой группы
            peer_key = _peer_group_key(ad)
            peer_med = peer_medians.get(peer_key, {})
            hook_rate = ad.get("hook_rate")
            ctr = float(ad.get("ctr") or 0)
            hook_med = peer_med.get("hook_rate")
            ctr_med = peer_med.get("ctr")

            hook_above = hook_rate is not None and hook_med is not None and hook_rate > hook_med
            ctr_above = ctr_med is not None and ctr > ctr_med

            if hook_above or ctr_above:
                score += 2
                which = []
                if hook_above:
                    which.append(f"hook_rate {hook_rate:.1f}% > медиана {hook_med:.1f}%")
                if ctr_above:
                    which.append(f"ctr {ctr:.2f}% > медиана {ctr_med:.2f}%")
                reasons.append(f"+2: {', '.join(which)} в группе {peer_key[0]}/{peer_key[1]}")
            else:
                if hook_med is not None or ctr_med is not None:
                    reasons.append("hook/ctr не выше медианы группы")

            # +1: ранний CPL НЕ ниже медианы группы (дешёвый CPL ранний = плохо)
            cpl = float(ad.get("cpl") or 0)
            cpl_med = peer_med.get("cpl")
            if cpl_med is not None and cpl > 0 and cpl >= cpl_med:
                score += 1
                reasons.append(f"+1: CPL {cpl:.1f} ≥ медиана {cpl_med:.1f} (не дешёвый мусорный трафик)")
            elif cpl_med is not None and cpl > 0:
                reasons.append(f"CPL {cpl:.1f} < медиана {cpl_med:.1f} (ранний дешёвый — осторожно)")

            # +1: тема эмоция/оффер
            if _has_emotion_theme(ad_name):
                score += 1
                reasons.append("+1: тема с эмоцией/оффером в названии")
            else:
                reasons.append("нет ключевых слов эмоции/оффера в названии")

        # --- Бизнес-причина для Telegram (собираем В МОМЕНТ решения, не парсим) ---
        business_reason = ""  # человеческая deцisive-часть; "" если правило не сработало
        peer_med_b = peer_medians.get(_peer_group_key(ad), {})
        _cpl_phrase = _cpl_vs_median_phrase(float(ad.get("cpl") or 0), peer_med_b.get("cpl"))

        # --- Часть B: Outcome/Pause-правила (зрелые объявления) ---

        parsed_leads = _parse_nonnegative_int(ad.get("leads"))
        leads = parsed_leads or 0
        zero_leads_age_days = _known_age_days(ad)
        is_zero_leads_after_3d = _is_zero_leads_after_3d(ad)
        qual_pct = ad.get("qual_pct")
        romi = ad.get("romi")
        # Источник-каскад оплат (§6.5): amo/shadow — ad.get("payments") как раньше;
        # erp — payments_effective(ad) = max(AMO, ERP), fail-closed (сигнал не теряется).
        payments = _eff_payments(ad, _use_erp_payments)
        # B1: None = сверка с AMO не проводилась (attach_amo_outcomes ещё не отработал
        # по этому объявлению). Без сверки payments=0 значит «нет данных», а не «точно 0».
        outcomes_matched_at = ad.get("outcomes_matched_at")
        spend = float(ad.get("spend") or 0)
        portfolio_action = portfolio_actions.get(ad_id, "ЖДАТЬ")

        # --- ПРАВИЛО ПРИОРИТЕТ 0: подтверждённый слив (тир A) ---
        # payments == 0 строго (НЕ None — None означает «нет данных», не паузим).
        # B1: ДОПОЛНИТЕЛЬНО требуем outcomes_matched_at IS NOT NULL — то есть сверка
        # с AMO реально прошла. Без сверки payments=0 может значить «просто нет данных»
        # (потерянная связка CRM), а не «точно 0 оплат» — такую рекламу НЕ паузим.
        #
        # ТИР A: большой расход + реальные лиды + 0 оплат → слив, квал вторичен.
        #   spend > waster_high_spend(300) AND leads >= waster_min_leads(15)
        #   AND (qual_pct is None OR qual_pct < waster_high_qual_cap(25))
        #   Страховка: если qual_pct >= 25% — лиды квалятся хорошо, оплата скорее всего
        #   просто в лаге → НЕ режем. Пример: $500 / 10% / 0 опл / 50 лид → слив.
        #
        # ТИР B: меньший расход, явно мусорный квал — только диагностика.
        #   spend > waster_min_spend(150) AND qual_pct is not None AND qual_pct < min_qual_pct(10)
        #   Пример: $200 / 5% / 0 опл → диагностический сигнал.
        waster_high_spend   = float(t.get("waster_high_spend",   300))
        waster_min_leads    = int(t.get("waster_min_leads",       15))
        waster_high_qual_cap = float(t.get("waster_high_qual_cap", 25))
        waster_min_spend    = float(t.get("waster_min_spend",     150))
        min_qual_threshold  = float(t.get("min_qual_pct",         10.0))

        _tier_a = (
            spend > waster_high_spend
            and leads >= waster_min_leads
            and (qual_pct is None or float(qual_pct) < waster_high_qual_cap)
        )
        _tier_b = (
            spend > waster_min_spend
            and qual_pct is not None
            and float(qual_pct) < min_qual_threshold
        )
        # B1: тир A подтверждаем только после факта сверки с AMO
        # (outcomes_matched_at не NULL) И payments==0. Сам timestamp не доказывает,
        # что когорта лидов созрела, поэтому тир B остаётся диагностикой.
        _outcomes_confirmed = outcomes_matched_at is not None
        # Гейт цикла оплаты: оплата зреет ~14 дней (основная масса оплат — за
        # 14 дн. от лида) — у объявления моложе этого срока «оплат 0» не
        # доказательство слива, а физика цикла. Без гейта тир A самоодобрял
        # паузы молодых рабочих реклам (квал хороший, но оплат ещё нет).
        # Неизвестный возраст = не кандидат (fail-closed, как в автопилоте).
        _payment_maturity_days = int(t.get("waster_payment_maturity_days", 14))
        _age_days_known = _known_age_days(ad)
        _payments_could_mature = (
            _age_days_known is not None and _age_days_known >= _payment_maturity_days
        )
        is_confirmed_waster = (
            _outcomes_confirmed and (payments == 0) and _tier_a and _payments_could_mature
        )
        is_tier_b_diagnostic = _outcomes_confirmed and (payments == 0) and _tier_b

        # Правило «3+ дня и 0 лидов» судит по календарю, а не по деньгам: к третьему
        # дню такие объявления обычно потратили центы (а заметная часть потом
        # просыпается). Политика: его место
        # занимает ранний стоп по расходу (services/early_kill.py). Флаг
        # thresholds.zero_leads_rule_enabled выключает только action, сам признак
        # считается по-прежнему — он нужен отчётам.
        zero_leads_rule_enabled = t.get("zero_leads_rule_enabled", True) is not False

        # Определяем action по всей логике
        if is_zero_leads_after_3d and zero_leads_rule_enabled:
            action = "PAUSE"
            reasons.append(
                "PAUSE: 3+ полных дня с запуска и 0 lifetime-лидов "
                f"(возраст {zero_leads_age_days} дн.)"
            )
            business_reason = (
                f"за {zero_leads_age_days} полных дн. с запуска не получено ни одного лида"
            )
        elif is_confirmed_waster:
            action = "PAUSE"
            qual_str = f"{float(qual_pct):.0f}%" if qual_pct is not None else "нет данных"
            reasons.append(
                "PAUSE: подтверждённый слив (тир A) — "
                f"расход ${spend:.0f}, 0 оплат, квал {qual_str}, лидов {leads}"
            )
            # Бизнес-причина: деньги уходят, продаж нет
            business_reason = (
                f"{leads} {pluralize_leads(leads)}, оплат 0 при расходе "
                f"{fmt_money(spend, '$')} — деньги уходят, продаж нет"
            )
            if qual_pct is not None:
                business_reason += f", квал {float(qual_pct):.0f}%"
            if _cpl_phrase:
                business_reason += f"; {_cpl_phrase}"
        elif score >= t["scale_min_score"]:
            action = "SCALE"
            # business_reason остаётся "" (SCALE в Telegram-паузы не попадает)
        elif portfolio_action == "ОТКЛЮЧИТЬ":
            # Портфельный аутсайдер → PAUSE
            action = "PAUSE"
            reasons.append(f"PAUSE: портфельный аутсайдер ({ad.get('reason', '')})")
            business_reason = "слабее похожих реклам в своей группе (город/тип)"
        elif (
            romi is not None and romi >= t["min_romi"]
        ) or (
            qual_pct is not None and qual_pct >= t["min_qual_pct"]
        ):
            action = "KEEP"
            if romi is not None and romi >= t["min_romi"]:
                reasons.append(f"KEEP: ROMI {romi:.0f}% (выше порога {t['min_romi']:.0f}%)")
            if qual_pct is not None and qual_pct >= t["min_qual_pct"]:
                reasons.append(f"KEEP: qual_pct {qual_pct:.0f}% (выше порога {t['min_qual_pct']:.0f}%)")
        else:
            action = "KEEP"  # по умолчанию держим (не хватает данных для уверенного PAUSE)
            reasons.append("KEEP: недостаточно данных для уверенного решения")

        # Незрелые quality-сигналы остаются в выводе, но не меняют action.
        if leads > 0 and qual_pct is not None and qual_pct == 0:
            quality_diagnostic = (
                f"diagnostic: {leads} {pluralize_leads(leads)}, qual_pct=0; "
                "без зрелой выборки это не причина для PAUSE"
            )
            if _cpl_phrase:
                quality_diagnostic += f"; {_cpl_phrase}"
            reasons.append(quality_diagnostic)

        if is_tier_b_diagnostic and not is_confirmed_waster:
            reasons.append(
                f"diagnostic: тир B — расход ${spend:.0f}, "
                f"квал {float(qual_pct):.0f}%, 0 оплат; "
                "outcomes_matched_at подтверждает сверку, но не зрелость лидов"
            )

        # --- Тренд недельных когорт (волна 3): ВЕС, а не самостоятельное основание ---
        # Тренд считается на АДСЕТЕ и применяется одинаково ко всем его
        # объявлениям. Подключается ПОСЛЕ каскада и НЕ участвует ни в медианах
        # пиринговой группы, ни в портфельном ранге (то и другое посчитано выше
        # по сырым метрикам) — относительные сравнения внутри группы остаются
        # ровно такими же, как без тренда.
        #
        # Асимметрия — главный предохранитель волны 3: падающий тренд может
        # только СНЯТЬ подъём и УСИЛИТЬ паузу. Растущий не даёт ни SCALE, ни
        # KEEP: тренд не поднимает бюджеты ни в каком режиме.
        (
            action,
            business_reason,
            trend_flags,
        ) = _apply_trend_weight(
            trend_ctx=trend_ctx,
            adset_id=adset_id,
            action=action,
            business_reason=business_reason,
            reasons=reasons,
            is_tier_b_diagnostic=is_tier_b_diagnostic,
            spend=spend,
            qual_pct=qual_pct,
        )

        # --- Guardian: ранние сигналы (день 1-3) + wasted_no_crm ---
        # Считаются ВСЕГДА для алертов и наблюдаемости, но никогда не меняют action.
        is_early_waster = _is_early_waster(ad, city_cpl_medians, t, use_erp_payments=_use_erp_payments)
        is_wasted_no_crm = _is_wasted_no_crm(ad, t)

        if is_early_waster:
            detail = (
                f"день {_day_since_launch(ad)}, расход ${spend:.0f}, "
                f"лидов {leads}, CPL {ad.get('cpl') or 0}"
            )
            label = "dry_run" if t["early_dry_run"] else "diagnostic"
            reasons.append(f"{label}: ранний слив (день 1-3) — {detail}")

        if is_wasted_no_crm:
            detail = f"расход ${spend:.0f}, лидов {leads}, дней без сверки {ad.get('days_running') or ad.get('day_since_launch') or 0}"
            label = "dry_run" if t["wnc_dry_run"] else "diagnostic"
            reasons.append(f"{label}: wasted_no_crm — {detail}")

        results.append({
            "ad_id": ad_id,
            "ad_name": ad_name,
            "adset_id": adset_id,
            "action": action,
            "score": score,
            "reasons": reasons,
            # Готовая человеческая бизнес-причина (для Telegram), собранная в момент
            # решения — не парсим reasons постфактум. "" если бизнес-правило не сработало.
            "business_reason": business_reason,
            # Флаг подтверждённого слива — используется в guardrail и сортировке autopilot
            "is_confirmed_waster": is_confirmed_waster,
            "is_tier_b_diagnostic": is_tier_b_diagnostic,
            # Guardian: ранние сигналы (см. docs/specs/ARCH-phase1-guardian.md §6.2)
            "is_early_waster": is_early_waster,
            "is_wasted_no_crm": is_wasted_no_crm,
            "is_zero_leads_after_3d": is_zero_leads_after_3d,
            # Тренд недельных когорт (волна 3) — наблюдаемость и вход удержания
            **trend_flags,
        })

    # --- Шаг 3: Guardrail — не PAUSE последнее активное в adset ---
    _apply_last_in_adset_guardrail(results)

    return results


def _apply_last_in_adset_guardrail(results: list[dict]) -> None:
    """Guardrail C: если все кандидаты в adset → PAUSE, лучшему по score ставим KEEP.

    Guardrail применяется и к confirmed_waster: последнее объявление можно
    выключить только после подтверждения, что другая реклама адсета ACTIVE.

    Мутирует список на месте (аналогично apply_portfolio_decisions).
    """
    # Группируем по adset_id
    by_adset: dict[str, list[dict]] = {}
    for r in results:
        adset_id = r.get("adset_id") or ""
        by_adset.setdefault(adset_id, []).append(r)

    for adset_id, group in by_adset.items():
        # Пустой adset_id — не применяем guardrail (неизвестная группа)
        if not adset_id:
            continue

        pause_candidates = [r for r in group if r["action"] == "PAUSE"]

        # Все в группе PAUSE → всегда защищаем лучшего кандидата.
        if len(pause_candidates) == len(group):
            best = max(pause_candidates, key=lambda r: r["score"])
            best["action"] = "KEEP"
            best["reasons"].append(
                "KEEP: защита — последняя реклама в адсете ожидает ACTIVE-замену"
            )
