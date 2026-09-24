"""Живой квал-страж пауз: не выключать рекламу, у которой квалы уже есть в AMO.

Регрессия: решения о паузе принимаются по KB (qual_pct), а сверка с AMO
отстаёт на часы — реклама с живыми квалами выглядела «лиды есть, квалов ноль»
и уезжала в паузу. В первый день полной автономии так выключались рабочие
рекламы с ценой квала ниже бенчмарка (их пришлось возвращать вручную).

Страж бьёт точечно: только кандидаты прогона, у которых KB видит ноль квалов,
перепроверяются живым запросом в AMO (точечный поиск query=<ad_id> — другого
фильтра AMO не умеет). Нашёлся хоть один
квал — кандидат снимается, данные объявлены незрелыми.

Fail-open по сети: AMO недоступен — страж молчит и НЕ блокирует паузу. Иначе
любой таймаут AMO выключал бы защиту от сливов целиком.
"""

from __future__ import annotations

import logging
from typing import Mapping

logger = logging.getLogger(__name__)

_PIPELINE_NEW_SALES = 3480844
_QUAL_FIELD_ID = 804012
_SERVICE_TAGS = frozenset({"Автосделка", "Рассылка Waba"})


def _is_qualified_v4(lead: Mapping[str, object]) -> bool:
    for field in lead.get("custom_fields_values") or []:
        if field.get("field_id") == _QUAL_FIELD_ID:
            return any(
                str(value.get("value", "")).strip().upper() == "ДА"
                for value in field.get("values", [])
            )
    return False


def _is_service_v4(lead: Mapping[str, object]) -> bool:
    tags = (lead.get("_embedded") or {}).get("tags") or []
    return any(tag.get("name") in _SERVICE_TAGS for tag in tags)


def live_qual_count(ad_id: str) -> int | None:
    """Живые квалы рекламы по AMO. None — AMO недоступен (страж молчит)."""

    try:
        from integrations.amo import _amo_get

        response = _amo_get(
            "leads",
            params={"query": str(ad_id), "with": "contacts,tags", "limit": 250},
        ) or {}
        quals = 0
        seen: set[object] = set()
        for lead in (response.get("_embedded") or {}).get("leads", []):
            if lead.get("pipeline_id") != _PIPELINE_NEW_SALES:
                continue
            if _is_service_v4(lead):
                continue
            contacts = (lead.get("_embedded") or {}).get("contacts") or [{}]
            contact_id = contacts[0].get("id") or lead.get("id")
            if contact_id in seen:
                continue
            seen.add(contact_id)
            if _is_qualified_v4(lead):
                quals += 1
        return quals
    except Exception as exc:  # noqa: BLE001 — сеть не выключает защиту от сливов
        logger.warning("live_qual_guard: AMO недоступен для %s — %s", ad_id, exc)
        return None


def kb_sees_zero_quals(local_ad: Mapping[str, object] | None) -> bool:
    """KB считает, что квалов нет (или не знает) — повод перепроверить живым AMO."""

    if not local_ad:
        return True
    value = local_ad.get("qual_pct")
    try:
        return value is None or float(value) == 0.0
    except (TypeError, ValueError):
        return True


def filter_stale_qual_candidates(
    decisions: list[dict],
    ads_by_id: Mapping[str, Mapping[str, object]],
) -> tuple[list[dict], list[str]]:
    """Убирает из pause-кандидатов рекламы с живыми квалами в AMO.

    Возвращает (оставшиеся кандидаты, снятые ad_id). Перепроверяются только те,
    у кого KB видит ноль квалов И есть лиды (нулевые по лидам рекламы AMO
    подтвердить квалами не может — их не трогаем, там страж бессмыслен).
    """

    kept: list[dict] = []
    dropped: list[str] = []
    for decision in decisions:
        ad_id = str(decision.get("ad_id") or "")
        local_ad = ads_by_id.get(ad_id)
        leads = (local_ad or {}).get("leads")
        has_leads = bool(leads) and str(leads) not in {"0", "0.0"}
        if not ad_id or not has_leads or not kb_sees_zero_quals(local_ad):
            kept.append(decision)
            continue
        quals = live_qual_count(ad_id)
        if quals and quals > 0:
            dropped.append(ad_id)
            logger.info(
                "live_qual_guard: %s снят с паузы — живых квалов в AMO %d, "
                "KB отстаёт (данные незрелые)",
                ad_id,
                quals,
            )
        else:
            kept.append(decision)
    return kept, dropped
