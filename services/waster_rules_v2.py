"""Подтверждённый слив v2 — два правила, откалиброванные ретро-симуляцией.

Ретро-симуляция на исторических данных (N дней AMO-лидов и объявлений): у
прежнего критерия «тир A» большинство сработок оказались ложными — реклама
оживала после точки среза (бот срезал бы заметный работающий хвост).
Причина: квал% считался по ВСЕМ лидам окна, а заметная часть квалов дня-0
дозревает позже (цикл квалификации: основная масса за 3 полных дня); незрелые
лиды разбавляли квал вниз, и это компенсировали поздним порогом $300.

Правила v2 считают только по зрелому — и потому могут быть ранними:

* R1 «зрелый ноль» — лидов достаточно, а квалов среди ЗРЕЛЫХ ровно ноль.
  На истории — единичные ложные сработки.
* R2 «мёртвая тишина» — расход капает, а лидов нет уже целое окно.
  На истории — без ложных сработок; окно 5 дней вместо 7 давало заметную
  долю ложных — семь дней это граница шума, не каприз.

Оба правила отвечают за РАЗНЫЕ классы слива: «лиды есть — квалов нет» и
«расход есть — лидов нет». Прежний тир A остаётся генератором предложений
с кнопкой; автономия сидит только на v2.

Fail-closed: любая ошибка сбора данных даёт «не слив» и лог — недоказанное
не режется.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

logger = logging.getLogger(__name__)

_TZ_LOCAL = timezone(timedelta(hours=5))
_KB_PATH = Path(__file__).resolve().parent.parent / "data" / "decisions.db"

# --- R1 «зрелый ноль» -------------------------------------------------------
# Зрелый лид = created не позже (сегодня − 3 полных дня): судьба подавляющей
# части лидов по квалу к этому моменту уже известна (замер цикла квалификации
# на исторических данных).
MATURITY_DAYS = 3
R1_MIN_MATURE_LEADS = 15
R1_MIN_SPEND_USD = 150.0
R1_MIN_AGE_DAYS = 5  # соответствует min_days_protect автопилота

# --- R2 «мёртвая тишина» ----------------------------------------------------
R2_WINDOW_DAYS = 7
R2_MIN_WINDOW_SPEND_USD = 50.0
R2_MIN_AGE_DAYS = 10


@dataclass(frozen=True, slots=True)
class WasterVerdict:
    is_waster: bool
    rule: str  # "R1_MATURE_ZERO" | "R2_DEAD_SILENCE" | "NONE" | "DATA_ERROR"
    detail: str


# ---------------------------------------------------------------------------
# Чистые правила (тестируются без сети и БД)
# ---------------------------------------------------------------------------

def mature_zero_verdict(
    lead_events: Sequence[tuple[date, bool]],
    total_spend_usd: float,
    first_spend_day: date,
    today: date,
) -> WasterVerdict:
    """R1: зрелых лидов достаточно, а зрелых квалов — ровно ноль.

    Дополнительный замок «не ожила»: ни одного квала и среди незрелых лидов.
    Ноль требуем строгий — серая зона 1–24% остаётся предложением владельцу.
    """
    age_days = (today - first_spend_day).days
    if age_days < R1_MIN_AGE_DAYS:
        return WasterVerdict(False, "NONE", f"молода: {age_days}д")
    maturity_cutoff = today - timedelta(days=MATURITY_DAYS)
    mature = [(d, q) for d, q in lead_events if d <= maturity_cutoff]
    if len(mature) < R1_MIN_MATURE_LEADS:
        return WasterVerdict(False, "NONE", f"зрелых лидов {len(mature)}")
    if any(q for _, q in mature):
        return WasterVerdict(False, "NONE", "есть зрелые квалы")
    if any(q for d, q in lead_events if d > maturity_cutoff):
        return WasterVerdict(False, "NONE", "ожила: свежий квал")
    if total_spend_usd < R1_MIN_SPEND_USD:
        return WasterVerdict(False, "NONE", f"расход ${total_spend_usd:.0f}")
    return WasterVerdict(
        True,
        "R1_MATURE_ZERO",
        f"{len(mature)} зрелых лидов, 0 квалов, ${total_spend_usd:.0f}",
    )


def dead_silence_verdict(
    daily_spend: Mapping[date, float],
    lead_dates: Sequence[date],
    first_spend_day: date,
    today: date,
) -> WasterVerdict:
    """R2: за окно расход капает, а лидов нет вообще."""
    age_days = (today - first_spend_day).days
    if age_days < R2_MIN_AGE_DAYS:
        return WasterVerdict(False, "NONE", f"молода: {age_days}д")
    window_start = today - timedelta(days=R2_WINDOW_DAYS)
    window_spend = sum(
        spend for day, spend in daily_spend.items() if window_start < day <= today
    )
    if window_spend < R2_MIN_WINDOW_SPEND_USD:
        return WasterVerdict(False, "NONE", f"окно ${window_spend:.0f}")
    if any(window_start < d <= today for d in lead_dates):
        return WasterVerdict(False, "NONE", "в окне есть лиды")
    return WasterVerdict(
        True,
        "R2_DEAD_SILENCE",
        f"{R2_WINDOW_DAYS}д тишины при ${window_spend:.0f}",
    )


# ---------------------------------------------------------------------------
# Сбор данных (кандидатов ≤6/день — точечные чтения дёшевы)
# ---------------------------------------------------------------------------

def _load_daily_spend(ad_id: str, since: date) -> dict[date, float]:
    conn = sqlite3.connect(f"file:{_KB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT date, spend FROM ad_daily_metrics WHERE ad_id = ? AND date >= ?",
            (ad_id, since.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    return {date.fromisoformat(str(d)): float(s or 0) for d, s in rows}


def _normalize_raw_lead(lead: dict) -> dict:
    """Сырой v4-лид AMO → формат хелперов integrations.amo.

    _amo_get отдаёт лиды как есть: кастомные поля лежат в
    ``custom_fields_values``, теги — в ``_embedded.tags``. Хелперы
    (_is_qualified/_extract_fb_fields/_is_service_lead) читают
    ``custom_fields`` и ``tags`` — нормализованный формат get_leads.
    Без моста сырой лид для них пуст: R1 «зрелый ноль» не видел ни одного
    лида (0 событий на объявлении, у которого в AMO есть лиды), а R2 ставил
    «тишину» объявлениям с лидами.
    """
    return {
        "custom_fields": lead.get("custom_fields_values") or [],
        "tags": ((lead.get("_embedded") or {}).get("tags")) or [],
    }


def _load_lead_events(ad_id: str, since: date) -> list[tuple[date, bool]]:
    """Точечные AMO-лиды объявления: (дата создания, квал сейчас).

    Обёртка над _load_lead_records с дневной точностью — её ждут R1/R2.
    """
    return [(moment.date(), qual) for moment, qual in _load_lead_records(ad_id, since)]


def _load_lead_records(ad_id: str, since: date) -> list[tuple[datetime, bool]]:
    """Точечные AMO-лиды объявления: (момент создания в локальной TZ, квал сейчас).

    query=<ad_id> — единственный работающий точечный поиск (filter по
    custom_fields в этом AMO отдаёт 400). query ищет подстроку, поэтому
    каждый лид перепроверяется точным сравнением fb_ad_id. Часовая точность
    нужна раннему стопу (services/early_kill.py): зрелость заявки — 72 часа.
    """
    from integrations.amo import (
        _amo_get,
        _extract_fb_fields,
        _is_qualified,
        _is_service_lead,
    )

    events: list[tuple[datetime, bool]] = []
    page = 1
    while page <= 10:
        payload = _amo_get(
            "leads",
            {
                "query": ad_id,
                "with": "contacts,tags",
                "limit": 250,
                "page": page,
                "filter[created_at][from]": int(
                    datetime.combine(since, datetime.min.time(), _TZ_LOCAL).timestamp()
                ),
            },
        )
        leads = ((payload or {}).get("_embedded") or {}).get("leads") or []
        for lead in leads:
            normalized = _normalize_raw_lead(lead)
            if _is_service_lead(normalized):
                continue
            if _extract_fb_fields(normalized).get("ad_id") != ad_id:
                continue
            created = datetime.fromtimestamp(
                int(lead.get("created_at") or 0), _TZ_LOCAL
            )
            events.append((created, _is_qualified(normalized)))
        if len(leads) < 250:
            break
        page += 1
    return events


def confirmed_waster_v2(ad_id: str, now: datetime | None = None) -> WasterVerdict:
    """Главная точка: слив ли объявление по правилам v2. Fail-closed."""
    today = (now or datetime.now(_TZ_LOCAL)).astimezone(_TZ_LOCAL).date()
    since = today - timedelta(days=45)
    try:
        daily_spend = _load_daily_spend(ad_id, since)
        if not daily_spend:
            return WasterVerdict(False, "DATA_ERROR", "нет дневного расхода в KB")
        first_spend_day = min(daily_spend)
        lead_events = _load_lead_events(ad_id, since)
    except Exception as exc:  # noqa: BLE001 — недоказанное не режется
        logger.warning("waster_rules_v2: сбор данных %s упал — %s", ad_id, exc)
        return WasterVerdict(False, "DATA_ERROR", str(exc)[:80])

    r2 = dead_silence_verdict(
        daily_spend, [d for d, _ in lead_events], first_spend_day, today
    )
    if r2.is_waster:
        return r2
    total_spend = sum(daily_spend.values())
    return mature_zero_verdict(lead_events, total_spend, first_spend_day, today)
