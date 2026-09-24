"""Правило «дорогой без выручки»: большой расход, зрелые лиды, ноль продаж.

Мотив (пример): креатив A — заметный расход, пара квалов, 0 продаж; креатив
B — расход больше, несколько квалов, 0 продаж. Для правил R1/R2 такие
объявления «рабочие», ведь квалы есть. Класс «квал есть, денег нет» автономия
не видела вовсе. Правило: расход ≥$500, лидам было ≥14 дней дозреть до оплаты
(цикл оплаты: основная масса оплат — за 14 дней), зрелых лидов достаточно,
продаж — ноль (ни среди зрелых, ни среди свежих) → кандидат.

Щит длинного цикла: рассрочки («рассрочка», «на 12 месяцев» в имени) не трогаем —
у рассрочки дорогой квал окупается позже, оплаты растягиваются дольше горизонта.
Маркеры — пример эвристики; настройте под названия своих офферов.

Продажи считаются запросом в AMO на момент проверки по статусам оплат
(config.AMO_PAYMENT_STATUS_IDS).
Fail-closed: недоступный AMO или отсутствие зрелых лидов = не кандидат.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

_TZ_LOCAL = timezone(timedelta(hours=5))
_PIPELINE_NEW_SALES = 3480844

NR_MIN_SPEND_USD = 500.0
NR_PAYMENT_MATURITY_DAYS = 14
NR_MIN_MATURE_LEADS = 5
# Маркеры офферов с длинным циклом оплаты — правило их не трогает
# (пример: рассрочка банка на 12 месяцев; подставьте маркеры своих офферов).
NR_SHIELD_MARKERS = ("рассрочка", "на 12 месяцев")


def _shielded(ad_name: str) -> bool:
    lowered = (ad_name or "").lower()
    return any(marker in lowered for marker in NR_SHIELD_MARKERS)


def _live_lead_outcomes(ad_id: str) -> tuple[int, int, int] | None:
    """(зрелых лидов, продаж всего, квалов всего) запросом в AMO. None — недоступен."""
    try:
        from config import AMO_PAYMENT_STATUS_IDS
        from integrations.amo import _amo_get

        response = _amo_get(
            "leads",
            params={"query": str(ad_id), "with": "contacts,tags", "limit": 250},
        ) or {}
        raw_leads = (response.get("_embedded") or {}).get("leads", [])
        if len(raw_leads) >= 250:
            # Страница переполнена — продажи могли остаться за кадром.
            # Недоказанное не режется.
            logger.warning(
                "no_revenue_waster: %s — 250+ лидов на странице, вердикт unknown",
                ad_id,
            )
            return None
        payment_statuses = set(AMO_PAYMENT_STATUS_IDS)
        maturity_cutoff = datetime.now(_TZ_LOCAL) - timedelta(
            days=NR_PAYMENT_MATURITY_DAYS
        )
        mature = sales = quals = 0
        for lead in (response.get("_embedded") or {}).get("leads", []):
            if lead.get("pipeline_id") != _PIPELINE_NEW_SALES:
                continue
            fields = lead.get("custom_fields_values") or []
            if not any(
                f.get("field_id") == 902422
                and str((f.get("values") or [{}])[0].get("value", "")).strip()
                == str(ad_id)
                for f in fields
            ):
                continue
            tags = ((lead.get("_embedded") or {}).get("tags")) or []
            tag_names = " ".join(str(t.get("name") or "").lower() for t in tags)
            if "автосделка" in tag_names or "рассылка waba" in tag_names:
                continue
            created = datetime.fromtimestamp(
                int(lead.get("created_at") or 0), _TZ_LOCAL
            )
            if created <= maturity_cutoff:
                mature += 1
            if lead.get("status_id") in payment_statuses:
                sales += 1
            if any(
                f.get("field_id") == 804012
                and str((f.get("values") or [{}])[0].get("value", "")).upper().strip()
                == "ДА"
                for f in fields
            ):
                quals += 1
        return mature, sales, quals
    except Exception as exc:  # noqa: BLE001 — недоказанное не режется
        logger.warning("no_revenue_waster: AMO недоступен для %s — %s", ad_id, exc)
        return None


def find_no_revenue_wasters(
    local_ads: Sequence[Mapping[str, object]],
    *,
    lead_outcomes: Callable[[str], tuple[int, int, int] | None] | None = None,
    known_age_days: Callable[[Mapping[str, object]], int | None] | None = None,
) -> list[dict]:
    """PAUSE-решения класса «дорогой без выручки». Формат — как у score_and_decide."""
    if lead_outcomes is None:
        lead_outcomes = _live_lead_outcomes
    if known_age_days is None:
        from services.decision_policy import _known_age_days as _default_age

        known_age_days = _default_age

    decisions: list[dict] = []
    for ad in local_ads:
        ad_id = str(ad.get("ad_id") or "")
        name = str(ad.get("ad_name") or "")
        if not ad_id or not name:
            continue
        spend = float(ad.get("spend") or 0)
        if spend < NR_MIN_SPEND_USD:
            continue
        if _shielded(name):
            continue
        age = known_age_days(ad)
        if age is None or age < NR_PAYMENT_MATURITY_DAYS:
            continue
        outcomes = lead_outcomes(ad_id)
        if outcomes is None:
            continue
        mature_leads, sales, quals = outcomes
        if mature_leads < NR_MIN_MATURE_LEADS:
            continue
        # Продажа в ЛЮБОМ лиде (даже свежем) спасает — замок «не ожила деньгами».
        if sales:
            continue
        decisions.append(
            {
                "ad_id": ad_id,
                "ad_name": name,
                "adset_id": ad.get("adset_id"),
                "action": "PAUSE",
                "score": 0,
                "is_no_revenue_waster": True,
                "reasons": [
                    (
                        f"дорогой без выручки: ${spend:.0f} за 30д, "
                        f"{mature_leads} зрелых лидов (14д+), {quals} квалов, 0 продаж"
                    )
                ],
            }
        )
    return decisions
