"""Тесты бэкфилла источника в AMO (scripts/backfill_lead_source.py).

Скрипт проставляет в карточку сделки метки её первоисточника — те, что AMO
теряет при копировании. Это ЗАПИСЬ в боевую CRM, поэтому правила жёсткие:
  - лид с собственными метками не трогаем никогда;
  - повторно себя не переписываем (тег-маркер);
  - без флага --apply не отправляется ни один PATCH;
  - если первоисточник не нашёлся — молча пропускаем, ничего не выдумываем.
"""

from unittest.mock import patch

import pytest

from scripts.backfill_lead_source import (
    collect_candidates,
    find_origin,
    run_backfill,
)

SMART_COPY_SID = 24557225
AUTO_TAG = "auto_source_applied"


def _lead(lead_id, name="Сделка", source_id=None, tags=None, utm=None, contact_id=None):
    fields = [
        {"field_id": 1, "field_name": k, "values": [{"value": v}]}
        for k, v in (utm or {}).items()
    ]
    lead = {
        "id": lead_id, "name": name, "source_id": source_id,
        "custom_fields": fields, "custom_fields_values": fields,
        "tags": [{"name": t} for t in (tags or [])],
        "_embedded": {"tags": [{"name": t} for t in (tags or [])]},
    }
    if contact_id:
        lead["contacts"] = [{"id": contact_id}]
        lead["_embedded"]["contacts"] = [{"id": contact_id}]
    return lead


# --- Отбор кандидатов ---

def test_lead_with_own_utm_is_not_candidate():
    """У лида есть своя разметка — чужую не навязываем."""
    leads = [_lead(1, utm={"utm_source": "facebook"})]
    assert collect_candidates(leads) == []


def test_copy_without_marks_is_candidate():
    leads = [_lead(1, name="Сделка #50", source_id=SMART_COPY_SID)]
    assert [l["id"] for l in collect_candidates(leads)] == [1]


def test_already_processed_lead_is_skipped():
    """Тег-маркер означает, что мы здесь уже были."""
    leads = [_lead(1, name="Сделка #50", source_id=SMART_COPY_SID, tags=[AUTO_TAG])]
    assert collect_candidates(leads) == []


def test_lead_with_real_channel_is_not_candidate():
    """Канал определяется по тегу — восстанавливать нечего."""
    leads = [_lead(1, tags=["fb_owner"])]
    assert collect_candidates(leads) == []


# --- Поиск первоисточника ---

def test_origin_found_by_name():
    """Имя копии ссылается на оригинал — берём его метки."""
    copy_lead = _lead(1, name="Сделка #50", source_id=SMART_COPY_SID)
    origin = _lead(50, utm={"utm_source": "facebook", "utm_medium": "cpc"}, tags=["fb_owner"])

    with patch("scripts.backfill_lead_source.get_lead", return_value=origin):
        found = find_origin(copy_lead)

    assert found["utm"]["utm_source"] == "facebook"
    assert "fb_owner" in found["tags"]


def test_origin_found_by_contact_history():
    """Ссылки в имени нет — ищем по прошлым сделкам контакта."""
    copy_lead = _lead(1, name="повторный заказ", source_id=SMART_COPY_SID, contact_id=100)

    with patch("scripts.backfill_lead_source.get_lead", return_value=None), \
         patch("scripts.backfill_lead_source.find_root_source",
               return_value={"utm": {"utm_source": "maps_citya"}, "tags": [], "root_lead_id": 77}):
        found = find_origin(copy_lead)

    assert found["utm"]["utm_source"] == "maps_citya"


def test_origin_with_only_work_tags_is_rejected():
    """«тег_заметка_1» и «тег_заметка_2» — пометки менеджеров, а не источник.

    Без этой проверки в карточку уезжает мусор годичной давности: старый лид
    контакта почти всегда чем-нибудь помечен.
    """
    copy_lead = _lead(1, name="Сделка #50", source_id=SMART_COPY_SID)
    origin = _lead(50, tags=["тег_заметка_1", "тег_заметка_2"])

    with patch("scripts.backfill_lead_source.get_lead", return_value=origin), \
         patch("scripts.backfill_lead_source.find_root_source", return_value=None):
        assert find_origin(copy_lead) is None


def test_bot_origin_is_rejected():
    """Рассылка по базе — не канал привлечения, восстанавливать нечего."""
    copy_lead = _lead(1, name="Сделка #50", source_id=SMART_COPY_SID)
    origin = _lead(50, tags=["бот:outbound"])

    with patch("scripts.backfill_lead_source.get_lead", return_value=origin), \
         patch("scripts.backfill_lead_source.find_root_source", return_value=None):
        assert find_origin(copy_lead) is None


def test_only_source_tags_are_copied():
    """С рекламного первоисточника переносим метки канала, а не рабочие пометки."""
    copy_lead = _lead(1, name="Сделка #50", source_id=SMART_COPY_SID)
    origin = _lead(50, tags=["fb_owner", "тег_заметка_2", "тег_заметка_3"])

    with patch("scripts.backfill_lead_source.get_lead", return_value=origin):
        found = find_origin(copy_lead)

    assert found["tags"] == ["fb_owner"], f"скопированы лишние метки: {found['tags']}"


def test_origin_without_marks_returns_none():
    """Оригинал сам пустой — записывать нечего."""
    copy_lead = _lead(1, name="Сделка #50", source_id=SMART_COPY_SID)
    with patch("scripts.backfill_lead_source.get_lead", return_value=_lead(50)), \
         patch("scripts.backfill_lead_source.find_root_source", return_value=None):
        assert find_origin(copy_lead) is None


# --- Прогон ---

def test_dry_run_does_not_write():
    """Без --apply не уходит ни один PATCH — это главный предохранитель."""
    leads = [_lead(1, name="Сделка #50", source_id=SMART_COPY_SID)]
    origin = _lead(50, utm={"utm_source": "facebook"}, tags=["fb_owner"])

    with patch("scripts.backfill_lead_source.get_lead", return_value=origin), \
         patch("scripts.backfill_lead_source.apply_source_to_lead") as mock_apply:
        stats = run_backfill(leads, apply=False)
        mock_apply.assert_not_called()

    assert stats["would_apply"] == 1
    assert stats["applied"] == 0


def test_apply_writes_and_counts():
    leads = [_lead(1, name="Сделка #50", source_id=SMART_COPY_SID)]
    origin = _lead(50, utm={"utm_source": "facebook"}, tags=["fb_owner"])

    with patch("scripts.backfill_lead_source.get_lead", return_value=origin), \
         patch("scripts.backfill_lead_source.apply_source_to_lead", return_value=True) as mock_apply:
        stats = run_backfill(leads, apply=True)
        mock_apply.assert_called_once()

    assert stats["applied"] == 1


def test_limit_caps_writes():
    """Лимит за прогон — страховка от массовой правки при ошибке в логике."""
    leads = [_lead(i, name=f"Сделка #{100 + i}", source_id=SMART_COPY_SID) for i in range(5)]
    origin = _lead(999, utm={"utm_source": "facebook"})

    with patch("scripts.backfill_lead_source.get_lead", return_value=origin), \
         patch("scripts.backfill_lead_source.apply_source_to_lead", return_value=True) as mock_apply:
        stats = run_backfill(leads, apply=True, limit=2)

    assert mock_apply.call_count == 2
    assert stats["applied"] == 2


def test_amo_error_does_not_stop_run():
    """Сбой на одном лиде не должен прерывать весь бэкфилл."""
    leads = [_lead(1, name="Сделка #50", source_id=SMART_COPY_SID),
             _lead(2, name="Сделка #51", source_id=SMART_COPY_SID)]
    origin = _lead(50, utm={"utm_source": "facebook"})

    with patch("scripts.backfill_lead_source.get_lead", return_value=origin), \
         patch("scripts.backfill_lead_source.apply_source_to_lead",
               side_effect=[Exception("AMO 500"), True]):
        stats = run_backfill(leads, apply=True)

    assert stats["applied"] == 1
    assert stats["errors"] == 1
