"""Сверка галочек Trello с живой рекламой — services/trello_check_reconciler.py."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from services import trello_check_reconciler as reconciler
from services.trello_check_reconciler import (
    LiveAd,
    BoardCard,
    ReconcileError,
    card_created_at,
    card_name_candidates,
    fetch_live_ads,
    fetch_board_cards,
    normalize_name,
    reconcile_trello_checks,
)


@pytest.fixture(autouse=True)
def _isolate_heartbeat_file(tmp_path, monkeypatch):
    """Крон обёрнут в @heartbeat — отметку пульса уводим в tmp, не в data/."""
    import services.cron_heartbeat as cron_hb

    monkeypatch.setattr(cron_hb, "_HB_FILE", tmp_path / "cron_heartbeats.json")


# --- Имена -------------------------------------------------------------------


def test_candidates_are_body_and_body_without_label_only() -> None:
    assert card_name_candidates(
        "CityA | Креатор Б / PRODB / Тема А 2 [PRODB]"
    ) == (
        "креатор б / prodb / тема а 2",
        "креатор б / prodb",
    )


def test_candidates_single_segment_body() -> None:
    assert card_name_candidates("CityA | Первая [PRODA]") == ("первая",)


def test_candidates_clone_with_asset_label_reaches_card_name_but_not_deeper() -> None:
    candidates = card_name_candidates("CityC | Павел ugc / PRODB / 3 видео / 3 ролик [PRODB]")
    assert "павел ugc / prodb / 3 видео" in candidates
    assert "павел ugc / prodb" not in candidates
    assert "павел ugc" not in candidates


def test_card_created_at_from_object_id_and_unknown_ids() -> None:
    # 0x68a00000 = 1755316224 = 2025-08-16T03:50:24Z
    assert card_created_at("68a000000000000000000001") == datetime(2025, 8, 16, 3, 50, 24, tzinfo=timezone.utc)
    assert card_created_at("card-1") is None
    assert card_created_at("") is None


def test_candidates_free_naming_keeps_inner_separator() -> None:
    """Свободные имена разовых скриптов не режутся до карточки — так и задумано."""
    assert card_name_candidates("intl_line | Продукт Б без цены | В1") == (
        "продукт б без цены | в1",
    )


def test_candidates_without_city_prefix_and_empty() -> None:
    assert card_name_candidates("тема 4 без города") == ("тема 4 без города",)
    assert card_name_candidates("") == ()
    assert card_name_candidates("   ") == ()


def test_normalize_name_collapses_case_spaces_and_yo() -> None:
    assert normalize_name("  Ёлка   / Тест ") == "елка / тест"
    assert normalize_name("Ёлка / тест") == normalize_name("ёлка /  ТЕСТ")


# --- Сверка ------------------------------------------------------------------


def _card(
    card_id: str,
    name: str,
    *,
    list_name: str = "Готово",
    due: bool = False,
    closed: bool = False,
    list_closed: bool = False,
    created: datetime | None = None,
) -> BoardCard:
    return BoardCard(
        card_id=card_id, name=name, list_name=list_name, due_complete=due,
        closed=closed, list_closed=list_closed, created_at=created,
    )


def _ad(ad_id: str, name: str, account_id: str = "1", created: datetime | None = None) -> LiveAd:
    return LiveAd(ad_id=ad_id, name=name, account_id=account_id, created_at=created)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_marks_unchecked_card_with_live_ads_and_reports_ads() -> None:
    cards = [
        _card("c1", "Максим / Тема А l2"),
        _card("c2", "Игорь / Тема Б / Подтема 1"),
    ]
    ads = [
        _ad("a1", "CityA | Максим / Тема А l2 [PRODA]"),
        _ad("a2", "CityC | Максим / Тема А l2 [PRODA]", account_id="2"),
    ]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == ["c1"]
    assert run.marked == ("c1",)
    assert run.failed == ()
    assert [m.card.card_id for m in run.to_mark] == ["c1"]
    assert run.to_mark[0].ad_ids == ("a1", "a2")
    assert run.unchecked_without_ads == 1
    assert run.unmatched_ads == ()


def test_card_waiting_for_resume_is_not_checked(monkeypatch) -> None:
    """Запущены не все города — галочки нет, иначе карточка выпадет из дозапуска."""
    import services.auto_launch as auto_launch

    monkeypatch.setattr(auto_launch, "resumable_card_ids", lambda state=None, now=None: {"c1"})
    cards = [_card("c1", "Максим / Тема А l2")]
    ads = [_ad("a1", "CityA | Максим / Тема А l2 [PRODA]")]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == [] and run.marked == ()


def test_checked_card_is_not_touched_and_ad_is_not_unmatched() -> None:
    cards = [_card("c1", "Павел ugc / PRODB / 1 видео", due=True)]
    ads = [_ad("a1", "CityE | Павел ugc / PRODB / 1 видео / 1 ролик готовый [PRODB]")]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == []
    assert run.to_mark == () and run.needs_review == ()
    assert run.unmatched_ads == ()
    assert run.unchecked_without_ads == 0


def test_multi_asset_card_is_marked_via_truncated_candidate() -> None:
    """Мультиассетный запуск: «Карточка / метка1», «Карточка / метка2» — ≥2 меток,
    усечённое совпадение достаточно доказано, галочка ставится."""
    cards = [_card("c1", "Павел ugc / PRODB / 3 видео")]
    ads = [
        _ad("a1", "CityB | Павел ugc / PRODB / 3 видео / 2 ролик [PRODB]"),
        _ad("a2", "CityB | Павел ugc / PRODB / 3 видео / 3 ролик [PRODB]"),
        _ad("a3", "CityA | Павел ugc / PRODB / 3 видео / 2 ролик [PRODB]"),
    ]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == ["c1"]
    assert run.to_mark[0].ad_ids == ("a1", "a2", "a3")
    assert run.needs_review == ()


def test_single_label_truncated_match_needs_review_not_mark() -> None:
    """Одна метка через усечение (клон «1 видео / 1 ролик готовый» по нескольким городам)
    — слабое доказательство: в needs_review, галочку ставит человек."""
    cards = [_card("c1", "Павел ugc / PRODB / 1 видео")]
    ads = [
        _ad("a1", "CityE | Павел ugc / PRODB / 1 видео / 1 ролик готовый [PRODB]"),
        _ad("a2", "CityB | Павел ugc / PRODB / 1 видео / 1 ролик готовый [PRODB]"),
    ]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == []
    assert [m.card.card_id for m in run.needs_review] == ["c1"]
    assert run.needs_review[0].weak is True
    assert run.unchecked_without_ads == 0
    assert run.unmatched_ads == ()


def test_exact_match_plus_truncated_is_strong() -> None:
    cards = [_card("c1", "Первая")]
    ads = [_ad("a1", "CityA | Первая"), _ad("a2", "CityB | Первая / доп ролик")]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == ["c1"]
    assert run.to_mark[0].weak is False


def test_renamed_source_with_open_prefix_card_goes_to_review_not_mark() -> None:
    """Точной карточки нет нигде (переименована), рядом открыта карточка-префикс:
    одиночное объявление к ней не ставит галочку — только на проверку."""
    cards = [_card("c_blogger", "Максим")]
    ads = [_ad("a1", "CityA | Максим / Тема А l2 [PRODA]")]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == []
    assert [m.card.card_id for m in run.needs_review] == ["c_blogger"]


def test_date_filtered_exact_group_falls_through_to_next_candidate() -> None:
    """Точная карточка моложе объявления (пересоздана), а старая карточка-база
    открыта: второй кандидат пробуется, а не отбрасывается."""
    cards = [
        _card("c_new_exact", "Павел ugc / PRODB / 3 ролик", created=_utc(2026, 9, 1)),
        _card("c_base", "Павел ugc / PRODB", created=_utc(2026, 8, 1)),
    ]
    ads = [_ad("a1", "CityB | Павел ugc / PRODB / 3 ролик [PRODB]", created=_utc(2026, 8, 2))]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == []
    assert run.unmatched_ads == ()
    assert [m.card.card_id for m in run.needs_review] == ["c_base"]
    assert run.unchecked_without_ads == 1


def test_renamed_card_leaves_clone_ads_unmatched() -> None:
    """Карточку «3 видео» переименовали в «2 видео», объявления остались
    со старым именем — точное правило их не сопоставляет и не ставит галочку."""
    cards = [_card("c1", "Павел ugc / PRODB / 2 видео")]
    ads = [_ad("a1", "CityB | Павел ugc / PRODB / 3 видео / 2 ролик [PRODB]")]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == []
    assert [ad.ad_id for ad in run.unmatched_ads] == ["a1"]
    assert run.unchecked_without_ads == 1


def test_free_named_ads_stay_unmatched_even_with_similar_card() -> None:
    cards = [_card("c1", "Вторая линейка / Спикер Б / Продукт Б 4 видео", list_name="Вторая линейка")]
    ads = [_ad("a1", "intl_line | Продукт Б без цены | В1")]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == []
    assert len(run.unmatched_ads) == 1


def test_ambiguous_card_name_is_not_marked() -> None:
    cards = [
        _card("c1", "Денис / Тема А"),
        _card("c2", "Денис / Тема А", list_name="Вторая линейка"),
    ]
    ads = [_ad("a1", "CityA | Денис / Тема А [PRODA]")]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == []
    assert sorted(m.card.card_id for m in run.ambiguous) == ["c1", "c2"]
    assert run.to_mark == ()


def test_card_outside_target_lists_is_reported_not_marked() -> None:
    cards = [_card("c1", "Виктор / Тема Б / Подтема 1", list_name="В работе")]
    ads = [_ad("a1", "CityA | Виктор / Тема Б / Подтема 1 [PRODA]")]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == []
    assert [m.card.card_id for m in run.outside_lists] == ["c1"]
    assert run.unchecked_without_ads == 0


def test_shorter_prefix_card_is_not_marked_by_deeper_ad_name() -> None:
    """Источник «… / 3 видео» архивирован, рядом открыта короткая «Павел ugc / PRODB»:
    объявление с двумя хвостами к ней не режется — остаётся без карточки."""
    cards = [_card("c_old", "Павел ugc / PRODB")]
    ads = [_ad("a1", "CityB | Павел ugc / PRODB / 3 видео / 2 ролик [PRODB]")]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == []
    assert [ad.ad_id for ad in run.unmatched_ads] == ["a1"]


def test_archived_full_name_card_wins_over_open_prefix_card() -> None:
    """Источник объявления архивирован, рядом открыта карточка-префикс «Максим»:
    полное имя находит архивную карточку, к префиксу объявление не проваливается."""
    cards = [
        _card("c_blogger", "Максим"),
        _card("c_archived", "Максим / Тема А l2", due=True, closed=True),
    ]
    ads = [_ad("a1", "CityA | Максим / Тема А l2 [PRODA]")]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == []
    assert run.to_mark == ()
    assert run.unmatched_ads == ()
    assert run.unchecked_without_ads == 1


def test_archived_duplicate_name_makes_open_card_ambiguous() -> None:
    """Старая версия карточки архивирована, новая открыта под тем же именем и старше
    объявлений: ставить галочку новой нельзя — решает человек."""
    cards = [
        _card("c_old", "Игорь / Тема Б", due=True, closed=True, created=_utc(2026, 7, 22)),
        _card("c_new", "Игорь / Тема Б", created=_utc(2026, 7, 29)),
    ]
    ads = [_ad("a1", "CityA | Игорь / Тема Б [PRODA]", created=_utc(2026, 8, 18))]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == []
    assert [m.card.card_id for m in run.ambiguous] == ["c_new"]


def test_ad_older_than_card_does_not_belong_to_it() -> None:
    """Карточка пересоздана после запуска: старые объявления к новой не привязываются."""
    cards = [_card("c_new", "Первая", created=_utc(2026, 9, 3, 12, 0))]
    ads = [_ad("a_old", "CityA | Первая", created=_utc(2026, 8, 18, 6, 0))]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == []
    assert [ad.ad_id for ad in run.unmatched_ads] == ["a_old"]
    assert run.unchecked_without_ads == 1


def test_ad_newer_than_card_or_without_dates_belongs() -> None:
    cards = [
        _card("c1", "Первая", created=_utc(2026, 9, 1)),
        _card("c2", "Вторая", created=None),
        _card("c3", "Третья", created=_utc(2026, 9, 1)),
    ]
    ads = [
        _ad("a1", "CityA | Первая", created=_utc(2026, 9, 2)),
        _ad("a2", "CityA | Вторая", created=_utc(2026, 1, 1)),
        _ad("a3", "CityA | Третья", created=None),
    ]
    marked: list[str] = []

    reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert sorted(marked) == ["c1", "c2", "c3"]


def test_card_in_archived_list_is_outside_not_marked() -> None:
    cards = [_card("c1", "Первая", list_name="Готово", list_closed=True)]
    ads = [_ad("a1", "CityA | Первая")]
    marked: list[str] = []

    run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=marked.append)

    assert marked == []
    assert [m.card.card_id for m in run.outside_lists] == ["c1"]
    assert run.unchecked_without_ads == 0
    assert run.unmatched_ads == ()


def test_custom_list_names_widen_target() -> None:
    cards = [_card("c1", "Виктор / Тест", list_name="В работе")]
    ads = [_ad("a1", "CityA | Виктор / Тест [PRODA]")]
    marked: list[str] = []

    run = reconcile_trello_checks(
        cards=cards, ads=ads, list_names=("Готово", "В работе"), mark_card_done=marked.append
    )

    assert marked == ["c1"]
    assert run.outside_lists == ()


def test_mark_failure_is_recorded_and_does_not_block_others(caplog) -> None:
    cards = [_card("c1", "Первая"), _card("c2", "Вторая")]
    ads = [_ad("a1", "CityA | Первая"), _ad("a2", "CityA | Вторая")]

    def mark(card_id: str) -> None:
        if card_id == "c1":
            raise RuntimeError("boom for url https://api.trello.com/1/cards/c1?key=K&token=T")

    with caplog.at_level("WARNING"):
        run = reconcile_trello_checks(cards=cards, ads=ads, mark_card_done=mark)

    assert run.marked == ("c2",)
    assert run.failed == ("c1",)
    assert "token=***" in caplog.text
    assert "token=T" not in caplog.text


def test_dry_run_marks_nothing_but_reports_plan() -> None:
    cards = [_card("c1", "Первая")]
    ads = [_ad("a1", "CityA | Первая")]
    mark = MagicMock()

    run = reconcile_trello_checks(cards=cards, ads=ads, apply=False, mark_card_done=mark)

    mark.assert_not_called()
    assert [m.card.card_id for m in run.to_mark] == ["c1"]
    assert run.marked == ()


def test_run_carries_account_scope() -> None:
    run = reconcile_trello_checks(
        cards=[], ads=[], accounts_scanned=("1",), accounts_failed=("2",), mark_card_done=MagicMock()
    )
    assert run.accounts_scanned == ("1",)
    assert run.accounts_failed == ("2",)
    assert run.marked_count == 0


# --- Источник FB -------------------------------------------------------------


def _row(ad_id: str, name: str, status: str = "ACTIVE", effective: str = "ACTIVE", created: str | None = None) -> dict:
    row = {"id": ad_id, "name": name, "status": status, "effective_status": effective}
    if created is not None:
        row["created_time"] = created
    return row


def test_fetch_live_ads_keeps_only_double_active_rows() -> None:
    inventory = {
        "1": [
            _row("a1", "живое"),
            _row("a2", "архив", status="ARCHIVED"),
            _row("a3", "пауза адсета", effective="ADSET_PAUSED"),
            _row("a4", "выключено", status="PAUSED", effective="PAUSED"),
        ],
    }

    ads, scanned, failed = fetch_live_ads(
        accounts=lambda: ("act_1",), inventory=lambda account: inventory[account]
    )

    assert ads == (LiveAd(ad_id="a1", name="живое", account_id="1", created_at=None),)
    assert scanned == ("1",)
    assert failed == ()


def test_fetch_live_ads_parses_created_time_and_tolerates_bad_values() -> None:
    inventory = [
        _row("a1", "с датой", created="2026-09-03T07:29:00+0000"),
        _row("a2", "битая дата", created="вчера"),
        _row("a3", "наивная дата", created="2026-09-03T07:29:00"),
    ]

    ads, _scanned, _failed = fetch_live_ads(accounts=lambda: ("1",), inventory=lambda account: inventory)

    assert ads[0].created_at == _utc(2026, 9, 3, 7, 29)
    assert ads[1].created_at is None
    assert ads[2].created_at is None


def test_fetch_live_ads_tolerates_one_failed_account(caplog) -> None:
    def inventory(account: str) -> list[dict]:
        if account == "2":
            raise RuntimeError("fb down")
        return [_row("a1", "живое")]

    with caplog.at_level("WARNING"):
        ads, scanned, failed = fetch_live_ads(accounts=lambda: ("1", "2"), inventory=inventory)

    assert [ad.ad_id for ad in ads] == ["a1"]
    assert scanned == ("1",)
    assert failed == ("2",)
    assert "act_2" in caplog.text


def test_fetch_live_ads_rejects_bad_account_ids() -> None:
    with pytest.raises(ReconcileError, match="accounts_invalid"):
        fetch_live_ads(accounts=lambda: ("act_x",), inventory=lambda account: [])
    with pytest.raises(ReconcileError, match="accounts_invalid"):
        fetch_live_ads(accounts=lambda: (), inventory=lambda account: [])


def test_fetch_live_ads_skips_empty_names_and_fails_only_that_account() -> None:
    inventory = {
        "1": [
            {"id": "a0", "status": "ACTIVE", "effective_status": "ACTIVE"},
            _row("a1", "   "),
            _row("a2", "живое"),
        ],
        "2": [{"id": "", "name": "без id", "status": "ACTIVE", "effective_status": "ACTIVE"}],
        "3": ["not-a-row"],
    }

    ads, scanned, failed = fetch_live_ads(
        accounts=lambda: ("1", "2", "3"), inventory=lambda account: inventory[account]
    )

    assert [ad.ad_id for ad in ads] == ["a2"]
    assert scanned == ("1",)
    assert failed == ("2", "3")


# --- Источник Trello ---------------------------------------------------------


def _response(payload: object) -> MagicMock:
    response = MagicMock()
    response.json.return_value = payload
    return response


def test_fetch_board_cards_parses_lists_and_keeps_closed() -> None:
    lists = [
        {"id": "L1", "name": "Готово", "closed": False},
        {"id": "L2", "name": "Вторая линейка", "closed": False},
        {"id": "L3", "name": "Перезапуск", "closed": True},
    ]
    cards = [
        {"id": "c1", "name": "Открытая", "dueComplete": False, "closed": False, "idList": "L1"},
        {"id": "c2", "name": "Закрытая", "dueComplete": False, "closed": True, "idList": "L1"},
        {"id": "c3", "name": "Отмеченная", "dueComplete": True, "closed": False, "idList": "L2"},
        {"id": "68a000000000000000000001", "name": "В архивной колонке", "dueComplete": False, "closed": False, "idList": "L3"},
    ]
    with patch.object(reconciler, "safe_request", side_effect=[_response(cards), _response(lists)]) as request:
        result = fetch_board_cards(board_id="B")

    assert result == (
        BoardCard(card_id="c1", name="Открытая", list_name="Готово", due_complete=False),
        BoardCard(card_id="c2", name="Закрытая", list_name="Готово", due_complete=False, closed=True),
        BoardCard(card_id="c3", name="Отмеченная", list_name="Вторая линейка", due_complete=True),
        BoardCard(
            card_id="68a000000000000000000001", name="В архивной колонке", list_name="Перезапуск",
            due_complete=False, list_closed=True, created_at=_utc(2025, 8, 16, 3, 50, 24),
        ),
    )
    assert request.call_args_list[0].args[1].endswith("/boards/B/cards/all")
    assert request.call_args_list[0].kwargs["params"]["limit"] == reconciler._CARDS_PAGE_SIZE
    assert "before" not in request.call_args_list[0].kwargs["params"]
    assert request.call_args_list[1].args[1].endswith("/boards/B/lists")
    assert request.call_args_list[1].kwargs["params"]["filter"] == "all"


def _card_row(card_id: str, list_id: str = "L1") -> dict:
    return {"id": card_id, "name": f"Карточка {card_id}", "dueComplete": False, "closed": False, "idList": list_id}


def test_fetch_board_cards_paginates_with_before_cursor(monkeypatch) -> None:
    monkeypatch.setattr(reconciler, "_CARDS_PAGE_SIZE", 2)
    lists = [{"id": "L1", "name": "Готово", "closed": False}]
    page1 = [_card_row("0000000c"), _card_row("0000000a")]
    page2 = [_card_row("00000009"), _card_row("00000005")]
    page3 = [_card_row("00000003")]
    with patch.object(
        reconciler, "safe_request",
        side_effect=[_response(page1), _response(page2), _response(page3), _response(lists)],
    ) as request:
        result = fetch_board_cards(board_id="B")

    assert [card.card_id for card in result] == ["0000000c", "0000000a", "00000009", "00000005", "00000003"]
    befores = [call.kwargs["params"].get("before") for call in request.call_args_list[:3]]
    assert befores == [None, "0000000a", "00000005"]
    assert request.call_args_list[3].args[1].endswith("/boards/B/lists")


def test_fetch_board_cards_full_last_page_needs_empty_page(monkeypatch) -> None:
    monkeypatch.setattr(reconciler, "_CARDS_PAGE_SIZE", 2)
    lists = [{"id": "L1", "name": "Готово", "closed": False}]
    with patch.object(
        reconciler, "safe_request",
        side_effect=[_response([_card_row("0000000c"), _card_row("0000000a")]), _response([]), _response(lists)],
    ):
        result = fetch_board_cards(board_id="B")

    assert len(result) == 2


@pytest.mark.parametrize(
    ("pages", "reason"),
    [
        ([[_card_row("0000000c"), _card_row("0000000a")], [_card_row("0000000a")]], "cards_paging_duplicate"),
        ([[_card_row("0000000c"), _card_row("0000000c")]], "cards_paging_duplicate"),
        ([[_card_row("0000000c"), _card_row("0000000a")], [_card_row("0000000b"), _card_row("0000000f")]], "cards_paging_cursor_invalid"),
    ],
)
def test_fetch_board_cards_fails_closed_on_broken_paging(monkeypatch, pages, reason) -> None:
    monkeypatch.setattr(reconciler, "_CARDS_PAGE_SIZE", 2)
    with patch.object(reconciler, "safe_request", side_effect=[_response(page) for page in pages]):
        with pytest.raises(ReconcileError, match=reason):
            fetch_board_cards(board_id="B")


def test_fetch_board_cards_page_limit_exceeded(monkeypatch) -> None:
    monkeypatch.setattr(reconciler, "_CARDS_PAGE_SIZE", 1)
    monkeypatch.setattr(reconciler, "_MAX_CARD_PAGES", 2)
    with patch.object(
        reconciler, "safe_request",
        side_effect=[_response([_card_row("0000000c")]), _response([_card_row("0000000a")]), _response([_card_row("00000005")])],
    ):
        with pytest.raises(ReconcileError, match="cards_page_limit_exceeded"):
            fetch_board_cards(board_id="B")


@pytest.mark.parametrize(
    ("cards", "reason"),
    [
        ([{"id": "c1", "name": "x", "dueComplete": "yes", "closed": False, "idList": "L1"}], "card_flags_invalid"),
        ([{"id": "c1", "name": "x", "dueComplete": False, "closed": False, "idList": "L9"}], "card_list_unknown"),
        ([{"id": "", "name": "x", "dueComplete": False, "closed": False, "idList": "L1"}], "card_id_invalid"),
        (["not-a-card"], "card_not_object"),
        ({"data": []}, "cards_payload_not_list"),
    ],
)
def test_fetch_board_cards_fails_closed_on_bad_snapshot(cards, reason) -> None:
    lists = [{"id": "L1", "name": "Готово", "closed": False}]
    with patch.object(reconciler, "safe_request", side_effect=[_response(cards), _response(lists)]):
        with pytest.raises(ReconcileError, match=reason):
            fetch_board_cards(board_id="B")


def test_fetch_board_cards_fails_on_bad_lists() -> None:
    with patch.object(reconciler, "safe_request", side_effect=[_response([]), _response({"lists": []})]):
        with pytest.raises(ReconcileError, match="lists_payload_not_list"):
            fetch_board_cards(board_id="B")
    with patch.object(reconciler, "safe_request", side_effect=[_response([]), _response([{"id": "L1", "name": "Готово"}])]):
        with pytest.raises(ReconcileError, match="list_closed_invalid"):
            fetch_board_cards(board_id="B")


# --- Боевая обвязка ----------------------------------------------------------


def test_from_config_refuses_when_no_account_was_read() -> None:
    with (
        patch.object(reconciler, "fetch_board_cards", return_value=()),
        patch.object(reconciler, "fetch_live_ads", return_value=((), (), ("1",))),
    ):
        with pytest.raises(ReconcileError, match="no_account_inventory"):
            reconciler.reconcile_trello_checks_from_config()


def test_from_config_wires_sources_and_apply_flag() -> None:
    cards = (_card("c1", "Первая"),)
    ads = (_ad("a1", "CityA | Первая"),)
    with (
        patch.object(reconciler, "fetch_board_cards", return_value=cards),
        patch.object(reconciler, "fetch_live_ads", return_value=(ads, ("1",), ())),
        patch.object(reconciler, "_mark_card_done_default") as mark,
    ):
        run = reconciler.reconcile_trello_checks_from_config(apply=False)
        assert run.marked == () and [m.card.card_id for m in run.to_mark] == ["c1"]
        mark.assert_not_called()

        run = reconciler.reconcile_trello_checks_from_config()
        mark.assert_called_once_with("c1")
        assert run.marked == ("c1",)
        assert run.accounts_scanned == ("1",)


# --- Крон в web/app.py -------------------------------------------------------


def _run(**overrides):
    base = dict(
        to_mark=(), marked=(), failed=(), ambiguous=(), needs_review=(), outside_lists=(), unmatched_ads=(),
        unchecked_without_ads=0, accounts_scanned=("1",), accounts_failed=(),
    )
    base.update(overrides)
    return reconciler.ReconcileRun(**base)


def test_cron_reports_success_on_clean_run() -> None:
    import web.app as web_app

    success, failure = MagicMock(), MagicMock()
    with (
        patch.object(reconciler, "reconcile_trello_checks_from_config", return_value=_run(marked=("c1",))),
        patch.object(web_app, "report_cron_success", success),
        patch.object(web_app, "report_cron_failure", failure),
    ):
        web_app._cron_trello_check_reconcile()

    success.assert_called_once_with("_cron_trello_check_reconcile")
    failure.assert_not_called()


@pytest.mark.parametrize("overrides", [{"failed": ("c1",)}, {"accounts_failed": ("2",)}])
def test_cron_reports_failure_on_partial_run(overrides) -> None:
    import web.app as web_app

    success, failure = MagicMock(), MagicMock()
    with (
        patch.object(reconciler, "reconcile_trello_checks_from_config", return_value=_run(**overrides)),
        patch.object(web_app, "report_cron_success", success),
        patch.object(web_app, "report_cron_failure", failure),
    ):
        web_app._cron_trello_check_reconcile()

    success.assert_not_called()
    failure.assert_called_once()
    assert failure.call_args.args[0] == "_cron_trello_check_reconcile"


def test_cron_reports_failure_on_exception() -> None:
    import web.app as web_app

    success, failure = MagicMock(), MagicMock()
    with (
        patch.object(reconciler, "reconcile_trello_checks_from_config", side_effect=RuntimeError("trello down")),
        patch.object(web_app, "report_cron_success", success),
        patch.object(web_app, "report_cron_failure", failure),
    ):
        web_app._cron_trello_check_reconcile()

    success.assert_not_called()
    failure.assert_called_once()
