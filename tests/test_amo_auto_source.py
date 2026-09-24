"""
Unit-тесты для services/amo_auto_source.py.
Мокаем только внешние границы: get_lead, get_contact_with_leads,
get_leads_batch, _amo_patch.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.amo_auto_source import (
    _filter_noise_tags,
    _extract_tags,
    _has_source,
    is_operator_created,
    apply_source_to_lead,
    find_root_source,
    process_lead_created,
    AUTO_SOURCE_TAG,
)


# ─── Фабрика тестовых лидов ──────────────────────────────────────────────────

def _make_lead(
    lead_id=1,
    contact_id=10,
    created_by=999,
    tags=None,
    utm=None,
    source_id=None,
    created_at=1700000000,
):
    """Создаёт тестовый лид-словарь в формате AMO."""
    custom_fields = []
    if utm:
        for k, v in utm.items():
            custom_fields.append({
                "field_name": k,
                "field_code": k.upper(),
                "values": [{"value": v}],
            })
    return {
        "id": lead_id,
        "created_by": created_by,
        "created_at": created_at,
        "source_id": source_id,
        "custom_fields_values": custom_fields,
        "_embedded": {
            "contacts": [{"id": contact_id}],
            "tags": [{"name": t} for t in (tags or [])],
        },
    }


# ─── Тесты _filter_noise_tags ─────────────────────────────────────────────────

def test_filter_noise_tags_removes_only_noise():
    """Шумовые теги удаляются, нешумовые — остаются."""
    tags = ["залётный", "online", "custom_tag", "рекомендация"]
    result = _filter_noise_tags(tags)
    assert "залётный" not in result
    assert "рекомендация" not in result
    assert "online" in result
    assert "custom_tag" in result


def test_filter_noise_tags_case_insensitive():
    """Фильтр нечувствителен к регистру."""
    tags = ["ЗАЛЁТНЫЙ", "Залетный", "БАЗА_ОПЕРАТОРА"]
    result = _filter_noise_tags(tags)
    assert result == []


# ─── Тесты _has_source ────────────────────────────────────────────────────────

def test_has_source_true_by_utm():
    """Лид с utm_source считается имеющим источник."""
    lead = _make_lead(utm={"utm_source": "facebook"})
    assert _has_source(lead) is True


def test_has_source_true_by_tags_after_filter():
    """Лид с нешумовым тегом считается имеющим источник."""
    # залётный — шум, fb_owner — нет → источник есть
    lead = _make_lead(tags=["залётный", "fb_owner"])
    assert _has_source(lead) is True


def test_has_source_false_only_noise_tags():
    """Только шумовые теги, без UTM, без source_id → источника нет."""
    lead = _make_lead(tags=["залётный", "рекомендация"])
    # Патчим AMO_SOURCE_ID_FIELD=None чтобы не зависеть от env
    with patch("services.amo_auto_source.AMO_SOURCE_ID_FIELD", None):
        assert _has_source(lead) is False


# ─── Тесты is_operator_created ───────────────────────────────────────────────

def test_is_operator_created_true():
    """Лид создан оператором из whitelist → True."""
    lead = _make_lead(created_by=123)
    assert is_operator_created(lead, {123, 456}) is True


def test_is_operator_created_false_empty_set():
    """Пустой whitelist → False (защита от недонастроенного env)."""
    lead = _make_lead(created_by=123)
    assert is_operator_created(lead, set()) is False


# ─── Тесты _extract_tags ─────────────────────────────────────────────────────

def test_extract_tags_from_embedded():
    """Теги корректно извлекаются из _embedded.tags."""
    lead = _make_lead(tags=["fb_owner", "taplink"])
    tags = _extract_tags(lead)
    assert tags == ["fb_owner", "taplink"]


# ─── Тест apply_source_to_lead — не перезаписывает существующий UTM ──────────

def test_apply_source_no_overwrite_existing_utm():
    """Если у лида уже есть utm_source — не перезаписывается."""
    current_lead = _make_lead(lead_id=1, utm={"utm_source": "google"})
    source_data = {
        "utm": {"utm_source": "facebook"},
        "tags": [],
        "source_id": None,
    }
    with patch("services.amo_auto_source._amo_patch") as mock_patch, \
         patch("services.amo_auto_source.AMO_SOURCE_ID_FIELD", None):
        apply_source_to_lead(1, source_data, current_lead=current_lead)
        # utm_source не должен попасть в custom_fields_values
        if mock_patch.called:
            call_body = mock_patch.call_args[0][1]
            fields = call_body.get("custom_fields_values", [])
            utm_source_fields = [
                f for f in fields
                if f.get("field_code", "").upper() == "UTM_SOURCE"
            ]
            assert utm_source_fields == [], "utm_source не должен перезаписываться"


# ─── Тест apply_source_to_lead — теги объединяются без дублей ────────────────

def test_apply_source_merges_tags():
    """Теги объединяются без дублей при PATCH."""
    current_lead = _make_lead(lead_id=1, tags=["existing_tag"])
    source_data = {
        "utm": {},
        "tags": ["new_tag", "existing_tag"],  # existing_tag уже есть
        "source_id": None,
    }
    with patch("services.amo_auto_source._amo_patch") as mock_patch, \
         patch("services.amo_auto_source.AMO_SOURCE_ID_FIELD", None):
        result = apply_source_to_lead(1, source_data, current_lead=current_lead)
        assert result is True
        call_body = mock_patch.call_args[0][1]
        tag_names = [t["name"] for t in call_body["_embedded"]["tags"]]
        # existing_tag присутствует ровно один раз
        assert tag_names.count("existing_tag") == 1
        # new_tag добавлен
        assert "new_tag" in tag_names
        # маркер защиты от петли добавлен
        assert AUTO_SOURCE_TAG in tag_names


# ─── Тест process_lead_created — пропуск если не оператор ───────────────────

def test_process_lead_skip_if_not_operator():
    """Лид создан не оператором → status='skipped', reason содержит 'not operator'."""
    lead = _make_lead(lead_id=42, created_by=9999)
    with patch("services.amo_auto_source.get_lead", return_value=lead), \
         patch("services.amo_auto_source.AMO_OPERATOR_USER_IDS", {123, 456}):
        result = process_lead_created(42)
    assert result["status"] == "skipped"
    assert "not operator" in result["reason"]


# ─── Тест process_lead_created — пропуск если уже применён ──────────────────

def test_process_lead_skip_if_already_applied():
    """Тег auto_source_applied уже стоит → status='skipped'."""
    lead = _make_lead(lead_id=7, tags=[AUTO_SOURCE_TAG])
    with patch("services.amo_auto_source.get_lead", return_value=lead):
        result = process_lead_created(7)
    assert result["status"] == "skipped"
    assert result["reason"] == "already applied"


# ─── Тест find_root_source — самый старый с источником ───────────────────────

def test_find_root_source_returns_oldest_with_source():
    """
    3 лида: самый старый без источника, средний с UTM, новый с тегом.
    find_root_source должен вернуть СРЕДНИЙ (первый по дате с источником).
    """
    # Лид без источника — самый старый
    lead_old = _make_lead(lead_id=10, created_at=1000000)
    # Лид со средней датой + UTM
    lead_mid = _make_lead(lead_id=20, created_at=1500000, utm={"utm_source": "facebook"})
    # Лид с новым тегом
    lead_new = _make_lead(lead_id=30, created_at=2000000, tags=["fb_owner"])

    contact = {
        "_embedded": {
            "leads": [
                {"id": 10},
                {"id": 20},
                {"id": 30},
            ]
        }
    }

    with patch("services.amo_auto_source.get_contact_with_leads", return_value=contact), \
         patch("services.amo_auto_source.get_leads_batch", return_value=[lead_old, lead_mid, lead_new]), \
         patch("services.amo_auto_source.AMO_SOURCE_ID_FIELD", None):
        result = find_root_source(contact_id=5, exclude_lead_id=99)

    assert result is not None
    assert result["root_lead_id"] == 20, (
        f"Ожидался лид 20 (первый с источником по возрасту), получен {result['root_lead_id']}"
    )
    assert result["utm"].get("utm_source") == "facebook"


# ─── Тест is_operator_created — не из whitelist ───────────────────────────────

def test_is_operator_created_false_not_in_whitelist():
    """created_by=999, whitelist={1234567, 2345678} → False."""
    lead = _make_lead(created_by=999)
    assert is_operator_created(lead, {1234567, 2345678}) is False


# ─── Тест _has_source — True через source_id ─────────────────────────────────

def test_has_source_true_by_source_id():
    """Лид с кастомным полем source_id (field_id совпадает) → _has_source() True."""
    field_id = 77777
    lead = {
        "id": 1,
        "created_by": 1,
        "created_at": 1700000000,
        "source_id": None,
        "custom_fields_values": [
            {
                "field_id": field_id,
                "field_name": "Источник",
                "field_code": "SOURCE",
                "values": [{"value": 42}],
            }
        ],
        "_embedded": {"contacts": [], "tags": []},
    }
    with patch("services.amo_auto_source.AMO_SOURCE_ID_FIELD", field_id):
        assert _has_source(lead) is True


# ─── Тест find_root_source — пропускает exclude_lead_id ──────────────────────

def test_find_root_source_skips_exclude_lead_id():
    """У контакта 2 лида с UTM — один exclude_lead_id, второй возвращается."""
    lead_exclude = _make_lead(lead_id=100, created_at=1000000, utm={"utm_source": "google"})
    lead_keep = _make_lead(lead_id=200, created_at=2000000, utm={"utm_source": "facebook"})

    contact = {
        "_embedded": {
            "leads": [{"id": 100}, {"id": 200}]
        }
    }

    # get_leads_batch получает только [200] — сервис исключает 100 до вызова батча
    with patch("services.amo_auto_source.get_contact_with_leads", return_value=contact), \
         patch("services.amo_auto_source.get_leads_batch", return_value=[lead_keep]), \
         patch("services.amo_auto_source.AMO_SOURCE_ID_FIELD", None):
        result = find_root_source(contact_id=5, exclude_lead_id=100)

    # lead_exclude исключён — должен вернуться lead_keep
    assert result is not None
    assert result["root_lead_id"] == 200
    assert result["utm"].get("utm_source") == "facebook"


# ─── Тест find_root_source — None если у всех нет источника ──────────────────

def test_find_root_source_returns_none_no_sources():
    """У контакта 3 лида без UTM/тегов/source_id → None."""
    lead1 = _make_lead(lead_id=10, created_at=1000000)
    lead2 = _make_lead(lead_id=20, created_at=2000000)
    lead3 = _make_lead(lead_id=30, created_at=3000000)

    contact = {
        "_embedded": {"leads": [{"id": 10}, {"id": 20}, {"id": 30}]}
    }

    with patch("services.amo_auto_source.get_contact_with_leads", return_value=contact), \
         patch("services.amo_auto_source.get_leads_batch", return_value=[lead1, lead2, lead3]), \
         patch("services.amo_auto_source.AMO_SOURCE_ID_FIELD", None):
        result = find_root_source(contact_id=5, exclude_lead_id=99)

    assert result is None


# ─── Тест find_root_source — None если контакт не найден ─────────────────────

def test_find_root_source_returns_none_no_contact():
    """get_contact_with_leads() вернул None → find_root_source возвращает None."""
    with patch("services.amo_auto_source.get_contact_with_leads", return_value=None):
        result = find_root_source(contact_id=999, exclude_lead_id=1)

    assert result is None


# ─── Тест apply_source_to_lead — пишет только пустые поля ────────────────────

def test_apply_source_writes_only_empty_fields():
    """utm_source уже заполнен, utm_medium пуст — только medium должен добавиться в PATCH."""
    current_lead = _make_lead(
        lead_id=1,
        utm={"utm_source": "facebook"},  # utm_source уже есть
    )
    source_data = {
        "utm": {"utm_source": "google", "utm_medium": "cpc"},
        "tags": [],
        "source_id": None,
    }

    with patch("services.amo_auto_source._amo_patch") as mock_patch, \
         patch("services.amo_auto_source.AMO_SOURCE_ID_FIELD", None):
        result = apply_source_to_lead(1, source_data, current_lead=current_lead)

    # PATCH был вызван (medium пустой — есть что копировать)
    assert result is True
    assert mock_patch.called

    call_body = mock_patch.call_args[0][1]
    fields = call_body.get("custom_fields_values", [])

    # utm_source не должен перезаписываться
    utm_source_fields = [
        f for f in fields if f.get("field_code", "").upper() == "UTM_SOURCE"
    ]
    assert utm_source_fields == [], "utm_source не должен перезаписываться"

    # utm_medium должен быть в PATCH
    utm_medium_fields = [
        f for f in fields if f.get("field_code", "").upper() == "UTM_MEDIUM"
    ]
    assert len(utm_medium_fields) == 1
    assert utm_medium_fields[0]["values"][0]["value"] == "cpc"


# ─── Тест apply_source_to_lead — False когда нечего копировать ───────────────

def test_apply_source_returns_false_when_nothing_to_copy():
    """Все поля уже заполнены, теги совпадают + AUTO_SOURCE_TAG есть → False, _amo_patch не вызван."""
    current_lead = _make_lead(
        lead_id=1,
        utm={"utm_source": "facebook", "utm_medium": "cpc"},
        tags=["existing_tag", AUTO_SOURCE_TAG],
    )
    source_data = {
        "utm": {"utm_source": "google", "utm_medium": "organic"},
        "tags": ["existing_tag"],
        "source_id": None,
    }

    with patch("services.amo_auto_source._amo_patch") as mock_patch, \
         patch("services.amo_auto_source.AMO_SOURCE_ID_FIELD", None):
        result = apply_source_to_lead(1, source_data, current_lead=current_lead)

    assert result is False
    mock_patch.assert_not_called()


# ─── Тест process_lead_created — happy path ───────────────────────────────────

def test_process_lead_created_happy_path():
    """Полный сценарий: оператор создал лид без UTM → копируем из старого лида."""
    # Новый лид — от оператора, без UTM, без AUTO_SOURCE_TAG
    new_lead = _make_lead(lead_id=1, contact_id=10, created_by=999)

    # Старый лид контакта — с utm_source
    old_lead = _make_lead(lead_id=50, contact_id=10, created_at=1000000, utm={"utm_source": "facebook"})

    contact = {
        "_embedded": {"leads": [{"id": 1}, {"id": 50}]}
    }

    with patch("services.amo_auto_source.get_lead", return_value=new_lead), \
         patch("services.amo_auto_source.get_contact_with_leads", return_value=contact), \
         patch("services.amo_auto_source.get_leads_batch", return_value=[old_lead]), \
         patch("services.amo_auto_source._amo_patch") as mock_patch, \
         patch("services.amo_auto_source.AMO_OPERATOR_USER_IDS", {999}), \
         patch("services.amo_auto_source.AMO_SOURCE_ID_FIELD", None):
        result = process_lead_created(1)

    # Статус applied
    assert result["status"] == "applied"
    assert result["lead_id"] == 1
    assert result["source_data"] is not None
    assert result["source_data"]["utm"].get("utm_source") == "facebook"

    # _amo_patch вызван ровно один раз
    mock_patch.assert_called_once()
    call_body = mock_patch.call_args[0][1]

    # PATCH содержит utm_source
    fields = call_body.get("custom_fields_values", [])
    utm_source_fields = [
        f for f in fields if f.get("field_code", "").upper() == "UTM_SOURCE"
    ]
    assert len(utm_source_fields) == 1
    assert utm_source_fields[0]["values"][0]["value"] == "facebook"

    # PATCH содержит тег auto_source_applied
    tag_names = [t["name"] for t in call_body.get("_embedded", {}).get("tags", [])]
    assert AUTO_SOURCE_TAG in tag_names
