"""Тесты services/launch_routing.py — маршрутизация (город, тип) → кабинет FB.

Контракт: L2 расщеплённых городов льётся в «ACME cabinet_b», их L1 и MQL —
в cabinet_a (config.FB_ACCOUNT_ID), CityF целиком в cabinet_b. Немаршрутизированная
пара = LaunchRoutingError (fail-closed), переопределения — через settings.json:
launch_routing.cities (город целиком) и launch_routing.routes (точечно по типу).

В карте есть тип PRODB: PRODB-адсеты всех городов карты (расщеплённые +
CityF) живут в cabinet_b; кабинет из слоя cities PRODB не переносит (только
routes), а null в cities исключает город целиком, включая PRODB.
"""

import sys
from pathlib import Path

import logging

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.scheduler as scheduler
import config
from services.launch_routing import (
    ACCOUNT_CABINET_B,
    LaunchRoutingError,
    accounts_to_scan,
    all_cities,
    cities_for_account,
    get_route_table,
    is_managed_city,
    observe_accounts,
    resolve_account,
    route_account,
    route_type,
    routing_fingerprint,
    types_for_city,
)

_CABINET_A = str(config.FB_ACCOUNT_ID).removeprefix("act_")
_SPLIT_CITIES = ("CityA", "CityB", "CityC", "CityD", "CityE")


@pytest.fixture(autouse=True)
def _no_settings(monkeypatch):
    """Дефолт: settings.json пуст — карта чисто дефолтная."""
    monkeypatch.setattr(scheduler, "load_settings", lambda: {})


def _with_settings(monkeypatch, block: dict) -> None:
    monkeypatch.setattr(
        scheduler, "load_settings", lambda: {"launch_routing": block}
    )


# ---------------------------------------------------------------------------
# Дефолтная карта: расщепление L2/L1 после миграции L2 в cabinet_b
# ---------------------------------------------------------------------------


class TestDefaultTable:
    def test_l2_of_all_cities_routes_to_cabinet_b(self):
        """Главное следствие миграции: новые L2-креативы идут в cabinet_b."""
        for city in _SPLIT_CITIES:
            assert resolve_account(city, "L2") == ACCOUNT_CABINET_B

    def test_l1_and_mql_of_all_cities_stay_in_cabinet_a(self):
        for city in _SPLIT_CITIES:
            assert resolve_account(city, "L1") == _CABINET_A
            assert resolve_account(city, "MQL") == _CABINET_A

    def test_city_lives_in_two_accounts_at_once(self):
        """Один город расщеплён между кабинетами — это и есть новая гранулярность."""
        # У города есть и PRODB (PRODB-адсет в cabinet_b).
        assert types_for_city("CityA") == {
            "L2": ACCOUNT_CABINET_B,
            "L1": _CABINET_A,
            "MQL": _CABINET_A,
            "PRODB": ACCOUNT_CABINET_B,
        }

    def test_cityf_routes_to_cabinet_b_entirely(self):
        assert resolve_account("CityF", "L2") == ACCOUNT_CABINET_B
        assert resolve_account("CityF", "L1") == ACCOUNT_CABINET_B
        assert ACCOUNT_CABINET_B == "29716040622546856"

    def test_all_cities_are_table_cities_with_cityf(self):
        cities = all_cities()
        assert cities == [
            "CityA", "CityB", "CityC", "CityD", "CityE", "CityF",
        ]

    def test_accounts_to_scan_unique_with_default_first(self):
        """Дефолтный кабинет первым — порядок скана не зависит от порядка пар."""
        assert accounts_to_scan() == (_CABINET_A, ACCOUNT_CABINET_B)

    def test_cities_for_account_reports_presence_not_ownership(self):
        # L2 каждого города теперь в cabinet_b — город «есть» в обоих кабинетах.
        assert set(cities_for_account(ACCOUNT_CABINET_B)) == set(_SPLIT_CITIES) | {
            "CityF"
        }
        assert set(cities_for_account(f"act_{ACCOUNT_CABINET_B}")) == set(
            _SPLIT_CITIES
        ) | {"CityF"}
        assert set(cities_for_account(_CABINET_A)) == set(_SPLIT_CITIES)

    def test_is_managed_city(self):
        assert is_managed_city("CityA") is True
        assert is_managed_city("CityF") is True
        assert is_managed_city("Онлайн") is False
        assert is_managed_city("CityG") is False

    def test_route_account_returns_none_instead_of_raising(self):
        assert route_account("CityA", "L2") == ACCOUNT_CABINET_B
        assert route_account("CityF", "MQL") is None
        assert route_account("CityG", "L2") is None


# ---------------------------------------------------------------------------
# Fail-closed: немаршрутизированная пара = отказ
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_unknown_city_raises(self):
        with pytest.raises(LaunchRoutingError, match="не маршрутизирован"):
            resolve_account("CityG", "L2")

    def test_unrouted_type_of_known_city_raises_with_distinct_message(self):
        """У CityF нет website-кампаний — MQL не должен уходить в cabinet_a."""
        with pytest.raises(LaunchRoutingError, match="Тип адсета"):
            resolve_account("CityF", "MQL")

    def test_empty_city_raises(self):
        with pytest.raises(LaunchRoutingError):
            resolve_account("", "L2")

    def test_none_city_raises(self):
        with pytest.raises(LaunchRoutingError):
            resolve_account(None, "L2")

    def test_empty_type_raises(self):
        with pytest.raises(LaunchRoutingError):
            resolve_account("CityA", "")

    def test_unknown_type_raises(self):
        with pytest.raises(LaunchRoutingError):
            resolve_account("CityA", "XX")

    def test_resolve_strips_whitespace_and_normalizes_case(self):
        assert resolve_account("  CityF  ", "l2") == ACCOUNT_CABINET_B


# ---------------------------------------------------------------------------
# route_type: язык карточки не равен типу адсета
# ---------------------------------------------------------------------------


class TestRouteType:
    def test_leadgen_uses_card_language(self):
        assert route_type("leadgen", "L2") == "L2"
        assert route_type("leadgen", "L1") == "L1"
        # PRODB-карточка идёт в PRODB-инвентарь (тип PRODB) независимо от языка:
        # раньше PRODB-карточки резолвились в PRODA-адсеты
        # (integrations/facebook.py, ветка leadgen_prodb) — это и был баг.
        assert route_type("leadgen_prodb", "L2") == "PRODB"
        assert route_type("leadgen_prodb", "L1") == "PRODB"

    def test_prodb_still_requires_valid_language(self):
        """Язык для PRODB выбирает лид-форму — мусорный язык не проходит."""
        with pytest.raises(ValueError):
            route_type("leadgen_prodb", "MQL")
        with pytest.raises(ValueError):
            route_type("leadgen_prodb", "")

    def test_website_always_uses_mql_even_for_l2_card(self):
        """Website-карточка на L2 льётся в MQL-инвентарь, а не в L2."""
        assert route_type("website", "L2") == "MQL"
        assert route_type("website", "L1") == "MQL"

    def test_online_campaigns_do_not_use_offline_map(self):
        with pytest.raises(ValueError, match="онлайн-контур"):
            route_type("mql_online", "L2")
        with pytest.raises(ValueError, match="онлайн-контур"):
            route_type("prodb_online", "L1")

    def test_unknown_language_raises(self):
        with pytest.raises(ValueError):
            route_type("leadgen", "MQL")


# ---------------------------------------------------------------------------
# Переопределения: слой cities (город целиком)
# ---------------------------------------------------------------------------


class TestCityOverrides:
    """Прежний формат настроек: значение города применяется ко всем его типам."""

    def test_override_returns_whole_city_to_cabinet_a(self, monkeypatch):
        _with_settings(monkeypatch, {"cities": {"CityF": _CABINET_A}})
        assert resolve_account("CityF", "L2") == _CABINET_A
        assert resolve_account("CityF", "L1") == _CABINET_A
        # MQL появился вместе с городом целиком
        assert resolve_account("CityF", "MQL") == _CABINET_A

    def test_override_adds_new_city(self, monkeypatch):
        _with_settings(monkeypatch, {"cities": {"CityG": "555000111"}})
        assert resolve_account("CityG", "L2") == "555000111"
        assert resolve_account("CityG", "L1") == "555000111"
        assert "CityG" in all_cities()

    def test_override_normalizes_act_prefix_and_int(self, monkeypatch):
        _with_settings(
            monkeypatch,
            {"cities": {"CityG": "act_555000111", "CityH": 777000222}},
        )
        assert resolve_account("CityG", "L2") == "555000111"
        assert resolve_account("CityH", "L1") == "777000222"

    def test_null_excludes_whole_city(self, monkeypatch):
        """null в cities исключает город из запуска целиком — включая PRODB.

        «Все города» любой оффлайн-кампании строятся из all_cities(); город,
        оставшийся в карте одной парой PRODB, ронял бы PRODA-запуск отказом по
        немаршрутизированной паре вместо пропуска города.
        """
        _with_settings(monkeypatch, {"cities": {"CityF": None}})
        assert "CityF" not in all_cities()
        assert is_managed_city("CityF") is False
        for adset_type in ("L2", "L1", "MQL", "PRODB"):
            with pytest.raises(LaunchRoutingError):
                resolve_account("CityF", adset_type)

    def test_rollback_of_whole_migration(self, monkeypatch):
        """Аварийный откат без деплоя: все расщеплённые города целиком в cabinet_a."""
        _with_settings(
            monkeypatch, {"cities": {city: _CABINET_A for city in _SPLIT_CITIES}}
        )
        for city in _SPLIT_CITIES:
            assert resolve_account(city, "L2") == _CABINET_A
        # CityF осталась в cabinet_b — кабинет из скана не пропал
        assert accounts_to_scan() == (_CABINET_A, ACCOUNT_CABINET_B)


# ---------------------------------------------------------------------------
# Переопределения: слой routes (точечно по типу)
# ---------------------------------------------------------------------------


class TestRouteOverrides:
    def test_single_type_rollback(self, monkeypatch):
        """Откат миграции одного города — одна строка настроек."""
        _with_settings(monkeypatch, {"routes": {"CityA": {"L2": _CABINET_A}}})
        assert resolve_account("CityA", "L2") == _CABINET_A
        assert resolve_account("CityA", "L1") == _CABINET_A
        # Остальные города не задеты
        assert resolve_account("CityB", "L2") == ACCOUNT_CABINET_B

    def test_null_excludes_single_type_only(self, monkeypatch):
        _with_settings(monkeypatch, {"routes": {"CityD": {"MQL": None}}})
        with pytest.raises(LaunchRoutingError):
            resolve_account("CityD", "MQL")
        assert resolve_account("CityD", "L2") == ACCOUNT_CABINET_B
        assert "CityD" in all_cities()

    def test_routes_apply_over_cities(self, monkeypatch):
        """Слой routes точечно перебивает слой cities."""
        _with_settings(
            monkeypatch,
            {
                "cities": {"CityA": "555000111"},
                "routes": {"CityA": {"L2": "777000222"}},
            },
        )
        assert resolve_account("CityA", "L2") == "777000222"
        assert resolve_account("CityA", "L1") == "555000111"
        assert resolve_account("CityA", "MQL") == "555000111"

    def test_routes_can_add_type_to_new_city(self, monkeypatch):
        _with_settings(monkeypatch, {"routes": {"CityG": {"L1": "555000111"}}})
        assert resolve_account("CityG", "L1") == "555000111"
        with pytest.raises(LaunchRoutingError):
            resolve_account("CityG", "L2")

    def test_lowercase_type_key_accepted(self, monkeypatch):
        _with_settings(monkeypatch, {"routes": {"CityA": {"l2": _CABINET_A}}})
        assert resolve_account("CityA", "L2") == _CABINET_A


# ---------------------------------------------------------------------------
# Тип PRODB: PRODB-адсеты всех городов карты в cabinet_b
# ---------------------------------------------------------------------------


_PRODB_CITIES = _SPLIT_CITIES + ("CityF",)


class TestEntRouting:
    def test_prodb_of_all_cities_routes_to_cabinet_b(self):
        for city in _PRODB_CITIES:
            assert resolve_account(city, "PRODB") == ACCOUNT_CABINET_B
            assert route_account(city, "PRODB") == ACCOUNT_CABINET_B

    def test_types_for_city_include_prodb(self):
        assert types_for_city("CityA")["PRODB"] == ACCOUNT_CABINET_B
        # У CityF MQL по-прежнему нет, а PRODB есть.
        assert types_for_city("CityF") == {
            "L2": ACCOUNT_CABINET_B,
            "L1": ACCOUNT_CABINET_B,
            "PRODB": ACCOUNT_CABINET_B,
        }

    def test_city_layer_does_not_move_prodb(self, monkeypatch):
        """«Вернуть город в cabinet_a» не утаскивает туда PRODB-адсет, которого там нет."""
        _with_settings(monkeypatch, {"cities": {"CityA": _CABINET_A}})
        assert resolve_account("CityA", "L2") == _CABINET_A
        assert resolve_account("CityA", "PRODB") == ACCOUNT_CABINET_B

    def test_city_layer_null_excludes_prodb_too(self, monkeypatch):
        """null = город вне запуска целиком; выключить только PRODB — через routes."""
        _with_settings(monkeypatch, {"cities": {"CityA": None}})
        with pytest.raises(LaunchRoutingError):
            resolve_account("CityA", "L2")
        with pytest.raises(LaunchRoutingError):
            resolve_account("CityA", "PRODB")
        assert "CityA" not in all_cities()

    def test_route_layer_null_on_prodb_keeps_city_for_proda(self, monkeypatch):
        """Точечное выключение PRODB не выкидывает город из «всех городов»."""
        _with_settings(monkeypatch, {"routes": {"CityF": {"PRODB": None}}})
        assert types_for_city("CityF") == {
            "L2": ACCOUNT_CABINET_B,
            "L1": ACCOUNT_CABINET_B,
        }
        assert "CityF" in all_cities()

    def test_route_layer_null_disables_prodb_of_one_city(self, monkeypatch):
        _with_settings(monkeypatch, {"routes": {"CityA": {"PRODB": None}}})
        with pytest.raises(LaunchRoutingError, match="не маршрутизирован"):
            resolve_account("CityA", "PRODB")
        # Остальные типы города и PRODB других городов не задеты.
        assert resolve_account("CityA", "L2") == ACCOUNT_CABINET_B
        assert resolve_account("CityB", "PRODB") == ACCOUNT_CABINET_B
        assert "CityA" in all_cities()

    def test_route_layer_can_move_prodb(self, monkeypatch):
        _with_settings(monkeypatch, {"routes": {"CityB": {"prodb": "555000111"}}})
        assert resolve_account("CityB", "PRODB") == "555000111"

    def test_fingerprint_changes_when_prodb_disabled(self, monkeypatch):
        before = routing_fingerprint()
        _with_settings(monkeypatch, {"routes": {"CityA": {"PRODB": None}}})
        assert routing_fingerprint() != before

    def test_scan_scope_and_city_list_unchanged_by_prodb(self):
        """PRODB живёт в уже сканируемом кабинете и в уже известных городах."""
        assert accounts_to_scan() == (_CABINET_A, ACCOUNT_CABINET_B)
        assert all_cities() == [
            "CityA", "CityB", "CityC", "CityD", "CityE", "CityF",
        ]


# ---------------------------------------------------------------------------
# Мусор в настройках: игнор записи, карта остаётся дефолтной
# ---------------------------------------------------------------------------


class TestInvalidSettings:
    def test_invalid_city_entries_ignored(self, monkeypatch, caplog):
        _with_settings(
            monkeypatch,
            {"cities": {
                "CityF": "not-a-number",   # не цифры — игнор, дефолт остаётся
                "": "123456",                   # пустой город — игнор
                "CityE": True,                 # bool — не account_id
            }},
        )
        with caplog.at_level("WARNING"):
            table = get_route_table()
        assert table[("CityF", "L2")] == ACCOUNT_CABINET_B
        assert table[("CityE", "L1")] == _CABINET_A
        assert not any(city == "" for city, _ in table)
        assert "игнорирую" in caplog.text

    def test_unknown_type_in_routes_ignored(self, monkeypatch, caplog):
        _with_settings(
            monkeypatch, {"routes": {"CityA": {"XX": "555000111", "L2": _CABINET_A}}}
        )
        with caplog.at_level("WARNING"):
            table = get_route_table()
        assert ("CityA", "XX") not in table
        assert table[("CityA", "L2")] == _CABINET_A
        assert "игнорирую" in caplog.text

    def test_non_dict_routes_entry_ignored(self, monkeypatch):
        _with_settings(monkeypatch, {"routes": {"CityA": "мусор"}})
        assert resolve_account("CityA", "L2") == ACCOUNT_CABINET_B

    def test_invalid_account_in_routes_ignored(self, monkeypatch, caplog):
        _with_settings(monkeypatch, {"routes": {"CityA": {"L2": "not-a-number"}}})
        with caplog.at_level("WARNING"):
            assert resolve_account("CityA", "L2") == ACCOUNT_CABINET_B
        assert "игнорирую" in caplog.text

    def test_non_dict_block_ignored(self, monkeypatch):
        monkeypatch.setattr(
            scheduler, "load_settings", lambda: {"launch_routing": ["мусор"]}
        )
        assert resolve_account("CityF", "L2") == ACCOUNT_CABINET_B

    def test_broken_settings_fall_back_to_defaults(self, monkeypatch):
        def _boom():
            raise OSError("settings.json недоступен")

        monkeypatch.setattr(scheduler, "load_settings", _boom)
        assert resolve_account("CityA", "L1") == _CABINET_A
        assert resolve_account("CityA", "L2") == ACCOUNT_CABINET_B


# ---------------------------------------------------------------------------
# Отпечаток карты — ключ кеша discovery и часть подписи конфига запуска
# ---------------------------------------------------------------------------


class TestRoutingFingerprint:
    def test_stable_for_same_table(self):
        assert routing_fingerprint() == routing_fingerprint()

    def test_changes_when_settings_change(self, monkeypatch):
        before = routing_fingerprint()
        _with_settings(monkeypatch, {"routes": {"CityA": {"L2": _CABINET_A}}})
        assert routing_fingerprint() != before

    def test_returns_to_previous_value_after_rollback(self, monkeypatch):
        before = routing_fingerprint()
        _with_settings(monkeypatch, {"routes": {"CityA": {"L2": _CABINET_A}}})
        monkeypatch.setattr(scheduler, "load_settings", lambda: {})
        assert routing_fingerprint() == before


# ---------------------------------------------------------------------------
# Слой «наблюдать, но не запускать»: observe_accounts
# ---------------------------------------------------------------------------

# Сценарий: последняя пара cabinet_a — MQL расщеплённых городов — выключается в
# settings. Кабинет уходит из карты, но не из наблюдения. L1 расщеплённых городов
# уведены в cabinet_b правкой settings.
_CABINET_A_OFF = {
    "routes": {
        city: {"L1": ACCOUNT_CABINET_B, "MQL": None} for city in _SPLIT_CITIES
    },
}


class TestObserveAccounts:
    def test_default_observe_is_cabinet_a(self):
        assert observe_accounts() == (_CABINET_A,)

    def test_cabinet_a_without_routes_stays_in_scan_scope(self, monkeypatch):
        """MQL → null у расщеплённых городов: запуск в cabinet_a невозможен, скан остаётся."""
        _with_settings(monkeypatch, _CABINET_A_OFF)
        assert _CABINET_A not in get_route_table().values()
        assert cities_for_account(_CABINET_A) == ()
        for city in _SPLIT_CITIES:
            with pytest.raises(LaunchRoutingError, match="не маршрутизирован"):
                resolve_account(city, "MQL")
            assert route_account(city, "MQL") is None
        # Наблюдение живёт отдельно от права на запуск.
        assert accounts_to_scan() == (_CABINET_A, ACCOUNT_CABINET_B)
        # Список городов и PRODA/PRODB-маршруты не задеты.
        assert all_cities() == [
            "CityA", "CityB", "CityC", "CityD", "CityE", "CityF",
        ]
        for city in _SPLIT_CITIES:
            assert resolve_account(city, "L2") == ACCOUNT_CABINET_B
            assert resolve_account(city, "L1") == ACCOUNT_CABINET_B
            assert resolve_account(city, "PRODB") == ACCOUNT_CABINET_B

    def test_observe_null_disables_layer(self, monkeypatch):
        _with_settings(monkeypatch, {**_CABINET_A_OFF, "observe_accounts": None})
        assert observe_accounts() == ()
        assert accounts_to_scan() == (ACCOUNT_CABINET_B,)

    def test_observe_empty_list_disables_layer(self, monkeypatch):
        _with_settings(monkeypatch, {**_CABINET_A_OFF, "observe_accounts": []})
        assert accounts_to_scan() == (ACCOUNT_CABINET_B,)

    def test_observe_extra_account_without_routes(self, monkeypatch):
        """Кабинет из observe виден скану, но запуск в него не открывается."""
        _with_settings(monkeypatch, {"observe_accounts": ["act_123456789"]})
        assert observe_accounts() == ("123456789",)
        assert accounts_to_scan() == (_CABINET_A, ACCOUNT_CABINET_B, "123456789")
        assert cities_for_account("123456789") == ()
        assert get_route_table() == get_route_table()  # карта не изменилась
        assert "123456789" not in get_route_table().values()

    def test_observe_dedupes_and_keeps_default_first(self, monkeypatch):
        _with_settings(
            monkeypatch,
            {
                **_CABINET_A_OFF,
                "observe_accounts": [
                    ACCOUNT_CABINET_B, f"act_{_CABINET_A}", _CABINET_A, ACCOUNT_CABINET_B,
                ],
            },
        )
        assert observe_accounts() == (ACCOUNT_CABINET_B, _CABINET_A)
        assert accounts_to_scan() == (_CABINET_A, ACCOUNT_CABINET_B)

    def test_observe_garbage_entries_ignored(self, monkeypatch, caplog):
        _with_settings(
            monkeypatch,
            {"observe_accounts": ["", "abc", "act_", "act_777", None]},
        )
        with caplog.at_level(logging.WARNING, logger="services.launch_routing"):
            assert observe_accounts() == ("777",)
        assert sum("невалидный account_id" in r.message for r in caplog.records) == 4

    def test_observe_not_a_list_keeps_default(self, monkeypatch, caplog):
        _with_settings(monkeypatch, {**_CABINET_A_OFF, "observe_accounts": "мусор"})
        with caplog.at_level(logging.WARNING, logger="services.launch_routing"):
            assert observe_accounts() == (_CABINET_A,)
        assert any("не список" in r.message for r in caplog.records)
        assert accounts_to_scan() == (_CABINET_A, ACCOUNT_CABINET_B)

    def test_fingerprint_ignores_observe_layer(self, monkeypatch):
        """Отпечаток — про маршруты; наблюдение не роняет застейдженный запуск."""
        base = routing_fingerprint()
        _with_settings(monkeypatch, {"observe_accounts": ["act_123456789"]})
        assert routing_fingerprint() == base
