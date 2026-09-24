"""Семейное правило слива: один креатив, слитый по нескольким городам.

Мотив: один и тот же креатив может сливать в нескольких городах
одновременно — семьёй заметно выше порогов при нуле квалов, — но каждый город в
отдельности не дотягивает до порогов R1 (15 зрелых лидов / $150 на ОДНО
объявление), и автономия его пропускает. Правило смотрит на семью целиком:
одинаковое ядро имени в ≥2 городах, суммарные лиды и расход выше порогов,
живых квалов у всей семьи ноль → зрелые члены семьи становятся
PAUSE-кандидатами.

Квалы считаются ЖИВЫМ AMO (live_qual_guard.live_qual_count), не по KB:
qual_leads в KB для свежих строк NULL, а отставание KB от AMO уже приводило
к паузе рабочих реклам.

Fail-closed: недоступный AMO по любому члену семьи (None) снимает всю семью
с кандидатов — недоказанное не режется.
"""

from __future__ import annotations

import logging
from typing import Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

FAMILY_MIN_CITIES = 2
FAMILY_MIN_LEADS = 15
FAMILY_MIN_SPEND_USD = 150.0
FAMILY_MIN_AGE_DAYS = 5
# Префикс «<Город> | » режется только если он короткий и без "/": сегменты
# вида «PRODA / Креатив X» — часть имени креатива, а не города.
_CITY_PREFIX_MAX_LEN = 30


def family_key(ad_name: str) -> str:
    """Ядро имени креатива: имя без городского префикса «<Город> | »."""
    name = (ad_name or "").strip()
    head, sep, tail = name.partition(" | ")
    if sep and tail and "/" not in head and len(head) <= _CITY_PREFIX_MAX_LEN:
        return tail.strip().lower()
    return name.lower()


def find_family_wasters(
    local_ads: Sequence[Mapping[str, object]],
    *,
    live_qual_count: Callable[[str], int | None] | None = None,
    known_age_days: Callable[[Mapping[str, object]], int | None] | None = None,
) -> list[dict]:
    """PAUSE-решения по семьям-сливам. Формат — как у score_and_decide."""
    if live_qual_count is None:
        from services.live_qual_guard import live_qual_count as _default_lqc

        live_qual_count = _default_lqc
    if known_age_days is None:
        from services.decision_policy import _known_age_days as _default_age

        known_age_days = _default_age

    families: dict[str, list[Mapping[str, object]]] = {}
    for ad in local_ads:
        ad_id = str(ad.get("ad_id") or "")
        name = str(ad.get("ad_name") or "")
        if not ad_id or not name:
            continue
        families.setdefault(family_key(name), []).append(ad)

    decisions: list[dict] = []
    for key, members in families.items():
        cities = {str(m.get("city") or "").strip() for m in members}
        cities.discard("")
        if len(cities) < FAMILY_MIN_CITIES:
            continue
        total_spend = sum(float(m.get("spend") or 0) for m in members)
        total_leads = sum(int(m.get("leads") or 0) for m in members)
        if total_spend < FAMILY_MIN_SPEND_USD or total_leads < FAMILY_MIN_LEADS:
            continue

        # Живые квалы всей семьи: единственный источник правды. Считаем только
        # членов с лидами — у остальных квалов быть не может.
        family_quals = 0
        data_ok = True
        for m in members:
            if int(m.get("leads") or 0) <= 0:
                continue
            quals = live_qual_count(str(m.get("ad_id")))
            if quals is None:
                data_ok = False
                break
            family_quals += quals
            if family_quals:
                break
        if not data_ok:
            logger.warning(
                "family_waster: AMO недоступен для семьи «%s» — семья пропущена", key
            )
            continue
        if family_quals:
            continue

        for m in members:
            age = known_age_days(m)
            if age is None or age < FAMILY_MIN_AGE_DAYS:
                continue
            decisions.append(
                {
                    "ad_id": str(m.get("ad_id")),
                    "ad_name": str(m.get("ad_name") or ""),
                    "adset_id": m.get("adset_id"),
                    "action": "PAUSE",
                    "score": 0,
                    "is_family_waster": True,
                    "reasons": [
                        (
                            f"семья-слив «{key}»: {len(cities)} городов, "
                            f"${total_spend:.0f}, {total_leads} лидов, 0 живых квалов"
                        )
                    ],
                }
            )
    return decisions
