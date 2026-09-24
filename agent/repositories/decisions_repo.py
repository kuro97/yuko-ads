"""Репозиторий решений — делегирует в agent.database (SQLite)."""

from agent.database import (
    save_decision as _save,
    get_decisions as _get,
    get_decisions_history as _get_history,
    get_decisions_for_ad as _get_for_ad,
    get_decision_counts as _get_counts,
    has_repeat_problem as _has_repeat,
)


def save_decision(tenant_id: str, ad_id: str, ad_name: str, action: str,
                  reason: str, confirmed_by: str = "user",
                  spend=None, leads=None, cpl=None,
                  ctr=None, cpm=None, romi=None, qual_pct=None,
                  effect_id: str | None = None,
                  projection_kind: str | None = None,
                  projection_payload: dict | None = None,
                  outbox_channel: str | None = None,
                  outbox_payload: dict | None = None) -> bool:
    """Сохраняет решение в SQLite."""
    return _save(ad_id, ad_name, action, reason, confirmed_by,
                 spend=spend, leads=leads, cpl=cpl,
                 ctr=ctr, cpm=cpm, romi=romi, qual_pct=qual_pct,
                 effect_id=effect_id,
                 projection_kind=projection_kind,
                 projection_payload=projection_payload,
                 outbox_channel=outbox_channel,
                 outbox_payload=outbox_payload)


def get_decisions(tenant_id: str, limit: int = 100) -> list[dict]:
    """Возвращает последние решения из SQLite."""
    return _get(limit=limit)


def get_decisions_history(tenant_id: str, action=None, ad_name=None,
                          date_from=None, date_to=None,
                          limit: int = 50, offset: int = 0) -> dict:
    """Возвращает историю решений с фильтрами."""
    return _get_history(action=action, ad_name=ad_name,
                        date_from=date_from, date_to=date_to,
                        limit=limit, offset=offset)


def get_decisions_for_ad(tenant_id: str, ad_id: str) -> list[dict]:
    """Все решения для ad_id."""
    return _get_for_ad(ad_id)


def get_decision_counts(tenant_id: str) -> dict[str, int]:
    """Возвращает {ad_id: количество_решений}."""
    return _get_counts()


def has_repeat_problem(tenant_id: str, ad_id: str, current_recommendation: str) -> bool:
    """True если объявление уже отключали и снова рекомендация ОТКЛЮЧИТЬ."""
    return _has_repeat(ad_id, current_recommendation)
