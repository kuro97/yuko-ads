"""Release A: live guard последнего ACTIVE-объявления."""

import json
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from unittest.mock import Mock

import pytest

from services import adset_pause_guard as guard


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


def _context(ad_id: str, adset_id: str = "100") -> guard.AdLiveContext:
    return {
        "ad_id": ad_id,
        "adset_id": adset_id,
        "configured_status": "ACTIVE",
        "effective_status": "ACTIVE",
    }


def _inventory(adset_id: str, active_ids: set[str], candidates: list[str], **overrides):
    result: guard.AdsetInventory = {
        "adset_id": adset_id,
        "active_ids": active_ids,
        "candidate_context": {ad_id: _context(ad_id, adset_id) for ad_id in candidates},
        "complete": True,
        "pages_read": 1,
        "error": None,
    }
    result.update(overrides)
    return result


def test_plan_lone_candidate_blocked_even_if_confirmed_waster():
    candidate = {"ad_id": "1", "is_confirmed_waster": True}
    inventories = {"100": _inventory("100", {"1"}, ["1"])}

    allowed, blocked = guard.plan_safe_pauses([candidate], inventories, id_key="ad_id")

    assert allowed == []
    assert blocked[0]["pause_guard_reason"] == "last_active_without_replacement"


def test_plan_two_active_candidates_allows_only_one():
    candidates = [{"ad_id": "1"}, {"ad_id": "2"}]
    inventories = {"100": _inventory("100", {"1", "2"}, ["1", "2"])}

    allowed, blocked = guard.plan_safe_pauses(candidates, inventories, id_key="ad_id")

    assert [item["ad_id"] for item in allowed] == ["1"]
    assert [item["ad_id"] for item in blocked] == ["2"]
    assert blocked[0]["pause_guard_reason"] == "last_active_without_replacement"


def test_plan_candidate_with_active_replacement_allowed():
    inventories = {"100": _inventory("100", {"1", "replacement"}, ["1"])}

    allowed, blocked = guard.plan_safe_pauses(
        [{"ad_id": "1"}], inventories, id_key="ad_id"
    )

    assert [item["ad_id"] for item in allowed] == ["1"]
    assert blocked == []


def test_plan_incomplete_inventory_blocks_everything():
    inventories = {
        "100": _inventory(
            "100", {"1", "2"}, ["1", "2"],
            complete=False, error="paging_cursor_missing",
        )
    }

    allowed, blocked = guard.plan_safe_pauses(
        [{"ad_id": "1"}, {"ad_id": "2"}], inventories, id_key="ad_id"
    )

    assert allowed == []
    assert {item["pause_guard_reason"] for item in blocked} == {"paging_cursor_missing"}


def test_fetch_inventory_reads_two_pages(monkeypatch):
    responses = iter([
        _Response({"1": {"id": "1", "adset_id": "100", "status": "ACTIVE", "effective_status": "ACTIVE"}}),
        _Response({
            "data": [{"id": "1", "adset_id": "100", "status": "ACTIVE", "effective_status": "ACTIVE"}],
            "paging": {"next": "next", "cursors": {"after": "cursor-1"}},
        }),
        _Response({
            "data": [{"id": "2", "adset_id": "100", "status": "ACTIVE", "effective_status": "ACTIVE"}],
            "paging": {},
        }),
    ])
    mock_get = Mock(side_effect=lambda *args, **kwargs: next(responses))
    monkeypatch.setattr("agent.fb_common._throttled_get", mock_get)
    monkeypatch.setattr("services.fb_token_provider.get_fb_token", lambda: "test-token")

    inventories = guard.fetch_pause_inventory(["1"])

    assert inventories["100"]["complete"] is True
    assert inventories["100"]["pages_read"] == 2
    assert inventories["100"]["active_ids"] == {"1", "2"}
    assert mock_get.call_args_list[-1].kwargs["params"]["after"] == "cursor-1"


@pytest.mark.parametrize(
    "second_payload,second_status,expected_reason",
    [
        (
            {"data": [], "paging": {"next": "next", "cursors": {}}},
            200,
            "paging_cursor_missing",
        ),
        ({"error": "boom"}, 500, "fb_http_500"),
    ],
)
def test_fetch_inventory_partial_response_fails_closed(
    monkeypatch, second_payload, second_status, expected_reason
):
    responses = iter([
        _Response({"1": {"id": "1", "adset_id": "100", "status": "ACTIVE", "effective_status": "ACTIVE"}}),
        _Response(second_payload, second_status),
    ])
    monkeypatch.setattr("agent.fb_common._throttled_get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr("services.fb_token_provider.get_fb_token", lambda: "test-token")

    inventory = guard.fetch_pause_inventory(["1"])["100"]

    assert inventory["complete"] is False
    assert inventory["active_ids"] == set()
    assert inventory["error"] == expected_reason


@pytest.mark.parametrize("paging", [[], ""])
def test_fetch_inventory_non_dict_paging_fails_closed(monkeypatch, paging):
    responses = iter([
        _Response({"1": {"id": "1", "adset_id": "100", "status": "ACTIVE", "effective_status": "ACTIVE"}}),
        _Response({
            "data": [{"id": "1", "adset_id": "100", "status": "ACTIVE", "effective_status": "ACTIVE"}],
            "paging": paging,
        }),
    ])
    monkeypatch.setattr("agent.fb_common._throttled_get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr("services.fb_token_provider.get_fb_token", lambda: "test-token")

    inventory = guard.fetch_pause_inventory(["1"])["100"]

    assert inventory["complete"] is False
    assert inventory["active_ids"] == set()
    assert inventory["error"] == "invalid_paging"


@pytest.mark.parametrize("include_paging", [False, True])
def test_fetch_inventory_absent_or_none_paging_is_complete(monkeypatch, include_paging):
    inventory_payload = {
        "data": [{"id": "1", "adset_id": "100", "status": "ACTIVE", "effective_status": "ACTIVE"}],
    }
    if include_paging:
        inventory_payload["paging"] = None
    responses = iter([
        _Response({"1": {"id": "1", "adset_id": "100", "status": "ACTIVE", "effective_status": "ACTIVE"}}),
        _Response(inventory_payload),
    ])
    monkeypatch.setattr("agent.fb_common._throttled_get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr("services.fb_token_provider.get_fb_token", lambda: "test-token")

    inventory = guard.fetch_pause_inventory(["1"])["100"]

    assert inventory["complete"] is True
    assert inventory["active_ids"] == {"1"}


def test_safe_pause_rechecks_under_lock_and_blocks_last_active(monkeypatch, tmp_path):
    inventories = {"100": _inventory("100", {"1"}, ["1"])}
    mock_alert = Mock()
    monkeypatch.setattr(guard, "fetch_pause_inventory", Mock(return_value=inventories))
    monkeypatch.setattr(guard, "_LOCKS_DIR", tmp_path)
    monkeypatch.setattr("services.notifications.send_critical_alert", mock_alert)

    outcome = guard.safe_pause_ad("1", source="test")

    assert outcome.ok is False
    assert outcome.reason == "last_active_without_replacement"
    assert outcome.action_result is guard.ActionResult.FAILED
    assert outcome.check_reason is not None
    assert outcome.check_reason.code == "LAST_EFFECTIVE_ACTIVE"
    mock_alert.assert_not_called()


def test_adset_mutation_lock_is_reentrant_and_releases_outer_lock(
    monkeypatch, tmp_path
):
    mock_flock = Mock()
    monkeypatch.setattr(guard, "_LOCKS_DIR", tmp_path)
    monkeypatch.setattr(guard.fcntl, "flock", mock_flock)

    with guard.adset_mutation_lock("100"):
        with guard.adset_mutation_lock("100"):
            pass

    with guard.adset_mutation_lock("100"):
        pass

    assert [invocation.args[1] for invocation in mock_flock.call_args_list] == [
        guard.fcntl.LOCK_EX,
        guard.fcntl.LOCK_UN,
        guard.fcntl.LOCK_EX,
        guard.fcntl.LOCK_UN,
    ]


def test_adset_mutation_lock_blocks_other_thread_until_release(monkeypatch, tmp_path):
    contender_started = Event()
    contender_acquired = Event()

    monkeypatch.setattr(guard, "_LOCKS_DIR", tmp_path)

    def contend_for_same_adset() -> None:
        contender_started.set()
        with guard.adset_mutation_lock("100"):
            contender_acquired.set()

    with guard.adset_mutation_lock("100"):
        contender = Thread(target=contend_for_same_adset, daemon=True)
        contender.start()
        assert contender_started.wait(timeout=1)
        assert not contender_acquired.wait(timeout=0.05)

    assert contender_acquired.wait(timeout=1)
    contender.join(timeout=1)
    assert not contender.is_alive()


def test_safe_pause_with_replacement_is_proposal_only(monkeypatch, tmp_path):
    inventories = {"100": _inventory("100", {"1", "2"}, ["1"])}
    mock_fetch = Mock(return_value=inventories)
    typed_pause = Mock()
    monkeypatch.setattr(guard, "fetch_pause_inventory", mock_fetch)
    monkeypatch.setattr(guard, "_LOCKS_DIR", tmp_path)
    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.set_ad_status",
        typed_pause,
    )

    outcome = guard.safe_pause_ad("1", source="test")

    assert outcome.ok is False
    assert outcome.reason == "owner_approval_required"
    assert outcome.active_other_ids == ("2",)
    assert mock_fetch.call_count == 1
    typed_pause.assert_not_called()


def test_safe_pause_two_cross_calls_are_both_proposal_only(monkeypatch, tmp_path):
    """Read-only guard не резервирует tombstone и не делает provider write."""
    def stale_inventory(ad_ids):
        return {"100": _inventory("100", {"1", "2"}, ad_ids)}

    typed_pause = Mock()
    mock_alert = Mock()
    monkeypatch.setattr(guard, "fetch_pause_inventory", Mock(side_effect=stale_inventory))
    monkeypatch.setattr(guard, "_LOCKS_DIR", tmp_path)
    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.set_ad_status",
        typed_pause,
    )
    monkeypatch.setattr("services.notifications.send_critical_alert", mock_alert)

    first = guard.safe_pause_ad("1", source="worker-1")
    second = guard.safe_pause_ad("2", source="worker-2")

    assert first.ok is False
    assert first.reason == "owner_approval_required"
    assert second.ok is False
    assert second.reason == "owner_approval_required"
    assert second.action_result is guard.ActionResult.FAILED
    assert second.check_reason is not None
    assert second.check_reason.code == "PAUSE_GUARD_DENIED"
    typed_pause.assert_not_called()
    mock_alert.assert_not_called()


def test_read_only_guard_does_not_write_tombstone(monkeypatch, tmp_path):
    inventories = {"100": _inventory("100", {"1", "2"}, ["1"])}
    save_tombstones = Mock(side_effect=OSError("disk full"))
    monkeypatch.setattr(guard, "fetch_pause_inventory", Mock(return_value=inventories))
    monkeypatch.setattr(guard, "_LOCKS_DIR", tmp_path)
    monkeypatch.setattr(guard, "_save_pause_tombstones", save_tombstones)
    monkeypatch.setattr("services.notifications.send_critical_alert", Mock())

    outcome = guard.safe_pause_ad("1", source="test")

    assert outcome.ok is False
    assert outcome.reason == "owner_approval_required"
    save_tombstones.assert_not_called()


def test_read_only_guard_creates_no_reservation_file(monkeypatch, tmp_path):
    inventories = {"100": _inventory("100", {"1", "2"}, ["1"])}
    monkeypatch.setattr(guard, "fetch_pause_inventory", Mock(return_value=inventories))
    monkeypatch.setattr(guard, "_LOCKS_DIR", tmp_path)

    outcome = guard.safe_pause_ad("1", source="test")

    assert outcome.reason == "owner_approval_required"
    assert not (tmp_path / "adset-100.paused.json").exists()


def test_read_only_guard_never_calls_typed_transport(monkeypatch, tmp_path):
    def inventory(ad_ids):
        return {"100": _inventory("100", {"1", "2"}, ad_ids)}

    typed_pause = Mock(side_effect=RuntimeError("access_token=SECRET"))
    monkeypatch.setattr(guard, "fetch_pause_inventory", Mock(side_effect=inventory))
    monkeypatch.setattr(guard, "_LOCKS_DIR", tmp_path)
    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.set_ad_status",
        typed_pause,
    )

    outcome = guard.safe_pause_ad("1", source="test")

    assert outcome.reason == "owner_approval_required"
    typed_pause.assert_not_called()


def test_read_only_calls_do_not_create_or_reconcile_tombstones(monkeypatch, tmp_path):
    active_both_a = {"100": _inventory("100", {"1", "2"}, ["1"])}
    active_both_b = {"100": _inventory("100", {"1", "2"}, ["2"])}
    converged_b = {
        "100": {
            **_inventory("100", {"2"}, ["1", "2"]),
            "candidate_context": {
                "1": {
                    "ad_id": "1", "adset_id": "100",
                    "configured_status": "PAUSED", "effective_status": "PAUSED",
                },
                "2": _context("2", "100"),
            },
        }
    }
    responses = iter([active_both_a, converged_b, active_both_b])
    monkeypatch.setattr(guard, "fetch_pause_inventory", Mock(side_effect=lambda ad_ids: next(responses)))
    monkeypatch.setattr(guard, "_LOCKS_DIR", tmp_path)
    monkeypatch.setattr("services.notifications.send_critical_alert", Mock())

    first = guard.safe_pause_ad("1", source="first")
    after_convergence = guard.safe_pause_ad("2", source="converged")
    after_reactivation = guard.safe_pause_ad("2", source="reactivated")

    assert first.reason == "owner_approval_required"
    assert after_convergence.reason == "last_active_without_replacement"
    assert after_reactivation.reason == "owner_approval_required"
    assert not (tmp_path / "adset-100.paused.json").exists()


def test_active_tombstone_kept_until_conservative_ttl():
    t0 = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    entry = guard._new_tombstone("PAUSE_CONFIRMED", t0)
    inventory = _inventory("100", {"1", "2"}, ["1"])

    before_ttl, changed_before = guard._reconcile_pause_tombstones(
        "100", {"1": entry}, inventory,
        now=t0 + guard._TOMBSTONE_TTL - timedelta(seconds=1),
    )
    after_ttl, changed_after = guard._reconcile_pause_tombstones(
        "100", {"1": entry}, inventory,
        now=t0 + guard._TOMBSTONE_TTL,
    )

    assert "1" in before_ttl
    assert changed_before is False
    assert after_ttl == {}
    assert changed_after is True


def test_missing_context_tombstone_is_bounded_but_not_removed_early():
    t0 = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    entry = guard._new_tombstone("AMBIGUOUS", t0)
    inventory = _inventory("100", {"2"}, ["2"])

    before_ttl, changed_before = guard._reconcile_pause_tombstones(
        "100",
        {"1": entry},
        inventory,
        now=t0 + guard._TOMBSTONE_TTL - timedelta(seconds=1),
    )
    after_ttl, changed_after = guard._reconcile_pause_tombstones(
        "100",
        {"1": entry},
        inventory,
        now=t0 + guard._TOMBSTONE_TTL,
    )

    assert "1" in before_ttl
    assert changed_before is False
    assert after_ttl == {}
    assert changed_after is True


def test_legacy_tombstone_schema_migrates_without_early_cleanup(
    monkeypatch, tmp_path
):
    path = tmp_path / "adset-100.paused.json"
    path.write_text(json.dumps({"paused_ids": ["1"]}), encoding="utf-8")
    monkeypatch.setattr(guard, "_LOCKS_DIR", tmp_path)
    now = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)

    entries, needs_write = guard._load_pause_tombstones("100", now=now)

    assert needs_write is True
    assert entries["1"]["state"] == "AMBIGUOUS"
    assert entries["1"]["updated_at"] == now.isoformat()


def test_token_bearing_exception_not_exposed_in_outcome_alert_or_log(monkeypatch, caplog):
    alert = Mock()
    monkeypatch.setattr(
        guard,
        "fetch_pause_inventory",
        Mock(side_effect=RuntimeError("https://graph.test?access_token=SUPER_SECRET&x=1")),
    )
    monkeypatch.setattr("services.notifications.send_critical_alert", alert)

    outcome = guard.safe_pause_ad("1", source="security-test")

    assert outcome.reason == "inventory_error:RuntimeError"
    assert outcome.action_result is guard.ActionResult.FAILED
    assert outcome.check_reason is not None
    assert outcome.check_reason.code == "PAUSE_GUARD_DENIED"
    assert "SUPER_SECRET" not in outcome.reason
    assert "SUPER_SECRET" not in caplog.text
    alert.assert_not_called()


def test_canonical_live_context_ignores_name_but_catches_drift():
    """Регрессия: exact-контекст несёт ``name``, полный inventory —
    нет; прямое сравнение словарей отбраковывало ВСЕ паузы (ни одного
    исполнения). Канонизация сравнивает только общие exact-поля."""
    exact = dict(_context("1"), name="CityE | Слив / 1 [PRODB]")
    full = _context("1")
    assert guard._canonical_live_context(exact) == guard._canonical_live_context(full)

    drifted = dict(_context("1"), effective_status="PAUSED")
    assert guard._canonical_live_context(exact) != guard._canonical_live_context(drifted)

    other_adset = _context("1", adset_id="200")
    assert guard._canonical_live_context(exact) != guard._canonical_live_context(other_adset)
