"""Мультикабинетный сбор FB-агрегатов когорт (cabinet_a + cabinet_b).

L2-адсеты нескольких городов переехали в кабинет «ACME cabinet_b»
(act_29716040622546856) — _collect_fb обязан ставить отчёт на каждый кабинет
из accounts_to_scan() и сливать строки в одну карту недель.
"""

from datetime import date

import pytest

from services import cohort_builder
from services.fb_token_provider import get_fb_account_id

CABINET_A = "152882611033373"
CABINET_B = "29716040622546856"


@pytest.fixture()
def two_account_rows(monkeypatch):
    """_fetch_fb_window отдаёт строки в зависимости от активного кабинета."""
    monkeypatch.setattr(
        cohort_builder, "_month_windows", lambda since, until: [(since, until)]
    )

    rows_by_account = {
        CABINET_A: [
            {
                "ad_id": "525_1", "date": date(2026, 8, 10), "spend": 100.0,
                "impressions": 1000, "leads": 10, "adset_id": "s1",
                "adset_name": "Owner | L1 | SO CityA", "ad_name": "m1",
            },
        ],
        CABINET_B: [
            {
                "ad_id": "120_1", "date": date(2026, 8, 11), "spend": 50.0,
                "impressions": 500, "leads": 5, "adset_id": "w1",
                "adset_name": "Owner | L2 | SO CityA", "ad_name": "w1",
            },
        ],
    }
    calls: list[str] = []

    def fake_window(since, until):
        account = get_fb_account_id()
        calls.append(account)
        return rows_by_account[account]

    monkeypatch.setattr(cohort_builder, "_fetch_fb_window", fake_window)
    monkeypatch.setattr(
        "services.launch_routing.accounts_to_scan", lambda: (CABINET_A, CABINET_B)
    )
    return calls


def test_collect_fb_merges_both_accounts(two_account_rows):
    weeks, covered = cohort_builder._collect_fb(date(2026, 8, 10), date(2026, 8, 16))

    assert two_account_rows == [CABINET_A, CABINET_B], "отчёт ставится на оба кабинета"
    week = cohort_builder.week_start_of(date(2026, 8, 10))
    assert ("525_1", week) in weeks and ("120_1", week) in weeks
    assert weeks[("120_1", week)].spend_usd == 50.0
    assert covered == {date(2026, 8, 10), date(2026, 8, 11)}


def test_collect_fb_fails_closed_when_second_account_breaks(monkeypatch):
    """Отказ cabinet_b роняет весь билд, а не пишет половину данных."""
    monkeypatch.setattr(
        cohort_builder, "_month_windows", lambda since, until: [(since, until)]
    )
    monkeypatch.setattr(
        "services.launch_routing.accounts_to_scan", lambda: (CABINET_A, CABINET_B)
    )

    def fake_window(since, until):
        if get_fb_account_id() == CABINET_B:
            raise cohort_builder.CohortBuildError("FB отчёт не получен")
        return []

    monkeypatch.setattr(cohort_builder, "_fetch_fb_window", fake_window)

    with pytest.raises(cohort_builder.CohortBuildError):
        cohort_builder._collect_fb(date(2026, 8, 10), date(2026, 8, 16))


def test_collect_fb_single_account_map_unchanged(monkeypatch):
    """Карта без cabinet_b (все города в cabinet_a) — один отчёт, без регрессии."""
    monkeypatch.setattr(
        cohort_builder, "_month_windows", lambda since, until: [(since, until)]
    )
    monkeypatch.setattr(
        "services.launch_routing.accounts_to_scan", lambda: (CABINET_A,)
    )
    calls: list[str] = []

    def fake_window(since, until):
        calls.append(get_fb_account_id())
        return []

    monkeypatch.setattr(cohort_builder, "_fetch_fb_window", fake_window)
    weeks, covered = cohort_builder._collect_fb(date(2026, 8, 10), date(2026, 8, 16))
    assert calls == [CABINET_A] and weeks == {} and covered == set()
