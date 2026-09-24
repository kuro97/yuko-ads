"""Частый live-страж покрытия: критический ноль, тонкое покрытие, дедуп, fail-closed.

Проверяется услуга целиком (services.coverage_guard.run_coverage_guard) на
временной SQLite и заглушках FB/Telegram, плюс регистрация крона
web.app._cron_coverage_guard.

Никаких реальных FB/Telegram-вызовов: инвентарь и отправка подменены.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.coverage_guard import (
    CoverageAlertTelegramSender,
    FacebookAccountCoverageClient,
    run_coverage_guard,
)
from services.coverage_monitor import (
    MIN_ACTIVE_PER_GROUP,
    TRACKED_CITIES,
    TRACKED_GROUPS,
    TRACKED_TYPES,
    configured_coverage_scopes,
)
from services.coverage_repository import (
    CoverageAdset,
    CoverageRepository,
    InventoryPage,
)
from services.database_migrations import apply_runtime_migrations


NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
CHAT_ID = 4242
# Первая группа — как в кабинете после ротации: живой ver3 плюс запаузенный ver2.
FIRST_CITY = TRACKED_CITIES[0]
FIRST_LANGUAGE = TRACKED_TYPES[0]
FIRST_GROUP_ADSETS = ("citya-l2-ver3", "citya-l2-ver2")


@pytest.fixture
def coverage_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "coverage-guard.sqlite3"
    apply_runtime_migrations(str(db_path))
    return db_path


def _live_groups(
    first_group_adsets: tuple[str, ...] = FIRST_GROUP_ADSETS,
) -> dict[tuple[str, str], tuple[CoverageAdset, ...]]:
    """Живой каталог кабинета: все адсеты каждой группы TRACKED_GROUPS
    (город × L2/L1 плюс PRODB-адсеты всех городов карты)."""
    groups: dict[tuple[str, str], tuple[CoverageAdset, ...]] = {}
    for city, language in TRACKED_GROUPS:
        groups[(city, language)] = (
            CoverageAdset(f"{city}-{language}-ver3", "ACTIVE"),
        )
    groups[(FIRST_CITY, FIRST_LANGUAGE)] = tuple(
        CoverageAdset(adset_id, "ACTIVE" if index == 0 else "PAUSED")
        for index, adset_id in enumerate(first_group_adsets)
    )
    return groups


class _FakeDirectory:
    """Каталог-заглушка вместо чтения act_<id>/adsets."""

    def __init__(
        self,
        groups: dict[tuple[str, str], tuple[CoverageAdset, ...]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.groups = groups if groups is not None else _live_groups()
        self.error = error

    def list_group_adsets(self, account_id: str):
        if self.error is not None:
            raise self.error
        return self.groups


class _FakeInventory:
    """Живой инвентарь-заглушка: сколько ACTIVE отдать по каждому adset."""

    def __init__(
        self,
        *,
        default_active: int = MIN_ACTIVE_PER_GROUP,
        active_by_adset: dict[str, int] | None = None,
        failing_adsets: frozenset[str] = frozenset(),
    ) -> None:
        self.default_active = default_active
        self.active_by_adset = dict(active_by_adset or {})
        self.failing_adsets = failing_adsets
        self.calls: list[str] = []

    def fetch_page(self, scope, after) -> InventoryPage:
        assert after is None, "полный инвентарь отдаётся одной страницей"
        self.calls.append(scope.group_key)
        rows: list[dict[str, str]] = []
        for adset in scope.adsets:
            if adset.adset_id in self.failing_adsets:
                raise RuntimeError("facebook unavailable")
            active = self.active_by_adset.get(adset.adset_id, self.default_active)
            rows.extend(
                {
                    "id": f"{adset.adset_id}-ad-{index}",
                    "account_id": f"act_{scope.account_id}",
                    "adset_id": adset.adset_id,
                    "status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
                for index in range(active)
            )
        return InventoryPage(
            rows=tuple(rows),
            has_next=False,
            next_cursor=None,
            scope_observed=True,
            complete=True,
        )


class _FakeTelegram:
    """Подтверждаемая доставка: возвращает возрастающий message_id."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str) -> int:
        self.sent.append((chat_id, text))
        return len(self.sent)


def _guard(
    coverage_db: Path,
    inventory: _FakeInventory,
    telegram: _FakeTelegram,
    *,
    now: datetime = NOW,
    directory: _FakeDirectory | None = None,
) -> dict:
    return run_coverage_guard(
        client=inventory,
        directory=directory or _FakeDirectory(),
        telegram_client=telegram,
        repository=CoverageRepository(coverage_db),
        telegram_chat_id=CHAT_ID,
        worker_id="test-guard",
        now=now,
    )


def _live_scopes(directory: _FakeDirectory | None = None):
    """Скоупы ровно так, как их построит страж — из живого каталога."""
    return configured_coverage_scopes(directory=directory or _FakeDirectory())


def _first_group() -> tuple[tuple[str, ...], str, str]:
    """Первая группа стража: (все adset_id группы, город, язык)."""
    scope = _live_scopes()[0]
    return tuple(scope.adset_ids), scope.city, scope.language


def _group_actives(adset_ids: tuple[str, ...], total: int) -> dict[str, int]:
    """Раскладка «сколько ACTIVE в группе»: всё в первый адсет, остальные пусты."""
    return {
        adset_id: (total if index == 0 else 0)
        for index, adset_id in enumerate(adset_ids)
    }


def _rows(db_path: Path, sql: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Ноль ACTIVE → немедленный критический алерт
# ---------------------------------------------------------------------------


def test_zero_active_group_sends_immediate_critical_alert(coverage_db: Path) -> None:
    adset_ids, city, language = _first_group()
    inventory = _FakeInventory(active_by_adset=_group_actives(adset_ids, 0))
    telegram = _FakeTelegram()

    result = _guard(coverage_db, inventory, telegram)

    assert result["zero"] == [f"{city}/{language}"]
    assert result["thin"] == []
    assert result["ok"] is True
    # Алерт уходит в этом же прогоне — ждать следующего тика не нужно.
    assert result["sent_count"] == 1
    assert len(telegram.sent) == 1
    chat_id, text = telegram.sent[0]
    assert chat_id == CHAT_ID
    assert text.startswith("❗")
    assert "Критическое покрытие" in text
    assert f"{city}/{language}" in text
    assert "0 effective ACTIVE" in text
    # В деталях — вся группа: и живой адсет, и выключенные.
    for adset_id in adset_ids:
        assert adset_id in text


def test_live_group_with_one_active_adset_is_not_zero(coverage_db: Path) -> None:
    """Регрессия: владелец пересоздал адсеты — покрытие есть, алерта нет."""
    adset_ids, _city, _language = _first_group()
    assert len(adset_ids) > 1, "первая группа должна быть многоадсетной"
    # Живой только один адсет группы, остальные запаузены.
    inventory = _FakeInventory(
        active_by_adset=_group_actives(adset_ids, MIN_ACTIVE_PER_GROUP)
    )
    telegram = _FakeTelegram()

    result = _guard(coverage_db, inventory, telegram)

    assert result["zero"] == []
    assert result["thin"] == []
    assert result["opened_count"] == 0
    assert telegram.sent == []


def test_zero_alert_marks_incident_open_in_durable_state(coverage_db: Path) -> None:
    adset_ids, _city, _language = _first_group()
    inventory = _FakeInventory(active_by_adset=_group_actives(adset_ids, 0))

    result = _guard(coverage_db, inventory, _FakeTelegram())

    assert result["opened_count"] == 1
    assert result["queued_delivery_count"] == 1


# ---------------------------------------------------------------------------
# Ниже минимума → предупреждение
# ---------------------------------------------------------------------------


def test_below_minimum_group_sends_warning_not_critical(coverage_db: Path) -> None:
    adset_ids, city, language = _first_group()
    inventory = _FakeInventory(
        active_by_adset=_group_actives(adset_ids, MIN_ACTIVE_PER_GROUP - 1)
    )
    telegram = _FakeTelegram()

    result = _guard(coverage_db, inventory, telegram)

    assert result["thin"] == [f"{city}/{language}"]
    assert result["zero"] == []
    assert len(telegram.sent) == 1
    _chat_id, text = telegram.sent[0]
    assert text.startswith("⚠️")
    assert "Тонкое покрытие" in text
    assert "Критическое" not in text


def test_full_coverage_sends_nothing(coverage_db: Path) -> None:
    telegram = _FakeTelegram()

    result = _guard(coverage_db, _FakeInventory(), telegram)

    assert result["zero"] == []
    assert result["thin"] == []
    assert result["ok_count"] == len(_live_scopes())
    assert telegram.sent == []


# ---------------------------------------------------------------------------
# Дедуп: не спамим тем же инцидентом каждые 30 минут
# ---------------------------------------------------------------------------


def test_repeated_zero_group_is_not_realerted_every_run(coverage_db: Path) -> None:
    adset_ids, _city, _language = _first_group()
    inventory = _FakeInventory(active_by_adset=_group_actives(adset_ids, 0))
    telegram = _FakeTelegram()

    first = _guard(coverage_db, inventory, telegram, now=NOW)
    second = _guard(coverage_db, inventory, telegram, now=NOW + timedelta(minutes=30))
    third = _guard(coverage_db, inventory, telegram, now=NOW + timedelta(minutes=60))

    assert first["sent_count"] == 1
    # Подтверждённый ZERO-алерт больше не повторяется, пока инцидент открыт.
    assert second["sent_count"] == 0
    assert third["sent_count"] == 0
    assert second["opened_count"] == 0
    assert len(telegram.sent) == 1


def test_rotated_adsets_do_not_open_a_duplicate_incident(coverage_db: Path) -> None:
    """Владелец пересоздал адсеты — инцидент тот же, ключ группы не поменялся."""
    adset_ids, city, language = _first_group()
    telegram = _FakeTelegram()

    first = _guard(
        coverage_db,
        _FakeInventory(active_by_adset=_group_actives(adset_ids, 0)),
        telegram,
        now=NOW,
    )
    rotated_ids = ("citya-l2-ver4", "citya-l2-ver3")
    rotated_directory = _FakeDirectory(_live_groups(rotated_ids))
    second = _guard(
        coverage_db,
        _FakeInventory(active_by_adset=_group_actives(rotated_ids, 0)),
        telegram,
        now=NOW + timedelta(minutes=30),
        directory=rotated_directory,
    )

    assert first["zero"] == [f"{city}/{language}"]
    assert second["zero"] == [f"{city}/{language}"]
    assert second["opened_count"] == 0
    assert len(telegram.sent) == 1
    incidents = _rows(
        coverage_db,
        "SELECT group_key, incident_kind, state FROM coverage_incidents",
    )
    assert len(incidents) == 1
    assert incidents[0]["group_key"].endswith(f"|{city}|{language}")


def test_thin_group_is_not_realerted_within_a_day(coverage_db: Path) -> None:
    adset_ids, _city, _language = _first_group()
    inventory = _FakeInventory(
        active_by_adset=_group_actives(adset_ids, MIN_ACTIVE_PER_GROUP - 1)
    )
    telegram = _FakeTelegram()

    _guard(coverage_db, inventory, telegram, now=NOW)
    later = _guard(coverage_db, inventory, telegram, now=NOW + timedelta(hours=6))

    assert later["sent_count"] == 0
    assert len(telegram.sent) == 1


def test_recovered_group_reports_closed_incident(coverage_db: Path) -> None:
    """Ноль → покрытие есть: владелец получает «восстановлено», а не тишину."""
    adset_ids, city, language = _first_group()
    telegram = _FakeTelegram()
    broken = _FakeInventory(active_by_adset=_group_actives(adset_ids, 0))
    healthy = _FakeInventory()

    _guard(coverage_db, broken, telegram, now=NOW)
    first_ok = _guard(coverage_db, healthy, telegram, now=NOW + timedelta(minutes=30))
    second_ok = _guard(coverage_db, healthy, telegram, now=NOW + timedelta(minutes=60))

    assert first_ok["resolved_count"] == 0
    assert second_ok["resolved_count"] == 1
    assert len(telegram.sent) == 2
    _chat_id, text = telegram.sent[1]
    assert text.startswith("✅")
    assert "Покрытие восстановлено" in text
    assert f"{city}/{language}" in text


def test_resolved_group_reopens_alert_after_new_outage(coverage_db: Path) -> None:
    adset_ids, _city, _language = _first_group()
    telegram = _FakeTelegram()
    broken = _FakeInventory(active_by_adset=_group_actives(adset_ids, 0))
    healthy = _FakeInventory()

    _guard(coverage_db, broken, telegram, now=NOW)
    # Два подряд полных OK закрывают инцидент (и шлют «восстановлено»).
    _guard(coverage_db, healthy, telegram, now=NOW + timedelta(minutes=30))
    _guard(coverage_db, healthy, telegram, now=NOW + timedelta(minutes=60))
    reopened = _guard(coverage_db, broken, telegram, now=NOW + timedelta(minutes=90))

    assert reopened["opened_count"] == 1
    assert reopened["sent_count"] == 1
    assert [text.startswith("❗") for _chat, text in telegram.sent] == [
        True,
        False,
        True,
    ]


# ---------------------------------------------------------------------------
# Ошибка FB → fail-closed, без ложного «всё ок»
# ---------------------------------------------------------------------------


def test_facebook_error_fails_closed_without_false_all_clear(coverage_db: Path) -> None:
    scopes = _live_scopes()
    inventory = _FakeInventory(
        failing_adsets=frozenset(
            adset_id for scope in scopes for adset_id in scope.adset_ids
        )
    )
    telegram = _FakeTelegram()

    result = _guard(coverage_db, inventory, telegram)

    assert result["ok"] is False
    assert result["fetch_complete"] is False
    assert len(result["unknown"]) == len(scopes)
    # Ни «в норме», ни ложных нулей: неизвестное не выдаётся за наблюдённое.
    assert result["ok_count"] == 0
    assert result["zero"] == []
    assert result["thin"] == []
    assert telegram.sent == []


def test_adset_directory_error_fails_closed_without_false_zero(
    coverage_db: Path,
) -> None:
    """Каталог адсетов недоступен — всё UNKNOWN, статичная карта не спасает."""
    telegram = _FakeTelegram()

    result = _guard(
        coverage_db,
        _FakeInventory(),
        telegram,
        directory=_FakeDirectory(error=RuntimeError("fb adsets down")),
    )

    assert result["ok"] is False
    assert result["fetch_complete"] is False
    assert len(result["unknown"]) == len(_live_scopes())
    assert result["zero"] == []
    assert result["ok_count"] == 0
    assert telegram.sent == []


def test_static_adsets_map_is_never_used_in_the_live_path(
    coverage_db: Path,
) -> None:
    """Онлайн-путь стража ходит только в живой каталог — config.ADSETS не читает."""
    from config import ADSETS

    inventory = _FakeInventory()
    static_fallback = MagicMock(side_effect=AssertionError("config.ADSETS прочитан"))

    with patch("services.coverage_monitor._static_group_adsets", static_fallback):
        result = _guard(coverage_db, inventory, _FakeTelegram())

    static_fallback.assert_not_called()
    assert result["ok"] is True
    static_ids = {
        adset_id
        for languages in ADSETS.values()
        for adset_id in languages.values()
    }
    observed_ids = {
        adset_id
        for scope in _live_scopes()
        for adset_id in scope.adset_ids
    }
    assert not static_ids & observed_ids


def test_partial_facebook_error_still_reports_not_ok(coverage_db: Path) -> None:
    adset_ids, city, language = _first_group()
    inventory = _FakeInventory(failing_adsets=frozenset(adset_ids))
    telegram = _FakeTelegram()

    result = _guard(coverage_db, inventory, telegram)

    assert result["ok"] is False
    assert result["unknown"] == [f"{city}/{language}"]
    assert result["ok_count"] == len(_live_scopes()) - 1


def test_scopes_follow_route_map_after_l2_migration() -> None:
    """L2-группы смотрят в cabinet_b, L1 — в cabinet_a; в cabinet_a остались спящие."""
    import config
    from services.launch_routing import ACCOUNT_CABINET_B

    cabinet_a = str(config.FB_ACCOUNT_ID).removeprefix("act_")
    scopes = _live_scopes()

    by_pair = {(scope.city, scope.language): scope.account_id for scope in scopes}
    for city in ("CityA", "CityB", "CityC", "CityD", "CityE"):
        assert by_pair[(city, "L2")] == ACCOUNT_CABINET_B, (
            f"{city}/L2 смотрел бы на спящий адсет cabinet_a и слал ложный ноль"
        )
        assert by_pair[(city, "L1")] == cabinet_a
        # PRODB-адсеты всех городов карты — тоже cabinet_b.
        assert by_pair[(city, "PRODB")] == ACCOUNT_CABINET_B
    assert by_pair[("CityF", "PRODB")] == ACCOUNT_CABINET_B
    # У CityF под наблюдением только PRODB — PRODA-пары нет.
    assert ("CityF", "L2") not in by_pair
    assert ("CityF", "L1") not in by_pair
    assert len(scopes) == len(TRACKED_GROUPS) == 16


def test_static_fallback_does_not_leak_cabinet_a_adsets_into_other_cabinet() -> None:
    """Без живого каталога чужой кабинет остаётся UNKNOWN, а не мерит cabinet_a-id.

    config.ADSETS описывает только дефолтный кабинет и содержит СПЯЩИЕ L2-id.
    Отдай их группе L2 (её кабинет — cabinet_b) — клиент искал бы cabinet_a-адсеты
    в cabinet_b, не нашёл и выдал ложный критический ноль по живой группе.
    """
    import config
    from services.coverage_monitor import configured_coverage_scopes
    from services.launch_routing import ACCOUNT_CABINET_B

    cabinet_a = str(config.FB_ACCOUNT_ID).removeprefix("act_")
    scopes = configured_coverage_scopes()

    by_pair = {(scope.city, scope.language): scope for scope in scopes}
    l2_scope = by_pair[("CityA", "L2")]
    assert l2_scope.account_id == ACCOUNT_CABINET_B
    assert l2_scope.adsets == (), "состав из config.ADSETS относится к cabinet_a"
    l1_scope = by_pair[("CityA", "L1")]
    assert l1_scope.account_id == cabinet_a
    assert l1_scope.adsets, "своему кабинету статический фолбэк достаётся"


def test_scope_without_route_is_unknown_not_zero(monkeypatch, coverage_db: Path) -> None:
    """Пара, исключённая из карты, уходит в UNKNOWN, а не в критический ноль."""
    import agent.scheduler as scheduler

    monkeypatch.setattr(
        scheduler,
        "load_settings",
        lambda: {"launch_routing": {"routes": {"CityA": {"L2": None}}}},
    )
    telegram = _FakeTelegram()
    result = _guard(coverage_db, _FakeInventory(), telegram)

    assert "CityA/L2" in result["unknown"]
    assert "CityA/L2" not in (result["zero"] or [])


def test_production_inventory_client_reads_each_account_once_per_run() -> None:
    """Один GET НА КАБИНЕТ за прогон, и ошибка не опрашивает FB повторно.

    После расщепления роутинга группы живут в двух кабинетах (L2 в
    cabinet_b, L1 в cabinet_a), поэтому чтений столько же, сколько уникальных
    кабинетов среди скоупов — но по-прежнему НЕ по одному на каждую из десяти
    групп: кеш по account_id обязан их склеить.
    """
    scopes = _live_scopes()
    expected_accounts = {scope.account_id for scope in scopes}
    assert len(expected_accounts) < len(scopes), (
        "кеш по кабинету должен склеивать несколько групп"
    )
    fetch = MagicMock(return_value=[])
    client = FacebookAccountCoverageClient()

    with patch(
        "services.approval_source_facebook._load_account_inventory",
        fetch,
    ):
        for scope in scopes:
            client.fetch_page(scope, None)

    assert fetch.call_count == len(expected_accounts)

    failing = FacebookAccountCoverageClient()
    boom = MagicMock(side_effect=RuntimeError("fb down"))
    with patch("services.approval_source_facebook._load_account_inventory", boom):
        for scope in scopes:
            with pytest.raises(RuntimeError):
                failing.fetch_page(scope, None)

    assert boom.call_count == len(expected_accounts)


def test_production_inventory_client_filters_rows_to_group_adsets() -> None:
    scopes = _live_scopes()
    target, other = scopes[0], scopes[1]
    assert len(target.adset_ids) > 1, "первая группа должна быть многоадсетной"
    # Graph не возвращает account_id в строках act_<id>/ads — адаптер обязан
    # подставить запрошенный scope сам.
    rows = [
        {
            "id": f"ad-{adset_id}",
            "adset_id": adset_id,
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
        }
        for adset_id in sorted(target.adset_ids)
    ]
    rows.append(
        {
            "id": "ad-other",
            "adset_id": sorted(other.adset_ids)[0],
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
        }
    )
    client = FacebookAccountCoverageClient()

    with patch(
        "services.approval_source_facebook._load_account_inventory",
        MagicMock(return_value=rows),
    ):
        page = client.fetch_page(target, None)

    # Вся группа целиком и ничего чужого.
    assert sorted(str(row["adset_id"]) for row in page.rows) == sorted(
        target.adset_ids
    )
    assert page.rows[0]["account_id"] == target.account_id
    assert page.has_next is False
    assert page.complete is True


def test_production_adset_directory_groups_live_account_adsets() -> None:
    """Каталог кабинета раскладывается по группам город × L2/L1 по имени."""
    from services.coverage_guard import FacebookAdsetDirectory

    rows = (
        {"id": "ver3", "name": "Owner | L2 | CityA | ver3", "status": "ACTIVE"},
        {"id": "ver2", "name": "Owner | L2 | CityA | ver2", "status": "PAUSED"},
        {"id": "l1-1", "name": "Owner | L1 | CityA | ver3", "status": "ACTIVE"},
        {
            "id": "mql-1",
            "name": "Owner | CityA | Instagram | MQL-CAPI",
            "status": "ACTIVE",
        },
        # PRODB-адсеты: CityA под наблюдением, CityF — только PRODB,
        # а её PRODA-пара (L2) под наблюдение не попадает.
        {"id": "prodb-1", "name": "Owner | PRODB | MQL | SO CityA | ver1", "status": "ACTIVE"},
        {"id": "prodb-ctf", "name": "Owner | PRODB | MQL | SO CityF | ver1", "status": "ACTIVE"},
        {"id": "l2-ctf", "name": "Owner | L2 | MQL | SO CityF | ver1", "status": "ACTIVE"},
        {"id": "junk", "name": "Тест без города", "status": "ACTIVE"},
    )

    with patch(
        "services.approval_source_facebook._paginate",
        MagicMock(return_value=rows),
    ):
        groups = FacebookAdsetDirectory().list_group_adsets("123")

    assert set(groups) == {
        ("CityA", "L2"),
        ("CityA", "L1"),
        ("CityA", "PRODB"),
        ("CityF", "PRODB"),
    }
    assert [adset.adset_id for adset in groups[("CityA", "PRODB")]] == ["prodb-1"]
    assert [adset.adset_id for adset in groups[("CityF", "PRODB")]] == ["prodb-ctf"]
    assert [
        (adset.adset_id, adset.status) for adset in groups[("CityA", "L2")]
    ] == [("ver2", "PAUSED"), ("ver3", "ACTIVE")]
    assert [adset.adset_id for adset in groups[("CityA", "L1")]] == ["l1-1"]


def test_production_adset_directory_rejects_invalid_rows() -> None:
    from services.coverage_guard import FacebookAdsetDirectory

    with patch(
        "services.approval_source_facebook._paginate",
        MagicMock(return_value=({"id": "no-status", "name": "Owner | L2 | CityA"},)),
    ):
        with pytest.raises(Exception):
            FacebookAdsetDirectory().list_group_adsets("123")


# ---------------------------------------------------------------------------
# Дублирование алерта в ленту уведомлений с правильным уровнем
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_level"),
    [
        ("❗ Критическое покрытие\nCityA/L2: 0 effective ACTIVE", "critical"),
        ("⚠️ Тонкое покрытие\nCityA/L2: 1 из минимум 2", "warning"),
        ("✅ Покрытие восстановлено\nCityA/L2: 3 effective ACTIVE", "info"),
    ],
)
def test_alert_sender_mirrors_level_into_notification_feed(
    text: str,
    expected_level: str,
) -> None:
    sender = CoverageAlertTelegramSender(bot_token="token-x")
    response = MagicMock()
    response.json.return_value = {"ok": True, "result": {"message_id": 77}}
    add_event = MagicMock()

    with (
        patch("requests.post", MagicMock(return_value=response)),
        patch("services.notifications.add_event", add_event),
    ):
        message_id = sender.send_message(CHAT_ID, text)

    assert message_id == 77
    assert add_event.call_args.kwargs["level"] == expected_level


def test_alert_sender_rejects_response_without_message_id() -> None:
    sender = CoverageAlertTelegramSender(bot_token="token-x")
    response = MagicMock()
    response.json.return_value = {"ok": True, "result": {}}

    with patch("requests.post", MagicMock(return_value=response)):
        with pytest.raises(Exception):
            sender.send_message(CHAT_ID, "❗ Критическое покрытие")


# ---------------------------------------------------------------------------
# Регистрация крона
# ---------------------------------------------------------------------------


def test_cron_coverage_guard_reports_success_on_complete_inventory() -> None:
    from web import app as web_app

    summary = {"ok": True, "zero": [], "thin": [], "unknown": [], "ok_count": 10}
    success = MagicMock()
    failure = MagicMock()

    with (
        patch("services.coverage_guard.run_coverage_guard", return_value=summary),
        patch.object(web_app, "report_cron_success", success),
        patch.object(web_app, "report_cron_failure", failure),
    ):
        web_app._cron_coverage_guard()

    success.assert_called_once_with("_cron_coverage_guard")
    failure.assert_not_called()


def test_cron_coverage_guard_reports_failure_on_incomplete_inventory() -> None:
    from web import app as web_app

    summary = {"ok": False, "zero": [], "thin": [], "unknown": ["CityA/L2"]}
    success = MagicMock()
    failure = MagicMock()

    with (
        patch("services.coverage_guard.run_coverage_guard", return_value=summary),
        patch.object(web_app, "report_cron_success", success),
        patch.object(web_app, "report_cron_failure", failure),
    ):
        web_app._cron_coverage_guard()

    # Неполный инвентарь не должен выглядеть здоровым прогоном.
    success.assert_not_called()
    assert failure.call_count == 1
    assert failure.call_args.args[0] == "_cron_coverage_guard"


def test_cron_coverage_guard_swallows_exception_and_reports_failure() -> None:
    from web import app as web_app

    failure = MagicMock()

    with (
        patch(
            "services.coverage_guard.run_coverage_guard",
            side_effect=RuntimeError("fb down"),
        ),
        patch.object(web_app, "report_cron_failure", failure),
    ):
        web_app._cron_coverage_guard()

    assert failure.call_count == 1


def test_coverage_guard_module_keeps_the_no_mutation_import_boundary() -> None:
    """Боевая обвязка стража тоже не должна дотягиваться до мутаторов.

    Продолжение границы из tests/test_live_coverage_monitor.py на новый модуль:
    услуга покрытия читает FB только через строго read-only источник.
    """
    import ast

    forbidden_fragments = {
        "action_gateway",
        "executor",
        "integrations.facebook",
        "owner_action",
        "proposal",
    }
    module_path = (
        Path(__file__).resolve().parent.parent / "services" / "coverage_guard.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported: set[str] = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )

    assert not {
        module
        for module in imported
        if any(fragment in module for fragment in forbidden_fragments)
    }


def test_cron_coverage_guard_is_registered_every_30_minutes() -> None:
    """Крон должен быть заведён в lifespan с интервалом 30 минут."""
    import inspect

    from web import app as web_app

    source = inspect.getsource(web_app.lifespan)

    assert 'scheduler.add_job(_cron_coverage_guard, "interval", minutes=30)' in source
    # Утренний отчёт не сломан — остаётся отдельным кроном.
    assert 'scheduler.add_job(_cron_coverage_report, "interval", minutes=15)' in source
