"""Семантическая проверка креатива после CREATE: FB нормализует URL (завершающий слэш).

Регресс: FB вернул ``https://example.com/``
вместо отправленного ``https://example.com`` в ``link_data.link`` и
``call_to_action.value.link``; строгое сравнение сочло это дрейфом и прервало
запуск после первого объявления (1 из N).
"""
from __future__ import annotations

import copy

from integrations import facebook as fb

EXPECTED = {
    "object_story_spec": {
        "link_data": {
            "call_to_action": {
                "type": "GET_QUOTE",
                "value": {"lead_gen_form_id": "300000000000001", "link": "https://example.com"},
            },
            "image_hash": "0123456789abcdef0123456789abcdef",
            "link": "https://example.com",
            "message": "Текст объявления / с косой чертой в конце/",
            "name": "ACME",
        },
        "page_id": "100000000000001",
    }
}

LIVE = {
    "id": "200000000000001",
    "object_story_spec": {
        "page_id": "100000000000001",
        "link_data": {
            "link": "https://example.com/",
            "message": "Текст объявления / с косой чертой в конце/",
            "name": "ACME",
            "image_hash": "0123456789abcdef0123456789abcdef",
            "call_to_action": {
                "type": "GET_QUOTE",
                "value": {"lead_gen_form_id": "300000000000001", "link": "https://example.com/"},
            },
        },
    },
}


def _compare(live: dict, expected: dict) -> bool:
    projection = fb._semantic_projection(live, expected)
    return fb.canonical_json(fb._normalize_url_leaves(projection)) == fb.canonical_json(
        fb._normalize_url_leaves(expected)
    )


def test_trailing_slash_from_graph_is_not_drift():
    """Живой ответ FB с «/» на конце ссылок равен отправленному payload."""
    assert _compare(LIVE, EXPECTED)


def test_without_normalization_the_same_payload_was_drift():
    """Регресс-якорь: до фикса именно это сравнение рвало запуск."""
    projection = fb._semantic_projection(LIVE, EXPECTED)
    assert fb.canonical_json(projection) != fb.canonical_json(EXPECTED)


def test_other_domain_is_still_drift():
    live = copy.deepcopy(LIVE)
    live["object_story_spec"]["link_data"]["link"] = "https://example.com/prodb/"
    assert not _compare(live, EXPECTED)


def test_cta_link_drift_is_still_drift():
    live = copy.deepcopy(LIVE)
    live["object_story_spec"]["link_data"]["call_to_action"]["value"]["link"] = "https://promo.example.com/"
    assert not _compare(live, EXPECTED)


def test_normalization_touches_only_url_leaves():
    """message с «/» на конце, image_hash, page_id и не-URL link не трогаем."""
    payload = {
        "link": "not-a-url/",
        "message": "текст/",
        "image_hash": "abc/",
        "nested": [{"link": "https://a.example/"}, {"picture": "https://cdn.example/x.jpg/"}],
    }
    normalized = fb._normalize_url_leaves(payload)
    assert normalized["link"] == "not-a-url/"
    assert normalized["message"] == "текст/"
    assert normalized["image_hash"] == "abc/"
    assert normalized["nested"] == [{"link": "https://a.example"}, {"picture": "https://cdn.example/x.jpg"}]
    assert payload["nested"][0]["link"] == "https://a.example/", "исходный объект не мутируется"


def test_normalization_is_idempotent_and_symmetric():
    once = fb._normalize_url_leaves(EXPECTED)
    twice = fb._normalize_url_leaves(once)
    assert once == twice
    assert fb._normalize_url_leaves(LIVE["object_story_spec"]) == fb._normalize_url_leaves(
        fb._semantic_projection(LIVE, EXPECTED)["object_story_spec"]
    )
