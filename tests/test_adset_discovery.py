"""Тесты agent/adset_discovery.py — мультикабинетный скан адсетов.

Оффлайн-discovery обходит все кабинеты карты роутинга
(services/launch_routing.py); карта работает парой (город, тип):
L2 расщеплённых городов — в «ACME cabinet_b», их L1 и MQL — в cabinet_a, CityF
целиком в cabinet_b. Проверяем:
- пара берётся ТОЛЬКО из своего кабинета (спящие дубли в чужом — warning + игнор);
- один город собирается из ДВУХ кабинетов и словари сливаются, а не заменяются;
- результат несёт accounts: {city: {type: account_id}};
- кеш per-кабинет с отпечатком карты, force_refresh обходит его;
- отказ любого кабинета = fallback на config, отфильтрованный картой;
- онлайн-режим остался одиночным сканом.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.adset_discovery as discovery
import agent.scheduler as scheduler
import config
from services.fb_token_provider import set_active_account
from services.launch_routing import ACCOUNT_CABINET_B, routing_fingerprint

_CABINET_A = str(config.FB_ACCOUNT_ID).removeprefix("act_")


_LIVE_CAMPAIGN = {"id": "camp-live", "name": "Боевая кампания", "effective_status": "ACTIVE"}
_DEAD_CAMPAIGN = {"id": "camp-dead", "name": "Кампания (старая)", "effective_status": "PAUSED"}


def _adset(adset_id: str, name: str, status: str = "ACTIVE", **extra) -> dict:
    # По умолчанию адсет живой кампании: скан отдаёт campaign{effective_status},
    # и боевой выбор требует ACTIVE-кампанию (_campaign_is_live).
    return {
        "id": adset_id,
        "name": name,
        "status": status,
        "created_time": "2026-08-01T10:00:00+0000",
        "campaign": dict(_LIVE_CAMPAIGN),
        **extra,
    }


# Кабинет cabinet_a: живые L1 и MQL расщеплённых городов, СПЯЩИЕ L2 после миграции,
# запаркованная CityF и псевдогород Онлайн.
_CABINET_A_ADSETS = [
    _adset("cta-l1", "Adset | L1 | PRODA | CityA | v3"),
    _adset("ctb-l1", "Adset | L1 | PRODA | CityB | v2"),
    # Спящие L2-дубли: L2 переехал в cabinet_b, эти адсеты остались PAUSED и
    # их никто не удаляет. Роутинг-фильтр обязан их игнорировать — иначе
    # креатив уходит сюда и не выходит.
    _adset("cta-l2-sleeping", "Adset | L2 | PRODA | CityA | v3", status="PAUSED"),
    _adset("ctb-l2-sleeping", "Adset | L2 | PRODA | CityB | v2", status="PAUSED"),
    # Запаркованные адсеты CityF в cabinet_a — город переехал в cabinet_b.
    _adset("ctf-old-l2", "Adset | L2 | PRODA | CityF | v1", status="PAUSED"),
    _adset("ctf-old-l1", "Adset | L1 | PRODA | CityF | v1", status="PAUSED"),
    # Псевдогород «Онлайн» вне карты роутинга — принимается из cabinet_a.
    _adset("onl-l2", "Онлайн L2 | Instagram | MQL_Online | ver1"),
    # MQL website CityA — остался в cabinet_a.
    _adset("cta-mql", "CityA | Instagram | MQL-CAPI | ver2"),
]

# Кабинет cabinet_b: живые L2 расщеплённых городов, живая CityF, чужой дубль L1.
_CABINET_B_ADSETS = [
    _adset("cta-l2-live", "Adset | L2 | PRODA | CityA | v3"),
    _adset("ctb-l2-live", "Adset | L2 | PRODA | CityB | v2"),
    _adset("120084330793960265", "Adset | L2 | MQL | CityF | v1"),
    _adset("120004955014264912", "Adset | L1 | MQL | CityF | v1"),
    # L1 CityA маршрутизирован в cabinet_a — дубль обязан отсеяться с warning.
    _adset("cta-l1-dup", "Adset | L1 | PRODA | CityA | dup"),
]


@pytest.fixture(autouse=True)
def _clean_context(monkeypatch):
    """Чистый кеш, оффлайн-контекст, дефолтная карта роутинга (без settings)."""
    monkeypatch.setattr(discovery, "_CACHE", {})
    monkeypatch.setattr(scheduler, "load_settings", lambda: {})
    set_active_account(None)
    yield
    set_active_account(None)


@pytest.fixture
def fetch_calls(monkeypatch):
    """Мок _fetch_account_adsets: отдаёт инвентарь по кабинету, считает вызовы."""
    calls: list[str] = []

    def _fake_fetch(account_id: str) -> list[dict]:
        calls.append(account_id)
        if account_id == _CABINET_A:
            return list(_CABINET_A_ADSETS)
        if account_id == ACCOUNT_CABINET_B:
            return list(_CABINET_B_ADSETS)
        raise AssertionError(f"Неожиданный кабинет в скане: {account_id}")

    monkeypatch.setattr(discovery, "_fetch_account_adsets", _fake_fetch)
    return calls


# ---------------------------------------------------------------------------
# Мультикабинетный скан и роутинг-фильтр по паре (город, тип)
# ---------------------------------------------------------------------------


class TestMultiAccountScan:
    def test_scans_both_accounts_and_merges(self, fetch_calls):
        result = discovery.discover_adsets(force_refresh=True)

        assert fetch_calls == [_CABINET_A, ACCOUNT_CABINET_B]
        assert result["source"] == "fb_api"
        # CityF — живая пара L2+L1 из cabinet_b
        assert result["leadgen"]["CityF"] == {
            "L2": "120084330793960265",
            "L1": "120004955014264912",
        }
        assert result["mql"]["CityA"] == "cta-mql"

    def test_split_city_assembled_from_two_accounts(self, fetch_calls):
        """Ключевой кейс миграции: L2 из cabinet_b, L1 из cabinet_a — в одном городе."""
        result = discovery.discover_adsets(force_refresh=True)

        assert result["leadgen"]["CityA"] == {
            "L2": "cta-l2-live",
            "L1": "cta-l1",
        }
        assert result["leadgen"]["CityB"] == {
            "L2": "ctb-l2-live",
            "L1": "ctb-l1",
        }

    def test_accounts_map_carries_account_per_pair(self, fetch_calls):
        result = discovery.discover_adsets(force_refresh=True)

        assert result["accounts"]["CityA"] == {
            "L2": ACCOUNT_CABINET_B,
            "L1": _CABINET_A,
            "MQL": _CABINET_A,
        }
        assert result["accounts"]["CityF"] == {
            "L2": ACCOUNT_CABINET_B,
            "L1": ACCOUNT_CABINET_B,
        }

    def test_sleeping_l2_in_cabinet_a_never_wins(self, fetch_calls, caplog):
        """Спящий L2-дубль в cabinet_a не должен подменить живой адсет cabinet_b."""
        with caplog.at_level("WARNING"):
            result = discovery.discover_adsets(force_refresh=True)

        assert result["leadgen"]["CityA"]["L2"] == "cta-l2-live"
        assert result["leadgen"]["CityB"]["L2"] == "ctb-l2-live"
        # В БОЕВОМ выборе спящих нет (в истории — есть, см. тест ниже)
        assert "cta-l2-sleeping" not in str(result["leadgen"])
        assert "ctb-l2-sleeping" not in str(result["leadgen"])
        assert "ctf-old-l2" not in str(result["leadgen"])
        assert "ctf-old-l1" not in str(result["leadgen"])
        assert "cta-l1-dup" not in str(result["leadgen"])
        assert "cta-l1-dup" not in str(result["mql"])
        assert "только история" in caplog.text
        assert "cta-l2-sleeping" in caplog.text
        assert "cta-l1-dup" in caplog.text

    def test_filtered_adsets_stay_in_old_for_historical_labeling(self, fetch_calls):
        """Отсеянные адсеты остаются в old: по нему размечается история.

        build_adset_map строит adset_id → (город, тип) из leadgen+old, а синк
        объявления с неизвестным adset_id ЗАТИРАЕТ city/adset_type пустыми
        (creative_backfill: ON CONFLICT … SET city = excluded.city). Выброси
        спящие L2-адсеты совсем — и докризисная статистика L2 потеряет город.
        """
        result = discovery.discover_adsets(force_refresh=True)

        assert "cta-l2-sleeping" in result["old"]["CityA"]["L2"]
        assert "ctb-l2-sleeping" in result["old"]["CityB"]["L2"]
        assert "cta-l1-dup" in result["old"]["CityA"]["L1"]
        # Запаркованная CityF из cabinet_a — тоже история, а не пропажа
        assert "ctf-old-l2" in result["old"]["CityF"]["L2"]
        assert "ctf-old-l1" in result["old"]["CityF"]["L1"]

    def test_adset_map_covers_both_live_and_sleeping(self, fetch_calls):
        """Карта разметки знает и живой адсет cabinet_b, и спящий из cabinet_a."""
        from agent.fb_common import build_adset_map

        adset_map = build_adset_map()

        assert adset_map["cta-l2-live"] == ("CityA", "L2")
        assert adset_map["cta-l2-sleeping"] == ("CityA", "L2")
        assert adset_map["ctf-old-l1"] == ("CityF", "L1")

    def test_unrouted_type_of_managed_city_is_dropped(self, monkeypatch, caplog):
        """У CityF нет маршрута MQL — её website-адсет не берётся ниоткуда."""
        def _fake_fetch(account_id: str) -> list[dict]:
            if account_id == _CABINET_A:
                return list(_CABINET_A_ADSETS)
            return list(_CABINET_B_ADSETS) + [
                _adset("ctf-mql", "CityF | Instagram | MQL-CAPI | ver1"),
            ]

        monkeypatch.setattr(discovery, "_fetch_account_adsets", _fake_fetch)
        with caplog.at_level("WARNING"):
            result = discovery.discover_adsets(force_refresh=True)

        assert "CityF" not in result["mql"]
        assert "ctf-mql" not in str(result["leadgen"])
        assert "не маршрутизирован" in caplog.text
        # В боевой выбор не попал, но для разметки истории остался
        assert "ctf-mql" in result["old"]["CityF"]["MQL"]

    def test_pseudo_city_online_accepted_from_cabinet_a_only(self, monkeypatch):
        def _fake_fetch(account_id: str) -> list[dict]:
            if account_id == _CABINET_A:
                return list(_CABINET_A_ADSETS)
            # Псевдогород «Онлайн» внезапно в cabinet_b — вне карты роутинга,
            # из не-дефолтного кабинета принимать нельзя.
            return list(_CABINET_B_ADSETS) + [
                _adset("onl-alien", "Онлайн L2 | Instagram | MQL_Online | чужой"),
            ]

        monkeypatch.setattr(discovery, "_fetch_account_adsets", _fake_fetch)
        result = discovery.discover_adsets(force_refresh=True)

        assert result["leadgen"]["Онлайн"] == {"L2": "onl-l2"}
        assert result["accounts"]["Онлайн"] == {"L2": _CABINET_A}
        assert "onl-alien" not in str(result["leadgen"])
        assert "onl-alien" in result["old"]["Онлайн"]["L2"]

    def test_old_adsets_merged_per_type_not_replaced(self, monkeypatch):
        """old города собирается из двух кабинетов и не перетирается вторым."""
        def _fake_fetch(account_id: str) -> list[dict]:
            if account_id == _CABINET_A:
                return list(_CABINET_A_ADSETS) + [
                    _adset(
                        "cta-l1-older",
                        "Adset | L1 | PRODA | CityA | v2",
                        status="PAUSED",
                    ),
                ]
            return list(_CABINET_B_ADSETS) + [
                _adset(
                    "cta-l2-older",
                    "Adset | L2 | PRODA | CityA | v2",
                    status="PAUSED",
                ),
            ]

        monkeypatch.setattr(discovery, "_fetch_account_adsets", _fake_fetch)
        result = discovery.discover_adsets(force_refresh=True)

        assert "cta-l1-older" in result["old"]["CityA"]["L1"]
        assert "cta-l2-older" in result["old"]["CityA"]["L2"]
        # Слияние по типу, а не замена словаря города вторым кабинетом
        assert set(result["old"]["CityA"]) == {"L2", "L1"}


# ---------------------------------------------------------------------------
# PRODB-адсеты (тип PRODB) — боевой выбор из cabinet_b
# ---------------------------------------------------------------------------


_PRODB_CITYA_NAME = "Adset | PRODB | MQL | CityA | v1"


class TestEntDiscovery:
    """Без пары (город, PRODB) в карте PRODB-адсеты уезжали
    в old; PRODB-карточки при этом резолвились по языку в PRODA-адсеты."""

    def _discover(self, monkeypatch, cabinet_a_extra=(), cabinet_b_extra=()):
        def _fake_fetch(account_id: str) -> list[dict]:
            if account_id == _CABINET_A:
                return list(_CABINET_A_ADSETS) + list(cabinet_a_extra)
            if account_id == ACCOUNT_CABINET_B:
                return list(_CABINET_B_ADSETS) + list(cabinet_b_extra)
            raise AssertionError(f"Неожиданный кабинет в скане: {account_id}")

        monkeypatch.setattr(discovery, "_fetch_account_adsets", _fake_fetch)
        return discovery.discover_adsets(force_refresh=True)

    def test_prodb_adset_in_cabinet_b_is_live_target(self, monkeypatch):
        result = self._discover(
            monkeypatch,
            cabinet_b_extra=[_adset("prodb-cta", _PRODB_CITYA_NAME)],
        )

        assert result["source"] == "fb_api"
        assert result["leadgen"]["CityA"]["PRODB"] == "prodb-cta"
        assert result["accounts"]["CityA"]["PRODB"] == ACCOUNT_CABINET_B
        # PRODA-пары города не задеты
        assert result["leadgen"]["CityA"]["L2"] == "cta-l2-live"
        assert result["leadgen"]["CityA"]["L1"] == "cta-l1"

    def test_prodb_adset_in_cabinet_a_is_history_only(self, monkeypatch, caplog):
        """Пара (CityA, PRODB) маршрутизирована в cabinet_b — дубль в cabinet_a не цель."""
        with caplog.at_level("WARNING"):
            result = self._discover(
                monkeypatch,
                cabinet_a_extra=[_adset("prodb-cta-cabinet_a", _PRODB_CITYA_NAME)],
                cabinet_b_extra=[_adset("prodb-cta", _PRODB_CITYA_NAME)],
            )

        assert result["leadgen"]["CityA"]["PRODB"] == "prodb-cta"
        assert "prodb-cta-cabinet_a" not in str(result["leadgen"])
        assert "prodb-cta-cabinet_a" in result["old"]["CityA"]["PRODB"]
        assert "маршрутизирована в act_" in caplog.text
        assert "prodb-cta-cabinet_a" in caplog.text

    def test_prodb_adset_in_dead_campaign_drops_out(self, monkeypatch):
        """PRODB-адсет выключенной кампании — не боевая цель (fail-closed)."""
        result = self._discover(
            monkeypatch,
            cabinet_b_extra=[
                _adset("prodb-cta-dead", _PRODB_CITYA_NAME, campaign=dict(_DEAD_CAMPAIGN)),
            ],
        )

        assert "PRODB" not in result["leadgen"].get("CityA", {})
        assert "PRODB" not in result["accounts"].get("CityA", {})
        assert "prodb-cta-dead" in result["old"]["CityA"]["PRODB"]

    def test_prodb_adset_labels_history_map(self, monkeypatch):
        """Разметка объявлений знает PRODB-адсет как (город, PRODB)."""
        from agent.fb_common import build_adset_map

        self._discover(
            monkeypatch,
            cabinet_b_extra=[_adset("prodb-cta", _PRODB_CITYA_NAME)],
        )
        adset_map = build_adset_map()

        assert adset_map["prodb-cta"] == ("CityA", "PRODB")


# ---------------------------------------------------------------------------
# Кеш per-кабинет
# ---------------------------------------------------------------------------


class TestPerAccountCache:
    def test_second_call_served_from_cache(self, fetch_calls):
        first = discovery.discover_adsets()
        second = discovery.discover_adsets()

        assert first["source"] == "fb_api"
        assert second["source"] == "cache"
        # По одному fetch на кабинет, второй вызов — из кеша
        assert fetch_calls == [_CABINET_A, ACCOUNT_CABINET_B]
        assert second["leadgen"] == first["leadgen"]
        assert second["accounts"] == first["accounts"]

    def test_force_refresh_bypasses_cache(self, fetch_calls):
        discovery.discover_adsets()
        discovery.discover_adsets(force_refresh=True)

        assert fetch_calls == [
            _CABINET_A, ACCOUNT_CABINET_B,
            _CABINET_A, ACCOUNT_CABINET_B,
        ]

    def test_cache_is_keyed_per_account_and_routing(self, fetch_calls):
        discovery.discover_adsets()
        fingerprint = routing_fingerprint()[:8]
        assert f"offline:{_CABINET_A}:{fingerprint}" in discovery._CACHE
        assert f"offline:{ACCOUNT_CABINET_B}:{fingerprint}" in discovery._CACHE

    def test_stale_routing_cache_entries_are_evicted(self, fetch_calls, monkeypatch):
        """Записи с прежним отпечатком карты не копятся в демоне до рестарта."""
        discovery.discover_adsets()
        assert len(discovery._CACHE) == 2

        for routed_account in (_CABINET_A, ACCOUNT_CABINET_B, _CABINET_A):
            monkeypatch.setattr(
                scheduler,
                "load_settings",
                lambda acc=routed_account: {
                    "launch_routing": {"routes": {"CityA": {"L2": acc}}}
                },
            )
            discovery.discover_adsets()
            assert len(discovery._CACHE) == 2, (
                f"кеш распух: {sorted(discovery._CACHE)}"
            )

    def test_settings_change_invalidates_cache_immediately(
        self, fetch_calls, monkeypatch
    ):
        """Переопределение карты обязано подействовать сразу, а не через TTL."""
        discovery.discover_adsets()
        assert fetch_calls == [_CABINET_A, ACCOUNT_CABINET_B]

        # Откат L2 CityA в cabinet_a без деплоя
        monkeypatch.setattr(
            scheduler,
            "load_settings",
            lambda: {"launch_routing": {"routes": {"CityA": {"L2": _CABINET_A}}}},
        )
        result = discovery.discover_adsets()

        assert result["source"] == "fb_api"  # кеш не переиспользован
        assert fetch_calls == [
            _CABINET_A, ACCOUNT_CABINET_B,
            _CABINET_A, ACCOUNT_CABINET_B,
        ]
        # L2 CityA теперь берётся из cabinet_a — спящий адсет стал живым маршрутом
        assert result["leadgen"]["CityA"]["L2"] == "cta-l2-sleeping"
        assert result["accounts"]["CityA"]["L2"] == _CABINET_A


# ---------------------------------------------------------------------------
# Fail-closed: отказ кабинета = fallback, отфильтрованный картой
# ---------------------------------------------------------------------------


class TestFallback:
    def test_any_account_failure_falls_back_to_config(self, monkeypatch):
        def _fake_fetch(account_id: str) -> list[dict]:
            if account_id == _CABINET_A:
                return list(_CABINET_A_ADSETS)
            raise RuntimeError("FB API act_%s: 500" % account_id)

        monkeypatch.setattr(discovery, "_fetch_account_adsets", _fake_fetch)
        result = discovery.discover_adsets(force_refresh=True)

        # Частичный результат не притворяется полным: статический config
        assert result["source"] == "fallback"
        # CityF в config.ADSETS нет — её запуск без live discovery откажет
        assert "CityF" not in result["leadgen"]

    def test_fallback_drops_l2_of_migrated_cities(self, monkeypatch):
        """В config.ADSETS лежат СПЯЩИЕ L2-id — отдавать их конвейеру нельзя."""
        monkeypatch.setattr(
            discovery,
            "_fetch_account_adsets",
            lambda account_id: (_ for _ in ()).throw(RuntimeError("FB down")),
        )
        result = discovery.discover_adsets(force_refresh=True)

        assert result["source"] == "fallback"
        for city in ("CityA", "CityB", "CityC", "CityD", "CityE"):
            assert "L2" not in result["leadgen"].get(city, {}), (
                f"{city}/L2 из config ушёл бы в спящий адсет cabinet_a"
            )
            if city in result["leadgen"]:
                assert result["accounts"][city]["L1"] == _CABINET_A

    def test_fallback_keeps_dropped_l2_for_historical_labeling(self, monkeypatch):
        """L2 из config не идёт в запуск, но остаётся размечающим историю."""
        monkeypatch.setattr(
            discovery,
            "_fetch_account_adsets",
            lambda account_id: (_ for _ in ()).throw(RuntimeError("FB down")),
        )
        result = discovery.discover_adsets(force_refresh=True)

        for city, types in config.ADSETS.items():
            static_l2 = (types or {}).get("L2")
            if not static_l2:
                continue
            assert static_l2 not in str(result["leadgen"])
            assert static_l2 in result["old"][city]["L2"]

    def test_fallback_keeps_l1_of_cabinet_a_cities(self, monkeypatch):
        monkeypatch.setattr(
            discovery,
            "_fetch_account_adsets",
            lambda account_id: (_ for _ in ()).throw(RuntimeError("FB down")),
        )
        result = discovery.discover_adsets(force_refresh=True)

        static_l1 = {
            city: types["L1"]
            for city, types in config.ADSETS.items()
            if isinstance(types, dict) and "L1" in types
        }
        assert static_l1, "фикстура опирается на L1-адсеты в config.ADSETS"
        for city, adset_id in static_l1.items():
            assert result["leadgen"][city]["L1"] == adset_id

    def test_empty_leadgen_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            discovery, "_fetch_account_adsets", lambda account_id: []
        )
        result = discovery.discover_adsets(force_refresh=True)
        assert result["source"] == "fallback"


# ---------------------------------------------------------------------------
# Онлайн-режим — одиночный скан, карта роутинга не участвует
# ---------------------------------------------------------------------------


class TestOnlineMode:
    def test_online_single_account_scan(self, monkeypatch):
        calls: list[str] = []

        def _fake_fetch(account_id: str) -> list[dict]:
            calls.append(account_id)
            return [
                _adset(
                    "onl-1",
                    "L2/ видео 1",
                    destination_type="ON_AD",
                    optimization_goal="LEAD_GENERATION",
                ),
                _adset(
                    "onl-2",
                    "L1/ видео 2",
                    destination_type="ON_AD",
                    optimization_goal="LEAD_GENERATION",
                ),
            ]

        monkeypatch.setattr(discovery, "_fetch_account_adsets", _fake_fetch)
        set_active_account("online")
        try:
            result = discovery.discover_adsets(force_refresh=True)
        finally:
            set_active_account(None)

        online_id = str(config.FB_ACCOUNT_ID_ONLINE).removeprefix("act_")
        # Один кабинет, карта роутинга не сканируется
        assert calls == [online_id]
        assert result["leadgen"] == {"Онлайн": {"L2": "onl-1", "L1": "onl-2"}}
        # Форма accounts та же вложенная, что у мультикабинетного скана
        assert result["accounts"] == {"Онлайн": {"L2": online_id, "L1": online_id}}

    def test_online_language_only_by_marker_token(self):
        """Язык онлайн-адсета — только по явному маркеру-токену l2/l1."""
        assert discovery._detect_adset_lang("L2/ видео 1") == "L2"
        assert discovery._detect_adset_lang("l1 видео 2") == "L1"
        assert discovery._detect_adset_lang("Видео | l2 | v1") == "L2"
        assert discovery._detect_adset_lang("[L1] Лид-форма") == "L1"
        # Префикс сильнее токена дальше по имени
        assert discovery._detect_adset_lang("L1/ видео (l2)") == "L1"
        # Маркер внутри слова и имя без маркера — язык не определён
        assert discovery._detect_adset_lang("Видео model2l2x") is None
        assert discovery._detect_adset_lang("Видео без маркера") is None

    def test_online_failure_returns_empty_not_config(self, monkeypatch):
        def _boom(account_id: str) -> list[dict]:
            raise RuntimeError("FB down")

        monkeypatch.setattr(discovery, "_fetch_account_adsets", _boom)
        set_active_account("online")
        try:
            result = discovery.discover_adsets(force_refresh=True)
        finally:
            set_active_account(None)

        assert result["source"] == "fallback"
        assert result["leadgen"] == {}
        assert result["accounts"] == {}


# ---------------------------------------------------------------------------
# Гейт живости кампании: цель роутинга всегда в живой кампании
# ---------------------------------------------------------------------------


class TestCampaignLivenessGate:
    """Регрессия: (CityA, L2) разрешалась в ACTIVE-адсет давно выключенной
    кампании «Кампания (старая)» — объявления создавались бы в
    неоткручиваемую кампанию. Боевой целью может быть только адсет кампании
    с effective_status == ACTIVE."""

    def _discover(self, monkeypatch, cabinet_a_adsets, cabinet_b_adsets=()):
        def _fake_fetch(account_id: str) -> list[dict]:
            if account_id == _CABINET_A:
                return list(cabinet_a_adsets)
            if account_id == ACCOUNT_CABINET_B:
                return list(cabinet_b_adsets)
            raise AssertionError(f"Неожиданный кабинет в скане: {account_id}")

        monkeypatch.setattr(discovery, "_fetch_account_adsets", _fake_fetch)
        return discovery.discover_adsets(force_refresh=True)

    def test_zombie_active_adset_in_paused_campaign_never_wins(self, monkeypatch):
        """ACTIVE-адсет мёртвой кампании (свежее боевого!) не становится целью."""
        result = self._discover(
            monkeypatch,
            cabinet_a_adsets=[
                _adset(
                    "cta-l1-live",
                    "Adset | L1 | PRODA | CityA | v3",
                    created_time="2026-07-16T10:00:00+0000",
                ),
                _adset(
                    "cta-l1-zombie",
                    "Adset | L1 | PRODA | CityA | zombie",
                    created_time="2026-08-20T10:00:00+0000",
                    campaign=dict(_DEAD_CAMPAIGN),
                ),
            ],
        )

        assert result["leadgen"]["CityA"]["L1"] == "cta-l1-live"
        # Зомби не пропадает: историческая разметка живёт в old
        assert "cta-l1-zombie" in result["old"]["CityA"]["L1"]

    def test_pair_with_only_dead_campaigns_drops_out_fail_closed(
        self, monkeypatch, caplog
    ):
        """Все кандидаты в мёртвых кампаниях → пары нет, запуск откажет громко."""
        with caplog.at_level("WARNING"):
            result = self._discover(
                monkeypatch,
                cabinet_a_adsets=[
                    _adset("cta-l1-live", "Adset | L1 | PRODA | CityA | v3"),
                    _adset(
                        "cta-mql-dead",
                        "CityA | Instagram | MQL-CAPI | ver2",
                        status="PAUSED",
                        campaign=dict(_DEAD_CAMPAIGN),
                    ),
                ],
            )

        assert "CityA" not in result["mql"]
        assert "cta-mql-dead" in result["old"]["CityA"]["MQL"]
        assert "нет адсетов в живой кампании" in caplog.text

    def test_adset_without_campaign_data_fails_closed(self, monkeypatch):
        """Скан без данных кампании — адсет не годится в боевые цели."""
        no_campaign = _adset("ctb-l1-naked", "Adset | L1 | PRODA | CityB | v2")
        no_campaign.pop("campaign")
        result = self._discover(
            monkeypatch,
            cabinet_a_adsets=[
                no_campaign,
                _adset("cta-l1-live", "Adset | L1 | PRODA | CityA | v3"),
            ],
        )

        assert "CityB" not in result["leadgen"]
        assert result["leadgen"]["CityA"]["L1"] == "cta-l1-live"

    def test_all_pairs_resolve_to_live_campaigns(self, fetch_calls):
        """Инвариант всей карты: каждая цель leadgen/mql — адсет живой кампании."""
        result = discovery.discover_adsets(force_refresh=True)

        target_ids = {
            adset_id
            for types in result["leadgen"].values()
            for adset_id in types.values()
        } | set(result["mql"].values())
        by_id = {
            adset["id"]: adset for adset in [*_CABINET_A_ADSETS, *_CABINET_B_ADSETS]
        }
        assert target_ids
        for adset_id in target_ids:
            campaign = by_id[adset_id].get("campaign") or {}
            assert campaign.get("effective_status") == "ACTIVE", (
                f"Цель {adset_id} в неживой кампании: {campaign}"
            )
