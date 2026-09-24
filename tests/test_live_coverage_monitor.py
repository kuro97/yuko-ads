"""Контрактные тесты полного read-only Facebook coverage inventory."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.coverage_monitor import (
    TRACKED_GROUPS,
    collect_live_coverage,
    configured_coverage_scopes,
)
from services.coverage_repository import CoverageAdset, CoverageScope, InventoryPage


NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "account-1"


def _scope(
    city: str,
    language: str,
    *adset_ids: str,
    statuses: Mapping[str, str] | None = None,
) -> CoverageScope:
    """Группа город × язык: все её адсеты сразу (по умолчанию все ACTIVE)."""
    return CoverageScope(
        account_id=ACCOUNT_ID,
        city=city,
        language=language,  # type: ignore[arg-type]
        adsets=tuple(
            CoverageAdset(
                adset_id=adset_id,
                status=(statuses or {}).get(adset_id, "ACTIVE"),
            )
            for adset_id in adset_ids
        ),
        min_active=2,
    )


def _ad(
    ad_id: str,
    scope: CoverageScope,
    *,
    adset_id: str | None = None,
    status: str = "ACTIVE",
    effective_status: str = "ACTIVE",
) -> dict[str, str]:
    return {
        "id": ad_id,
        "account_id": f"act_{scope.account_id}",
        "adset_id": adset_id or scope.adsets[0].adset_id,
        "status": status,
        "effective_status": effective_status,
    }


class FakeDirectory:
    """Живой каталог-заглушка: что кабинет отдаёт по каждой группе."""

    def __init__(
        self,
        groups: Mapping[tuple[str, str], tuple[CoverageAdset, ...]],
        *,
        error: Exception | None = None,
    ) -> None:
        self.groups = dict(groups)
        self.error = error
        self.calls: list[str] = []

    def list_group_adsets(
        self,
        account_id: str,
    ) -> Mapping[tuple[str, str], tuple[CoverageAdset, ...]]:
        self.calls.append(account_id)
        if self.error is not None:
            raise self.error
        return self.groups


class FakeInventoryClient:
    def __init__(
        self,
        pages: Mapping[
            tuple[str, str | None],
            InventoryPage | Exception,
        ],
    ) -> None:
        self.pages = dict(pages)
        self.calls: list[tuple[str, str | None]] = []

    def fetch_page(
        self,
        scope: CoverageScope,
        after: str | None,
    ) -> InventoryPage:
        key = (scope.group_key, after)
        self.calls.append(key)
        result = self.pages[key]
        if isinstance(result, Exception):
            raise result
        return result


def test_configured_scopes_fall_back_to_static_map_without_directory() -> None:
    """Без живого каталога остаётся офлайн-фолбэк config.ADSETS."""
    from config import ADSETS

    scopes = configured_coverage_scopes(account_id="act_123")

    # Базово групп 10 (расщеплённые города × L2/L1); плюс PRODB всех
    # городов карты (страж видит PRODB), но статичный фолбэк знает только PRODA-пары.
    assert len(scopes) == len(TRACKED_GROUPS) == 16
    assert {scope.city for scope in scopes} == set(ADSETS) | {"CityF"}
    assert {scope.language for scope in scopes} == {"L2", "L1", "PRODB"}
    assert all(scope.account_id == "123" for scope in scopes)
    assert {
        (scope.city, scope.language, adset.adset_id)
        for scope in scopes
        for adset in scope.adsets
    } == {
        (city, language, adset_id)
        for city, languages in ADSETS.items()
        for language, adset_id in languages.items()
    }
    # PRODB в config.ADSETS нет — состав PRODB-групп в фолбэке неизвестен (UNKNOWN).
    assert all(scope.adsets == () for scope in scopes if scope.language == "PRODB")


def test_configured_scopes_take_whole_group_from_live_directory() -> None:
    """Группа — все адсеты города × языка из кабинета, а не один ID из карты."""
    from config import ADSETS

    directory = FakeDirectory(
        {
            ("CityA", "L2"): (
                CoverageAdset("live-l2-1", "ACTIVE"),
                CoverageAdset("live-l2-2", "PAUSED"),
                CoverageAdset("live-l2-3", "PAUSED"),
            ),
        }
    )

    scopes = configured_coverage_scopes(account_id="act_123", directory=directory)

    citya_l2 = next(
        scope
        for scope in scopes
        if (scope.city, scope.language) == ("CityA", "L2")
    )
    assert [adset.adset_id for adset in citya_l2.adsets] == [
        "live-l2-1",
        "live-l2-2",
        "live-l2-3",
    ]
    assert directory.calls == ["123"]
    # Статичная карта в живом пути не участвует вообще.
    static_ids = {
        adset_id
        for languages in ADSETS.values()
        for adset_id in languages.values()
    }
    assert not static_ids & {
        adset.adset_id for scope in scopes for adset in scope.adsets
    }
    # Ключ инцидента групповой: смена состава адсетов его не двигает.
    assert citya_l2.group_key == "123|CityA|L2"


def test_live_directory_failure_gives_unknown_never_static_fallback() -> None:
    """Каталог упал — группы без состава (UNKNOWN), а не подмена из config."""
    directory = FakeDirectory({}, error=TimeoutError("fb down"))

    scopes = configured_coverage_scopes(account_id="act_123", directory=directory)
    snapshot = collect_live_coverage(
        client=FakeInventoryClient({}),
        scopes=scopes,
        now=NOW,
    )

    assert len(scopes) == len(TRACKED_GROUPS) == 16
    assert all(scope.adsets == () for scope in scopes)
    assert snapshot.fetch_complete is False
    assert snapshot.observed_group_count == 0
    assert {group.status for group in snapshot.groups} == {"UNKNOWN"}
    assert all(group.effective_active_count is None for group in snapshot.groups)


def test_group_is_ok_when_only_one_of_three_adsets_has_active_ads() -> None:
    """Владелец пересоздал адсеты: живой один — покрытие есть, а не ноль."""
    scope = _scope(
        "CityA",
        "L2",
        "adset-ver3",
        "adset-ver2",
        "adset-old",
        statuses={"adset-ver2": "PAUSED", "adset-old": "PAUSED"},
    )
    client = FakeInventoryClient(
        {
            (scope.group_key, None): InventoryPage(
                (
                    _ad("live-1", scope, adset_id="adset-ver3"),
                    _ad("live-2", scope, adset_id="adset-ver3"),
                    _ad(
                        "paused-1",
                        scope,
                        adset_id="adset-ver2",
                        status="ACTIVE",
                        effective_status="ADSET_PAUSED",
                    ),
                    _ad(
                        "paused-2",
                        scope,
                        adset_id="adset-old",
                        status="PAUSED",
                        effective_status="CAMPAIGN_PAUSED",
                    ),
                ),
                False,
                None,
            ),
        }
    )

    snapshot = collect_live_coverage(client=client, scopes=(scope,), now=NOW)

    group = snapshot.groups[0]
    assert group.status == "OK"
    assert group.effective_active_count == 2
    # Выключенные адсеты в счёт не идут, но остаются в деталях алерта.
    assert [
        (adset.adset_id, adset.status, adset.ads_total, adset.active_count)
        for adset in group.adsets
    ] == [
        ("adset-ver3", "ACTIVE", 2, 2),
        ("adset-ver2", "PAUSED", 1, 0),
        ("adset-old", "PAUSED", 1, 0),
    ]


def test_group_is_zero_only_when_no_adset_has_active_ads() -> None:
    scope = _scope(
        "CityA",
        "L2",
        "adset-a",
        "adset-b",
        statuses={"adset-a": "PAUSED", "adset-b": "PAUSED"},
    )
    client = FakeInventoryClient(
        {
            (scope.group_key, None): InventoryPage(
                (
                    _ad(
                        "dead-1",
                        scope,
                        adset_id="adset-a",
                        status="ACTIVE",
                        effective_status="ADSET_PAUSED",
                    ),
                    _ad(
                        "dead-2",
                        scope,
                        adset_id="adset-b",
                        status="PAUSED",
                        effective_status="PAUSED",
                    ),
                ),
                False,
                None,
            ),
        }
    )

    snapshot = collect_live_coverage(client=client, scopes=(scope,), now=NOW)

    assert snapshot.groups[0].status == "ZERO"
    assert snapshot.groups[0].effective_active_count == 0


def test_group_without_discovered_adsets_is_unknown_not_zero() -> None:
    """Пустой состав группы — это «не знаем», а не «рекламы нет»."""
    scope = _scope("CityD", "L2")
    client = FakeInventoryClient({})

    snapshot = collect_live_coverage(client=client, scopes=(scope,), now=NOW)

    assert snapshot.groups[0].status == "UNKNOWN"
    assert snapshot.groups[0].effective_active_count is None
    assert snapshot.observed_group_count == 0
    assert client.calls == []


def test_collects_all_pages_and_classifies_zero_thin_ok() -> None:
    zero = _scope("CityA", "L2", "adset-zero")
    thin = _scope("CityA", "L1", "adset-thin")
    ok = _scope("CityB", "L2", "adset-ok")
    client = FakeInventoryClient(
        {
            (zero.group_key, None): InventoryPage((), False, None),
            (thin.group_key, None): InventoryPage(
                (_ad("thin-1", thin),),
                False,
                None,
            ),
            (ok.group_key, None): InventoryPage(
                (_ad("ok-1", ok),),
                True,
                "next-1",
            ),
            (ok.group_key, "next-1"): InventoryPage(
                (
                    _ad("ok-2", ok),
                    _ad(
                        "paused",
                        ok,
                        status="PAUSED",
                        effective_status="PAUSED",
                    ),
                ),
                False,
                None,
            ),
        }
    )

    snapshot = collect_live_coverage(
        client=client,
        scopes=(zero, thin, ok),
        now=NOW,
    )

    assert snapshot.fetch_complete is True
    assert snapshot.error_code is None
    assert snapshot.configured_group_count == 3
    assert snapshot.observed_group_count == 3
    assert snapshot.page_count == 4
    assert [group.status for group in snapshot.groups] == ["ZERO", "THIN", "OK"]
    assert [group.effective_active_count for group in snapshot.groups] == [0, 1, 2]
    assert client.calls == [
        (zero.group_key, None),
        (thin.group_key, None),
        (ok.group_key, None),
        (ok.group_key, "next-1"),
    ]


@pytest.mark.parametrize(
    ("page", "error"),
    [
        (InventoryPage((), True, None), None),
        (InventoryPage((), False, None, scope_observed=False), None),
        (InventoryPage((), False, None, complete=False), None),
        (None, TimeoutError("sensitive provider detail")),
    ],
)
def test_partial_missing_or_error_is_unknown_never_zero(
    page: InventoryPage | None,
    error: Exception | None,
) -> None:
    scope = _scope("CityD", "L1", "adset-risk")
    client = FakeInventoryClient(
        {(scope.group_key, None): error if error is not None else page}  # type: ignore[dict-item]
    )

    snapshot = collect_live_coverage(
        client=client,
        scopes=(scope,),
        now=NOW,
    )

    assert snapshot.fetch_complete is False
    assert snapshot.observed_group_count == 0
    assert snapshot.groups[0].status == "UNKNOWN"
    assert snapshot.groups[0].effective_active_count is None


def test_unknown_status_and_scope_mismatch_are_unknown() -> None:
    unknown_status = _scope("CityE", "L2", "adset-status")
    wrong_scope = _scope("CityC", "L1", "adset-scope")
    client = FakeInventoryClient(
        {
            (unknown_status.group_key, None): InventoryPage(
                (
                    _ad(
                        "unknown-1",
                        unknown_status,
                        effective_status="NEW_PROVIDER_STATUS",
                    ),
                ),
                False,
                None,
            ),
            (wrong_scope.group_key, None): InventoryPage(
                (
                    {
                        **_ad("wrong-1", wrong_scope),
                        "adset_id": "another-adset",
                    },
                ),
                False,
                None,
            ),
        }
    )

    snapshot = collect_live_coverage(
        client=client,
        scopes=(unknown_status, wrong_scope),
        now=NOW,
    )

    assert [group.status for group in snapshot.groups] == ["UNKNOWN", "UNKNOWN"]
    assert snapshot.observed_group_count == 0


def test_coverage_modules_have_no_mutation_or_execution_imports() -> None:
    project_root = Path(__file__).resolve().parents[1]
    forbidden_import_fragments = {
        "action_gateway",
        "executor",
        "integrations.facebook",
        "owner_action",
        "proposal",
    }
    forbidden_calls = {
        "create_ad",
        "delete",
        "launch",
        "pause",
        "post",
        "resume",
        "update",
    }

    for relative_path in (
        "services/coverage_monitor.py",
        "services/coverage_repository.py",
    ):
        tree = ast.parse((project_root / relative_path).read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert not {
            module
            for module in imports
            if any(fragment in module for fragment in forbidden_import_fragments)
        }
        external_client_calls = {
            node.func.attr
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "client"
            )
        }
        assert external_client_calls.isdisjoint(forbidden_calls)
