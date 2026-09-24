"""Read-only recovery audit: cutoff, exact matching и durable idempotency."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from services import creative_intelligence as ci
from services import launch_recovery as recovery


LOCAL_TZ = timezone(timedelta(hours=5))


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    ci.DB_PATH = None
    db_path = str(tmp_path / "recovery.db")
    ci.init_kb(db_path)
    yield db_path
    ci.DB_PATH = None


def _action(index: int, completed_at: datetime) -> dict:
    return {
        "id": f"action-{index}",
        "type": "updateCard",
        "date": completed_at.isoformat(),
        "data": {
            "old": {"dueComplete": False},
            "card": {
                "id": f"card-{index}",
                "name": f"Креатив {index}",
                "dueComplete": True,
            },
            "board": {"id": "board-test"},
        },
    }


def _completion(completed_at: datetime | None = None) -> recovery.TrelloCompletion:
    return recovery.TrelloCompletion(
        action_id="action-1",
        board_id="board-test",
        card_id="card-1",
        card_name="Креатив",
        completed_at=completed_at or datetime(2026, 7, 10, 12, tzinfo=LOCAL_TZ),
    )


def _manifest(*names: str) -> recovery.MediaManifest:
    return recovery.MediaManifest(
        manifest_sha256="a" * 64,
        files=(recovery.MediaFileEvidence("video.mp4", 3, "b" * 64),),
        expected_names_by_city={"CityA": tuple(names)},
    )


def _live_adsets(
    *,
    offline_account: str = "account-1",
    offline_adset: str = "adset-1",
) -> dict[str, recovery.LiveAdsetDiscovery]:
    return {
        "offline": recovery.LiveAdsetDiscovery(
            account_kind="offline",
            account_id=offline_account,
            leadgen={"CityA": {"L2": offline_adset, "L1": "offline-l1"}},
            mql={"CityA": "offline-mql"},
        ),
        "online": recovery.LiveAdsetDiscovery(
            account_kind="online",
            account_id="online-account",
            leadgen={"Онлайн": {"L2": "online-l2", "L1": "online-l1"}},
            mql={},
        ),
    }


def _inventory_ad(
    ad_id: str,
    name: str,
    *,
    created_at: datetime | None = None,
    account_kind: str = "offline",
    account_id: str = "account-1",
    adset_id: str = "adset-1",
) -> dict:
    return {
        "id": ad_id,
        "name": name,
        "created_time": (
            created_at or datetime(2026, 7, 10, 12, tzinfo=LOCAL_TZ)
        ).isoformat(),
        "account_kind": account_kind,
        "account_id": account_id,
        "adset_id": adset_id,
    }


def _build_plans(
    monkeypatch,
    names: tuple[str, ...],
    inventory: dict[str, list[dict]],
) -> tuple[recovery.RecoveryCityPlan, ...]:
    monkeypatch.setattr(
        recovery,
        "_resolve_city_scope",
        lambda _card, _city: ("offline", "account-1", "adset-1"),
    )
    return recovery.build_recovery_city_plans(
        _completion(),
        _manifest(*names),
        inventory,
        datetime(2026, 7, 11, 12, tzinfo=LOCAL_TZ),
        card={"id": "card-1", "name": "Креатив", "campaign_type": "leadgen"},
    )


def test_scan_accepts_exact_cutoff_and_all_paginated_input(monkeypatch):
    from integrations import trello

    actions = [
        _action(index, recovery.RECOVERY_CUTOFF + timedelta(minutes=index))
        for index in range(1001)
    ]
    actions.append(_action(2000, recovery.RECOVERY_CUTOFF - timedelta(seconds=1)))
    calls: list[tuple[str, datetime, int]] = []

    def fake_actions(board_id, since, *, page_size, before=None):
        assert before is None
        calls.append((board_id, since, page_size))
        return actions

    monkeypatch.setattr(trello, "get_board_update_card_actions", fake_actions)
    monkeypatch.setattr(trello, "TRELLO_BOARD_ID", "unused", raising=False)
    monkeypatch.setattr("config.TRELLO_BOARD_ID", "board-test")

    completions = recovery.scan_completed_cards_since(recovery.RECOVERY_CUTOFF)

    assert len(completions) == 1001
    assert completions[0].completed_at == recovery.RECOVERY_CUTOFF
    assert calls == [("board-test", recovery.RECOVERY_CUTOFF, 1000)]


def test_scan_rejects_naive_or_before_fixed_cutoff():
    with pytest.raises(ValueError, match="timezone-aware"):
        recovery.scan_completed_cards_since(datetime(2026, 7, 1))
    with pytest.raises(ValueError, match="RECOVERY_CUTOFF"):
        recovery.scan_completed_cards_since(
            recovery.RECOVERY_CUTOFF - timedelta(microseconds=1)
        )


def test_recovery_fetches_complete_account_inventory_contract(monkeypatch):
    from integrations import facebook

    first = MagicMock(status_code=200)
    first.json.return_value = {
        "data": [
            {
                "id": "ad-1",
                "name": "CityA | Креатив [PRODA]",
                "account_id": "123",
                "adset_id": "456",
                "created_time": "2026-07-10T10:00:00+0000",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
            }
        ],
        "paging": {
            "next": "https://graph.example/next",
            "cursors": {"after": "cursor-1"},
        },
    }
    final = MagicMock(status_code=200)
    final.json.return_value = {"data": []}
    calls = []

    monkeypatch.setattr(recovery, "_resolve_account_id", lambda _kind: "123")
    monkeypatch.setattr(
        "services.fb_token_provider.fb_account",
        lambda _kind=None: nullcontext(),
    )
    monkeypatch.setattr(facebook, "get_fb_account_id", lambda: "act_123")
    monkeypatch.setattr(facebook, "get_fb_token", lambda: "test-token")

    def fake_get(url, *, params):
        calls.append((url, dict(params)))
        return first if len(calls) == 1 else final

    monkeypatch.setattr(facebook, "_throttled_get", fake_get)

    inventory = recovery.fetch_complete_launch_inventory(("offline",))

    assert [row["id"] for row in inventory["offline"]] == ["ad-1"]
    assert inventory["offline"][0]["account_kind"] == "offline"
    assert inventory["offline"][0]["account_id"] == "123"
    assert inventory["offline"][0]["inventory_complete"] is True
    assert calls[0][1].get("after") is None
    assert calls[1][1]["after"] == "cursor-1"


def test_recovery_adset_discovery_force_refreshes_both_accounts(monkeypatch):
    from agent import adset_discovery

    payloads = iter(
        (
            {
                "source": "fb_api",
                "leadgen": {"CityA": {"L2": "offline-l2"}},
                "mql": {"CityA": "offline-mql"},
            },
            {
                "source": "fb_api",
                "leadgen": {"Онлайн": {"L2": "online-l2"}},
                "mql": {},
            },
        )
    )
    calls: list[bool] = []

    def discover(*, force_refresh=False):
        calls.append(force_refresh)
        return next(payloads)

    monkeypatch.setattr(adset_discovery, "discover_adsets", discover)
    monkeypatch.setattr(
        "services.fb_token_provider.fb_account",
        lambda _kind=None: nullcontext(),
    )
    monkeypatch.setattr(
        recovery,
        "_resolve_account_id",
        lambda kind: "offline-account" if kind == "offline" else "online-account",
    )

    discovered = recovery.discover_live_recovery_adsets(("offline", "online"))

    assert calls == [True, True]
    assert discovered["offline"].account_id == "offline-account"
    assert discovered["online"].leadgen["Онлайн"]["L2"] == "online-l2"


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "cache", "leadgen": {"CityA": {"L2": "1"}}, "mql": {}},
        {"source": "fallback", "leadgen": {"CityA": {"L2": "1"}}, "mql": {}},
        {"source": "fb_api", "leadgen": {}, "mql": {}},
        {
            "source": "fb_api",
            "leadgen": {"CityA": {"L2": "same", "L1": "same"}},
            "mql": {},
        },
    ],
    ids=["cache", "fallback", "empty", "ambiguous"],
)
def test_recovery_adset_discovery_rejects_non_live_or_ambiguous(monkeypatch, payload):
    from agent import adset_discovery

    monkeypatch.setattr(
        "services.fb_token_provider.fb_account",
        lambda _kind=None: nullcontext(),
    )
    monkeypatch.setattr(recovery, "_resolve_account_id", lambda _kind: "123")
    monkeypatch.setattr(adset_discovery, "discover_adsets", lambda **_kwargs: payload)

    with pytest.raises(recovery.RecoveryReviewRequired):
        recovery.discover_live_recovery_adsets(("offline",))


def test_manifest_hash_is_deterministic_for_different_enumeration(
    tmp_path, monkeypatch
):
    first = tmp_path / "b.mp4"
    second = tmp_path / "a.mp4"
    first.write_bytes(b"bbb")
    second.write_bytes(b"aaa")
    card = {
        "id": "card-1",
        "name": "Креатив",
        "desc": "",
        "labels": ["PRODA"],
        "cities": ["CityA"],
    }
    monkeypatch.setattr(
        recovery,
        "_download_media_for_card",
        lambda _card: {"type": "video", "paths": [str(first), str(second)]},
    )
    first_manifest = recovery.build_media_manifest(card)
    monkeypatch.setattr(
        recovery,
        "_download_media_for_card",
        lambda _card: {"type": "video", "paths": [str(second), str(first)]},
    )

    second_manifest = recovery.build_media_manifest(card)

    assert first_manifest == second_manifest
    assert [file.relative_name for file in first_manifest.files] == ["a.mp4", "b.mp4"]
    assert first_manifest.expected_names_by_city["CityA"] == (
        "CityA | Креатив / a [PRODA]",
        "CityA | Креатив / b [PRODA]",
    )


def test_city_plan_persists_exact_scope_names_count_and_inclusive_window(monkeypatch):
    completion = _completion()
    audited_at = datetime(2026, 7, 11, 12, tzinfo=LOCAL_TZ)
    monkeypatch.setattr(
        recovery,
        "_resolve_city_scope",
        lambda _card, city: (
            "online" if city == "Онлайн" else "offline",
            f"account-{city}",
            f"adset-{city}",
        ),
    )
    manifest = recovery.MediaManifest(
        manifest_sha256="c" * 64,
        files=(recovery.MediaFileEvidence("video.mp4", 3, "d" * 64),),
        expected_names_by_city={
            "CityA": ("CityA | Креатив [PRODA]",),
            "Онлайн": ("Онлайн | Креатив [PRODA]",),
        },
    )

    plans = recovery.build_recovery_city_plans(
        completion,
        manifest,
        {"offline": [], "online": []},
        audited_at,
        card={"id": "card-1", "campaign_type": "leadgen"},
    )

    assert len(plans) == 2
    assert {plan.account_kind for plan in plans} == {"offline", "online"}
    assert all(plan.account_id == f"account-{plan.city}" for plan in plans)
    assert all(plan.adset_id == f"adset-{plan.city}" for plan in plans)
    assert all(
        plan.expected_ad_count == len(plan.expected_ad_names) == 1 for plan in plans
    )
    assert all(
        plan.reconcile_from == completion.completed_at - timedelta(hours=24)
        for plan in plans
    )
    assert all(plan.reconcile_until == audited_at for plan in plans)
    assert all(plan.phase == "MISSING" for plan in plans)


@pytest.mark.parametrize(
    "inventory",
    [
        [_inventory_ad("ad-1", "CityA | Креатив [PRODA]")],
        [
            _inventory_ad("ad-1", "CityA | Креатив [PRODA]"),
            _inventory_ad("ad-2", "CityA | Креатив [PRODA]"),
        ],
    ],
    ids=["partial", "duplicate"],
)
def test_partial_or_duplicate_city_is_review_required(monkeypatch, inventory):
    plans = _build_plans(
        monkeypatch,
        ("CityA | Креатив [PRODA]", "CityA | Креатив / 2 [PRODA]"),
        {"offline": inventory},
    )

    case = recovery.classify_recovery_case(_completion(), _manifest("x"), plans)

    assert plans[0].phase == "REVIEW_REQUIRED"
    assert case.phase == "REVIEW_REQUIRED"


@pytest.mark.parametrize(
    "ad",
    [
        _inventory_ad(
            "ad-old",
            "CityA | Креатив [PRODA]",
            created_at=datetime(2026, 7, 9, 11, 59, 59, tzinfo=LOCAL_TZ),
        ),
        _inventory_ad(
            "ad-wrong-account",
            "CityA | Креатив [PRODA]",
            account_id="account-2",
        ),
        _inventory_ad(
            "ad-wrong-adset",
            "CityA | Креатив [PRODA]",
            adset_id="adset-2",
        ),
    ],
    ids=["outside-window", "wrong-account", "wrong-adset"],
)
def test_outside_window_or_wrong_scope_is_review_required(monkeypatch, ad):
    plan = _build_plans(
        monkeypatch,
        ("CityA | Креатив [PRODA]",),
        {"offline": [ad]},
    )[0]

    assert plan.phase == "REVIEW_REQUIRED"
    assert plan.found_ad_ids == ()
    assert plan.evidence["ambiguous_ads"]


def test_full_exact_batch_is_complete_and_entire_absence_is_missing(monkeypatch):
    names = ("CityA | Креатив [PRODA]", "CityA | Креатив / 2 [PRODA]")
    exact = [
        _inventory_ad("ad-1", names[0]),
        _inventory_ad("ad-2", names[1]),
    ]

    complete = _build_plans(monkeypatch, names, {"offline": exact})[0]
    missing = _build_plans(monkeypatch, names, {"offline": []})[0]

    assert complete.phase == "COMPLETE"
    assert complete.found_ad_ids == ("ad-1", "ad-2")
    assert missing.phase == "MISSING"
    assert missing.found_ad_ids == ()


def test_duplicate_or_empty_expected_names_never_become_missing(monkeypatch):
    duplicate = _build_plans(
        monkeypatch,
        ("CityA | Креатив [PRODA]", "CityA | Креатив"),
        {"offline": []},
    )[0]
    empty = _build_plans(monkeypatch, ("",), {"offline": []})[0]

    assert duplicate.phase == "REVIEW_REQUIRED"
    assert empty.phase == "REVIEW_REQUIRED"


def test_missing_batch_without_reserve_waits_for_slot_and_never_calls_cleaner(
    monkeypatch,
):
    from integrations import facebook

    inventory = {
        "offline": [
            _inventory_ad(f"other-{index}", f"Другое объявление {index}")
            for index in range(facebook.MAX_ADS_PER_ADSET)
        ]
    }
    cleaner_calls: list[str] = []
    monkeypatch.setattr(
        facebook,
        "cleanup_stale_ads",
        lambda *_args, **_kwargs: cleaner_calls.append("delete"),
    )

    plan = _build_plans(
        monkeypatch,
        ("CityA | Креатив [PRODA]",),
        inventory,
    )[0]
    case = recovery.classify_recovery_case(_completion(), _manifest("x"), (plan,))

    assert plan.phase == "WAITING_SLOT"
    assert plan.capacity_available == 0
    assert case.phase == "WAITING_SLOT"
    assert cleaner_calls == []


def test_audit_is_durable_idempotent_and_calls_zero_mutations(
    isolated_db,
    monkeypatch,
):
    from integrations import facebook, trello

    completion = _completion()
    manifest = _manifest("CityA | Креатив [PRODA]")
    card = {
        "id": completion.card_id,
        "name": completion.card_name,
        "desc": "",
        "dueComplete": True,
        "closed": False,
        "labels": ["PRODA"],
        "campaign_type": "leadgen",
    }
    mutations: list[str] = []
    monkeypatch.setattr(
        recovery, "scan_completed_cards_since", lambda _since: [completion]
    )
    monkeypatch.setattr(recovery, "build_media_manifest", lambda _card: manifest)
    monkeypatch.setattr(
        recovery,
        "discover_live_recovery_adsets",
        lambda _kinds: _live_adsets(),
    )
    monkeypatch.setattr(
        recovery, "fetch_complete_launch_inventory", lambda _kinds: {"offline": []}
    )
    monkeypatch.setattr(
        recovery,
        "_resolve_city_scope",
        lambda _card, _city, *_live: ("offline", "account-1", "adset-1"),
    )
    monkeypatch.setattr(trello, "get_card", lambda _card_id: card)
    monkeypatch.setattr(
        trello, "mark_card_done", lambda *_a, **_kw: mutations.append("trello")
    )
    monkeypatch.setattr(
        facebook,
        "cleanup_stale_ads",
        lambda *_a, **_kw: mutations.append("delete"),
    )
    monkeypatch.setattr(
        facebook,
        "launch_creative",
        lambda *_a, **_kw: mutations.append("create"),
    )

    first = recovery.audit_missing_launches()
    second = recovery.audit_missing_launches()

    conn = sqlite3.connect(isolated_db)
    try:
        case_count = conn.execute(
            "SELECT COUNT(*) FROM launch_recovery_cases"
        ).fetchone()[0]
        plan_count = conn.execute(
            "SELECT COUNT(*) FROM launch_recovery_city_plans"
        ).fetchone()[0]
        stored = conn.execute(
            """
            SELECT account_kind, account_id, adset_id, expected_ad_names_json,
                   expected_ad_count, reconcile_from, reconcile_until, phase
            FROM launch_recovery_city_plans
            """
        ).fetchone()
    finally:
        conn.close()

    assert first.discovered_cards == second.discovered_cards == 1
    assert first.missing_city_plans == 1
    assert case_count == plan_count == 1
    assert stored[:3] == ("offline", "account-1", "adset-1")
    assert stored[4] == 1
    assert stored[7] == "MISSING"
    assert datetime.fromisoformat(stored[5]).tzinfo is not None
    assert datetime.fromisoformat(stored[6]).tzinfo is not None
    assert mutations == []


def test_audit_ambiguous_inventory_persists_review_and_zero_mutations(
    monkeypatch,
):
    from integrations import facebook, trello

    completion = _completion()
    card = {
        "id": completion.card_id,
        "name": completion.card_name,
        "desc": "",
        "dueComplete": True,
        "closed": False,
        "labels": ["PRODA"],
        "campaign_type": "leadgen",
    }
    mutations: list[str] = []
    monkeypatch.setattr(
        recovery, "scan_completed_cards_since", lambda _since: [completion]
    )
    monkeypatch.setattr(
        recovery,
        "build_media_manifest",
        lambda _card: _manifest("CityA | Креатив [PRODA]"),
    )
    monkeypatch.setattr(
        recovery,
        "discover_live_recovery_adsets",
        lambda _kinds: _live_adsets(),
    )
    monkeypatch.setattr(
        recovery,
        "fetch_complete_launch_inventory",
        lambda _kinds: {
            "offline": [
                _inventory_ad("ad-1", "CityA | Креатив [PRODA]"),
                _inventory_ad("ad-2", "CityA | Креатив [PRODA]"),
            ]
        },
    )
    monkeypatch.setattr(
        recovery,
        "_resolve_city_scope",
        lambda _card, _city, *_live: ("offline", "account-1", "adset-1"),
    )
    monkeypatch.setattr(trello, "get_card", lambda _card_id: card)
    monkeypatch.setattr(
        trello, "mark_card_done", lambda *_a, **_kw: mutations.append("trello")
    )
    monkeypatch.setattr(
        facebook,
        "cleanup_stale_ads",
        lambda *_a, **_kw: mutations.append("delete"),
    )
    monkeypatch.setattr(
        facebook,
        "launch_creative",
        lambda *_a, **_kw: mutations.append("create"),
    )

    summary = recovery.audit_missing_launches()
    cases = recovery.get_recovery_status()

    assert summary.review_required_cases == 1
    assert cases[0].phase == "REVIEW_REQUIRED"
    assert cases[0].city_plans[0].phase == "REVIEW_REQUIRED"
    assert mutations == []


def test_each_completion_action_gets_one_case_when_card_was_recompleted(
    isolated_db,
    monkeypatch,
):
    from integrations import trello

    first = _completion(datetime(2026, 7, 10, 12, tzinfo=LOCAL_TZ))
    second = recovery.TrelloCompletion(
        action_id="action-2",
        board_id=first.board_id,
        card_id=first.card_id,
        card_name=first.card_name,
        completed_at=datetime(2026, 7, 11, 12, tzinfo=LOCAL_TZ),
    )
    card = {
        "id": first.card_id,
        "name": first.card_name,
        "desc": "",
        "dueComplete": True,
        "closed": False,
        "labels": ["PRODA"],
        "campaign_type": "leadgen",
    }
    monkeypatch.setattr(
        recovery,
        "scan_completed_cards_since",
        lambda _since: [first, second],
    )
    monkeypatch.setattr(
        recovery,
        "build_media_manifest",
        lambda _card: _manifest("CityA | Креатив [PRODA]"),
    )
    monkeypatch.setattr(
        recovery,
        "discover_live_recovery_adsets",
        lambda _kinds: _live_adsets(),
    )
    monkeypatch.setattr(
        recovery,
        "fetch_complete_launch_inventory",
        lambda _kinds: {"offline": [], "online": []},
    )
    monkeypatch.setattr(
        recovery,
        "_resolve_city_scope",
        lambda _card, _city, *_live: ("offline", "account-1", "adset-1"),
    )
    monkeypatch.setattr(trello, "get_card", lambda _card_id: card)

    summary = recovery.audit_missing_launches()

    conn = sqlite3.connect(isolated_db)
    try:
        rows = conn.execute(
            """
            SELECT trello_action_id, phase
            FROM launch_recovery_cases ORDER BY source_completed_at
            """
        ).fetchall()
        city_rows = conn.execute(
            """
            SELECT c.trello_action_id, p.phase
            FROM launch_recovery_city_plans AS p
            JOIN launch_recovery_cases AS c ON c.case_id = p.case_id
            ORDER BY c.source_completed_at, p.city
            """
        ).fetchall()
    finally:
        conn.close()

    assert summary.discovered_cards == 2
    assert rows == [("action-1", "DISCOVERED"), ("action-2", "REVIEW_REQUIRED")]
    assert city_rows == [("action-1", "MISSING"), ("action-2", "REVIEW_REQUIRED")]
