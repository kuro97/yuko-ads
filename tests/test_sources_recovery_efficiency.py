"""Экономия запросов к AMO при восстановлении первоисточника копий.

Замечания из ревью, закрытые здесь:
  - оригинал догружался из AMO, даже если он уже лежит в загруженном периоде;
  - самоссылочное имя («Сделка #<свой id>») гоняло один и тот же запрос по кругу
    до исчерпания лимита глубины;
  - в журнале не было видно, сработало восстановление или нет.

AMO в этом аккаунте медленный и рвёт соединения, поэтому лишний запрос — не
теория, а реальный риск потерять восстановление целиком.
"""

import logging
from unittest.mock import patch

from web.sources_routes import _enrich_smart_copy
from services.sources import classify_lead_source

SMART_COPY_SID = 24557225


def _copy(lead_id, name, contact_id=None):
    lead = {
        "id": lead_id, "name": name, "source_id": SMART_COPY_SID,
        "custom_fields": [], "tags": [],
    }
    if contact_id is not None:
        lead["contacts"] = [{"id": contact_id}]
    return lead


def _lead(lead_id, tags=None, name="Сделка"):
    return {
        "id": lead_id, "name": name, "source_id": None, "custom_fields": [],
        "tags": [{"name": t} for t in (tags or [])], "created_at": 1000,
    }


def test_parent_from_current_period_is_not_refetched():
    """Оригинал уже загружен в периоде — второй раз в AMO не идём."""
    copy_lead = _copy(99, "Сделка #50")
    parent = _lead(50, tags=["fb_owner"])

    with patch("web.sources_routes.get_leads_batch") as mock_batch:
        _enrich_smart_copy([copy_lead, parent])
        mock_batch.assert_not_called()

    assert classify_lead_source(copy_lead) == "Facebook Ads"


def test_only_missing_parents_are_fetched():
    """Из двух оригиналов догружается лишь тот, которого нет в периоде."""
    in_period = _lead(50, tags=["maps"])
    copies = [_copy(99, "Сделка #50"), _copy(98, "Сделка #77")]

    fetched = []

    def fake_batch(ids):
        fetched.extend(ids)
        return [{"id": 77, "name": "Сделка", "source_id": None, "created_at": 1,
                 "custom_fields_values": [], "_embedded": {"tags": [{"name": "taplink_lead"}]}}]

    with patch("web.sources_routes.get_leads_batch", side_effect=fake_batch):
        _enrich_smart_copy(copies + [in_period])

    assert fetched == [77], f"запрошены лишние оригиналы: {fetched}"
    assert classify_lead_source(copies[0]) == "Каталог-карты"
    assert classify_lead_source(copies[1]) == "Taplink"


def test_self_referencing_name_does_not_loop():
    """«Сделка #<свой id>» — не ссылка на оригинал, запрос не повторяем."""
    lead = _copy(31835358, "Сделка #31835358")
    calls = {"n": 0}

    def counting_batch(ids):
        calls["n"] += 1
        return [{"id": 31835358, "name": "Сделка #31835358", "source_id": SMART_COPY_SID,
                 "created_at": 1, "custom_fields_values": [], "_embedded": {"tags": []}}]

    with patch("web.sources_routes.get_leads_batch", side_effect=counting_batch), \
         patch("web.sources_routes.get_contacts_with_leads_batch", return_value={}):
        _enrich_smart_copy([lead])

    assert calls["n"] <= 1, f"самоссылка гоняет запросы по кругу: {calls['n']} раз"
    assert classify_lead_source(lead) == "Умное копирование"


def test_recovery_result_is_logged(caplog):
    """В журнале видно, сколько копий удалось привязать к первоисточнику.

    Уровень WARNING обязателен: root-логгер веб-приложения на WARNING, и запись
    уровнем INFO до journald не доходит.
    """
    copies = [_copy(99, "Сделка #50"), _copy(98, "Сделка #51")]
    parents = [_lead(50, tags=["fb_owner"]), _lead(51, tags=[])]

    with caplog.at_level(logging.WARNING, logger="web.sources_routes"):
        _enrich_smart_copy(copies + parents)

    text = " ".join(r.getMessage() for r in caplog.records)
    assert "копи" in text.lower(), f"нет строки о восстановлении копий: {text}"
    assert "1" in text, f"в логе нет числа восстановленных: {text}"
