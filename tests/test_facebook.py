"""Тесты Facebook интеграции: ёмкость адсетов, запрет очистки объявлений.

Модуль стал read-only: у `integrations.facebook` больше нет ни HTTP-сессии,
ни исполняющих мутаторов. Чтения идут через `_throttled_get`, а DELETE/ARCHIVE
удалён совсем — `cleanup_stale_ads` отвечает `ForbiddenMutation`, потому что
у операции нет typed owner contract. Любая провайдерская мутация возможна
только в execution boundary после одобрения владельца, поэтому автофикстура
`no_provider_mutation` роняет тест при любом прямом вызове мутатора.
"""

import sys
import uuid
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import integrations.facebook as facebook_module
from integrations.facebook import (
    AdsetInventoryError,
    CleanupGuardEvidence,
    CleanupGuardError,
    cleanup_all_adsets,
    cleanup_stale_ads,
    get_adset_capacity,
    get_cleanup_capacity,
    MAX_ADS_PER_ADSET,
)
from integrations.facebook_ads_mutation_transport import ForbiddenMutation
from services.cleanup_authorization import issue_delete_authorization
from services.cleanup_repository import CleanupDeleteClaim
from tests.gateway_test_helpers import install_provider_mutation_guard


_REAL_PREPARE_CURRENT_DELETE_BOUNDARY = (
    facebook_module._prepare_current_cleanup_delete_boundary
)


def _fresh_local_zero() -> dict:
    return {
        "kb_found": True,
        "local_spend_usd": 0.0,
        "local_impressions": 0,
        "local_clicks": 0,
        "local_leads": 0,
        "local_payments": 0,
        "any_positive_delivery": False,
        "any_positive_outcome": False,
        "complete": True,
        "error": None,
    }


@pytest.fixture(autouse=True)
def no_provider_mutation(monkeypatch):
    """Ни один тест этого файла не имеет права мутировать Facebook."""
    return install_provider_mutation_guard(monkeypatch)


@pytest.fixture(autouse=True)
def _mock_durable_cleanup_boundary():
    """Integration unit tests never touch production DB or live insights."""
    with patch(
        "services.cleanup_authorization.create_cleanup_delete_authorization"
    ), patch(
        "services.cleanup_authorization.mark_cleanup_delete_http_started"
    ), patch(
        "integrations.facebook._fetch_cleanup_lifetime_zero",
        return_value={"spend": 0.0, "impressions": 0, "clicks": 0},
    ), patch(
        "integrations.facebook._prepare_current_cleanup_delete_boundary",
        return_value=(
            _fresh_local_zero(),
            "a" * 64,
            {"config_hash": "b" * 64, "config_generation": 7},
        ),
    ):
        yield


def _mock_adset_response(daily_budget_cents, ads):
    """Создаёт моковые ответы FB API для get_adset_info."""
    normalized_ads = []
    for ad in ads:
        status = ad.get("status", "")
        normalized_ads.append({
            **ad,
            "adset_id": "123",
            "effective_status": ad.get("effective_status", status),
            "creative": ad.get("creative", {"id": str(100000 + len(normalized_ads))}),
        })
    adset_resp = MagicMock()
    adset_resp.status_code = 200
    adset_resp.json.return_value = {
        "daily_budget": str(daily_budget_cents),
        "name": "Test Adset",
        "effective_status": "ACTIVE",
        "account_id": "10001",
    }

    ads_resp = MagicMock()
    ads_resp.status_code = 200
    ads_resp.json.return_value = {"data": normalized_ads}

    def side_effect(url, **kwargs):
        if "/ads" in url:
            return ads_resp
        return adset_resp

    return side_effect


def test_capacity_empty_adset():
    """Пустой адсет — все слоты свободны."""
    with patch("integrations.facebook._throttled_get") as mock_get:
        mock_get.side_effect = _mock_adset_response(4500, [])  # $45
        cap = get_adset_capacity("123")

    assert cap["ad_count"] == 0
    assert cap["available"] == MAX_ADS_PER_ADSET
    assert cap["daily_budget"] == 45.0
    assert cap["recommended"] == 3  # $45 / $15
    assert cap["inventory_complete"] is True
    assert cap["effective_active_count"] == 0


def test_capacity_full_adset():
    """Полный адсет — нет свободных слотов."""
    ads = [{"id": str(i), "name": f"Ad {i}", "status": "ACTIVE",
            "created_time": "2026-02-01T10:00:00+0000"} for i in range(50)]

    with patch("integrations.facebook._throttled_get") as mock_get:
        mock_get.side_effect = _mock_adset_response(3000, ads)  # $30
        cap = get_adset_capacity("123")

    assert cap["ad_count"] == 50
    assert cap["available"] == 0
    assert cap["recommended"] == 2  # $30 / $15
    assert cap["effective_active_count"] == 50


def test_capacity_stale_ads():
    """Стейл объявления: PAUSED возрастом 15+ полных дней."""
    old_date = (datetime.now().astimezone() - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    recent_date = (datetime.now().astimezone() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S+0000")

    ads = [
        {"id": "1", "name": "Old paused", "status": "PAUSED", "created_time": old_date},
        {"id": "2", "name": "Recent paused", "status": "PAUSED", "created_time": recent_date},
        {"id": "3", "name": "Old active", "status": "ACTIVE", "created_time": old_date},
    ]

    with patch("integrations.facebook._throttled_get") as mock_get:
        mock_get.side_effect = _mock_adset_response(6000, ads)  # $60
        cap = get_adset_capacity("123")

    assert cap["ad_count"] == 3
    assert len(cap["stale_ads"]) == 1  # Только старый PAUSED
    assert cap["stale_ads"][0]["id"] == "1"
    assert cap["effective_active_count"] == 1


def test_capacity_no_budget():
    """Нулевой бюджет — рекомендация 0."""
    with patch("integrations.facebook._throttled_get") as mock_get:
        mock_get.side_effect = _mock_adset_response(0, [])
        cap = get_adset_capacity("123")

    assert cap["recommended"] == 0
    assert cap["daily_budget"] == 0


def test_capacity_recommended_count():
    """Рекомендуемое количество = бюджет / $15."""
    with patch("integrations.facebook._throttled_get") as mock_get:
        mock_get.side_effect = _mock_adset_response(7500, [])  # $75
        cap = get_adset_capacity("123")

    assert cap["recommended"] == 5  # $75 / $15


def test_capacity_stale_days_30_отсекает_20_дневное_paused():
    """get_adset_capacity(stale_days=30): дефолт 15 включает 20-дневное PAUSED,
    а stale_days=30 отсекает его (объявление ещё не «стейл» по новому порогу)."""
    date_20d = (datetime.now().astimezone() - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    date_45d = (datetime.now().astimezone() - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%S+0000")

    ads = [
        {"id": "1", "name": "20 days paused", "status": "PAUSED", "created_time": date_20d},
        {"id": "2", "name": "45 days paused", "status": "PAUSED", "created_time": date_45d},
    ]

    # Дефолт stale_days=15 — 20-дневное PAUSED уже считается «стейл» (включено)
    with patch("integrations.facebook._throttled_get") as mock_get:
        mock_get.side_effect = _mock_adset_response(6000, ads)
        cap_default = get_adset_capacity("123")
    stale_ids_default = {ad["id"] for ad in cap_default["stale_ads"]}
    assert stale_ids_default == {"1", "2"}, "дефолт 15 дней должен включать оба PAUSED"

    # stale_days=30 — 20-дневное PAUSED моложе порога, отсекается
    with patch("integrations.facebook._throttled_get") as mock_get:
        mock_get.side_effect = _mock_adset_response(6000, ads)
        cap_30 = get_adset_capacity("123", stale_days=30)
    stale_ids_30 = {ad["id"] for ad in cap_30["stale_ads"]}
    assert stale_ids_30 == {"2"}, "stale_days=30 должен отсечь 20-дневное PAUSED, оставить только 45-дневное"


def test_capacity_includes_exact_15_day_cutoff_deterministically():
    """Ровно 15 суток включаются без зависимости от времени выполнения теста."""
    fixed_now = datetime.fromisoformat("2026-07-21T12:00:00+00:00")
    ads = [
        {
            "id": f"age-{age_days}",
            "name": f"Age {age_days}",
            "status": "PAUSED",
            "created_time": (fixed_now - timedelta(days=age_days)).strftime(
                "%Y-%m-%dT%H:%M:%S+0000"
            ),
        }
        for age_days in (14, 15, 16)
    ]

    with patch("integrations.facebook.datetime", wraps=datetime) as mocked_datetime, patch(
        "integrations.facebook._throttled_get"
    ) as mock_get:
        mocked_datetime.now.return_value = fixed_now
        mock_get.side_effect = _mock_adset_response(6000, ads)
        capacity = get_adset_capacity("123")

    assert [ad["id"] for ad in capacity["stale_ads"]] == ["age-16", "age-15"]


def test_capacity_reads_all_pages_and_counts_effective_active():
    """ACTIVE на второй странице не теряется."""
    adset = MagicMock(status_code=200)
    adset.json.return_value = {
        "daily_budget": "3000",
        "name": "Test Adset",
        "effective_status": "ACTIVE",
        "account_id": "10001",
    }
    page_one = MagicMock(status_code=200)
    page_one.json.return_value = {
        "data": [{
            "id": "paused-1", "name": "Paused", "status": "PAUSED",
            "effective_status": "PAUSED", "created_time": "2026-01-01T00:00:00+0000",
            "adset_id": "123",
            "creative": {"id": "90001"},
        }],
        "paging": {"next": "safe-next", "cursors": {"after": "cursor-1"}},
    }
    page_two = MagicMock(status_code=200)
    page_two.json.return_value = {"data": [{
        "id": "active-1", "name": "Active", "status": "ACTIVE",
        "effective_status": "ACTIVE", "created_time": "2026-01-02T00:00:00+0000",
        "adset_id": "123",
        "creative": {"id": "90002"},
    }]}

    with patch("integrations.facebook._throttled_get", side_effect=[adset, page_one, page_two]):
        cap = get_adset_capacity("123")

    assert cap["ad_count"] == 2
    assert cap["effective_active_count"] == 1
    assert cap["effective_active_ids"] == ["active-1"]


def test_capacity_partial_paging_fails_closed():
    """next без cursor — это partial inventory, а не нулевая страница."""
    adset = MagicMock(status_code=200)
    adset.json.return_value = {
        "daily_budget": "3000",
        "name": "Test Adset",
        "effective_status": "ACTIVE",
        "account_id": "10001",
    }
    partial = MagicMock(status_code=200)
    partial.json.return_value = {"data": [], "paging": {"next": "unsafe-next"}}

    with patch("integrations.facebook._throttled_get", side_effect=[adset, partial]):
        with pytest.raises(AdsetInventoryError, match="paging_cursor_missing"):
            get_adset_capacity("123")


def test_inventory_is_unfiltered_and_unknown_status_blocks_cleanup(no_provider_mutation):
    """Будущий enum виден локально и запрещает весь destructive cleanup."""
    old_date = (datetime.now().astimezone() - timedelta(days=45)).strftime(
        "%Y-%m-%dT%H:%M:%S+0000"
    )
    adset = MagicMock(status_code=200)
    adset.json.return_value = {
        "daily_budget": "3000",
        "name": "Test Adset",
        "effective_status": "ACTIVE",
        "account_id": "10001",
    }
    ads = MagicMock(status_code=200)
    ads.json.return_value = {"data": [
        {
            "id": "active-1", "name": "Active", "status": "ACTIVE",
            "effective_status": "ACTIVE", "created_time": old_date, "adset_id": "123",
            "creative": {"id": "90003"},
        },
        {
            "id": "old-1", "name": "Old", "status": "PAUSED",
            "effective_status": "PAUSED", "created_time": old_date, "adset_id": "123",
            "creative": {"id": "90004"},
        },
        {
            "id": "future-1", "name": "Future", "status": "PAUSED",
            "effective_status": "FUTURE_DELIVERY_STATE", "created_time": old_date,
            "adset_id": "123",
            "creative": {"id": "90005"},
        },
        {
            "id": "deleted-1", "name": "Deleted", "status": "DELETED",
            "effective_status": "DELETED", "created_time": old_date, "adset_id": "123",
        },
    ]}

    with patch("integrations.facebook._throttled_get", side_effect=[adset, ads]) as get:
        capacity = get_adset_capacity("123")

    ad_edge_params = get.call_args_list[1].kwargs["params"]
    assert "effective_status" not in ad_edge_params
    assert capacity["fetched_ad_count"] == 4
    assert capacity["ad_count"] == 3
    assert capacity["available"] == MAX_ADS_PER_ADSET - 3
    assert "future-1" in capacity["ad_ids"]
    assert "deleted-1" not in capacity["ad_ids"]
    assert capacity["unknown_effective_status_ids"] == ["future-1"]

    with patch("integrations.facebook.get_adset_capacity", return_value=capacity), \
         patch("services.notifications.send_critical_alert") as alert, \
         patch("integrations.facebook._mint_cleanup_guard_evidence") as mint:
        with pytest.raises(CleanupGuardError, match="unknown_effective_status"):
            get_cleanup_capacity("123", candidate_id="old-1")

    alert.assert_called_once()
    mint.assert_not_called()
    no_provider_mutation.assert_untouched()


def _guard_capacity(*, active: bool = True) -> dict:
    old = {
        "id": "old-1", "name": "Old", "status": "PAUSED",
        "effective_status": "PAUSED", "created_time": "2026-01-01T00:00:00+0000",
        "adset_id": "123",
    }
    ad_ids = ["old-1"]
    active_ids = []
    if active:
        ad_ids.append("active-1")
        active_ids.append("active-1")
    return {
        "adset_id": "123", "name": "Test", "daily_budget": 30.0,
        "adset_effective_status": "ACTIVE",
        "ad_count": len(ad_ids), "max_ads": 50, "available": 50 - len(ad_ids),
        "recommended": 2, "stale_ads": [old], "ad_ids": ad_ids,
        "effective_active_ids": active_ids,
        "effective_active_count": len(active_ids),
        "unknown_effective_status_ids": [],
        "fetched_ad_count": len(ad_ids),
        "inventory_complete": True,
    }


def _delete_authorization(ad_id: str = "old-1"):
    claim = CleanupDeleteClaim(
        claim_id=f"claim-{uuid.uuid4().hex}",
        run_id="run-1",
        workflow_id="workflow-1",
        ad_id=ad_id,
        ad_name="Old",
        adset_id="123",
        purpose="REPLACEMENT_SLOT",
        state="CLAIMED",
        capacity_before=48,
        claimed_at=datetime.now().astimezone(),
    )
    with patch(
        "services.cleanup_authorization.create_cleanup_delete_authorization",
    ):
        return issue_delete_authorization(claim)


def test_cleanup_zero_active_blocks_delete_and_alerts(no_provider_mutation):
    with patch("integrations.facebook.get_adset_capacity", return_value=_guard_capacity(active=False)), \
         patch("services.notifications.send_critical_alert") as alert:
        with pytest.raises(CleanupGuardError, match="recovery_needed_no_active"):
            get_cleanup_capacity("123", candidate_id="old-1")

    no_provider_mutation.assert_untouched()
    alert.assert_called_once()


def test_cleanup_unknown_inventory_blocks_delete_and_alerts(no_provider_mutation):
    with patch("integrations.facebook.get_adset_capacity", side_effect=AdsetInventoryError("partial")), \
         patch("services.notifications.send_critical_alert") as alert:
        with pytest.raises(CleanupGuardError, match="inventory_unknown"):
            get_cleanup_capacity("123", candidate_id="old-1")

    no_provider_mutation.assert_untouched()
    alert.assert_called_once()


def test_cleanup_malformed_inventory_emits_one_alert(no_provider_mutation):
    malformed = _guard_capacity()
    malformed["inventory_complete"] = False
    with patch("integrations.facebook.get_adset_capacity", return_value=malformed), \
         patch("services.notifications.send_critical_alert") as alert:
        with pytest.raises(CleanupGuardError, match="inventory_malformed"):
            get_cleanup_capacity("123", candidate_id="old-1")

    alert.assert_called_once()
    no_provider_mutation.assert_untouched()


def test_current_delete_boundary_rereads_runtime_and_local_evidence():
    config = {
        "enabled": True,
        "dry_run": False,
        "allow_irreversible_delete": True,
        "replacement_enabled": True,
        "kill_switch": False,
        "stale_days": 30,
    }
    local = _fresh_local_zero()
    identity = {"config_hash": "b" * 64, "config_generation": 7}
    with patch(
        "services.adset_cleaner.get_cleaner_config",
        return_value=config,
    ) as read_config, patch(
        "services.adset_cleaner.cleanup_runtime_config_identity",
        return_value=identity,
    ) as hash_config, patch(
        "services.adset_cleaner.load_local_zero_evidence",
        return_value=local,
    ) as read_local, patch(
        "integrations.facebook.prepare_cleanup_delete_boundary",
        return_value="c" * 64,
    ) as persist:
        result = _REAL_PREPARE_CURRENT_DELETE_BOUNDARY(
            claim_id="claim-1",
            workflow_id="workflow-1",
            adset_id="adset-1",
            ad_id="old-1",
        )

    assert result == (local, "c" * 64, identity)
    read_config.assert_called_once_with()
    hash_config.assert_called_once_with(config)
    read_local.assert_called_once_with("old-1")
    persist.assert_called_once_with(
        claim_id="claim-1",
        workflow_id="workflow-1",
        adset_id="adset-1",
        ad_id="old-1",
        runtime_config_hash="b" * 64,
        runtime_config_generation=7,
        local_zero_evidence=local,
    )


def test_cleanup_never_deletes_active_or_last_total_object(no_provider_mutation):
    capacity = _guard_capacity()
    capacity["stale_ads"] = []
    with patch("integrations.facebook.get_adset_capacity", return_value=capacity):
        with pytest.raises(CleanupGuardError, match="candidate_not_live_stale"):
            get_cleanup_capacity("123", candidate_id="active-1")

    no_provider_mutation.assert_untouched()


def _switching_candidate_id():
    """str-подкласс, который меняет значение между вызовами __str__."""

    class SwitchingString(str):
        calls = 0

        def __str__(self):
            self.calls += 1
            return "old-1" if self.calls == 1 else "active-1"

    return SwitchingString("active-1")


def _forbidden_cleanup_case(case: str):
    """Готовит вход в cleanup_stale_ads: (candidates, count, evidence, extra).

    `extra` — объект, который тест дополнительно проверяет после вызова.
    """
    capacity = _guard_capacity()
    if case in {"multi_candidate_batch", "cross_candidate_binding"}:
        second = {**capacity["stale_ads"][0], "id": "old-2", "name": "Old 2"}
        capacity["stale_ads"].append(second)
        capacity["ad_ids"].insert(1, "old-2")
        capacity["ad_count"] += 1
        capacity["available"] -= 1
        capacity["fetched_ad_count"] += 1

    with patch("integrations.facebook.get_adset_capacity", return_value=capacity):
        guarded = get_cleanup_capacity("123", candidate_id="old-1")
    genuine = guarded["cleanup_guard_evidence"]

    if case == "valid_single_candidate":
        return [{"id": "old-1", "name": "Old"}], 1, genuine, None
    if case == "forged_evidence":
        forged = CleanupGuardEvidence(
            adset_id=genuine.adset_id,
            stale_days=genuine.stale_days,
            candidate_id=genuine.candidate_id,
            total_ids=genuine.total_ids,
            effective_active_ids=genuine.effective_active_ids,
            issued_at_monotonic=genuine.issued_at_monotonic,
        )
        return [{"id": "old-1", "name": "Old"}], 1, forged, None
    if case == "replaced_evidence":
        return [{"id": "old-1", "name": "Old"}], 1, replace(genuine), None
    if case == "mutated_evidence":
        object.__setattr__(genuine, "adset_id", "456")
        return [{"id": "old-1", "name": "Old"}], 1, genuine, None
    if case == "multi_candidate_batch":
        candidates = [
            {"id": "old-2", "name": "Old 2"},
            {"id": "old-1", "name": "Old"},
        ]
        return candidates, 2, genuine, None
    if case == "cross_candidate_binding":
        return [{"id": "old-2", "name": "Old 2"}], 1, genuine, None
    if case == "cross_adset_binding":
        return [{"id": "old-1", "name": "Old", "adset_id": "456"}], 1, genuine, None
    if case == "stateful_str_id":
        switching = _switching_candidate_id()
        return [{"id": switching, "name": "Old"}], 1, genuine, switching
    raise AssertionError(f"неизвестный кейс {case}")


@pytest.mark.parametrize(
    "case",
    [
        "valid_single_candidate",
        "forged_evidence",
        "replaced_evidence",
        "mutated_evidence",
        "multi_candidate_batch",
        "cross_candidate_binding",
        "cross_adset_binding",
        "stateful_str_id",
    ],
)
def test_cleanup_stale_ads_is_forbidden_before_any_side_effect(case, no_provider_mutation):
    """DELETE/ARCHIVE удалён из продукта: у операции нет typed owner contract.

    Раньше здесь проверялась конкретная ветка guard-цепочки (подделанная
    evidence, батч из двух кандидатов, str-подкласс с меняющимся id и т.д.).
    Цепочка вместе с самим DELETE удалена, поэтому инвариант «никогда не удаляем
    лишнее» усилился до «удалять нечем»: при ЛЮБОМ входе — от полностью
    корректного до враждебного — операция отклоняется ДО любых побочных
    эффектов. Ни adset-lock, ни durable http-start, ни consume evidence, ни
    единого обращения к Facebook.
    """
    candidates, count, evidence, extra = _forbidden_cleanup_case(case)
    authorization = _delete_authorization()

    with patch("integrations.facebook.get_adset_capacity") as capacity, \
         patch("services.adset_pause_guard.adset_mutation_lock") as lock, \
         patch("services.cleanup_authorization.mark_cleanup_delete_http_started") as http_started, \
         patch("services.cleanup_authorization.revoke_delete_authorization") as revoke, \
         patch("integrations.facebook._consume_cleanup_guard_evidence") as consume, \
         patch("integrations.facebook._fetch_cleanup_lifetime_zero") as lifetime:
        with pytest.raises(ForbiddenMutation, match="DELETE_ARCHIVE_OPERATION_FORBIDDEN"):
            cleanup_stale_ads(
                candidates,
                count,
                guard_evidence=evidence,
                authorization=authorization,
            )

    capacity.assert_not_called()
    lock.assert_not_called()
    http_started.assert_not_called()
    revoke.assert_not_called()
    consume.assert_not_called()
    lifetime.assert_not_called()
    no_provider_mutation.assert_untouched()
    if case == "stateful_str_id":
        assert extra.calls == 0, "враждебный id не должен даже приводиться к строке"


def test_cleanup_stale_ads_forbidden_on_replay_and_after_expiry(no_provider_mutation):
    """Повтор и протухшая evidence тоже запрещены — отказ идемпотентен.

    Прежде evidence была одноразовой: второй вызов ловил consumed_or_forged, а
    просроченная — evidence_expired. Теперь отказ не зависит от состояния
    evidence вообще, поэтому проверяем именно это: тот же вход, вызванный
    дважды, и просроченный вход дают один и тот же безопасный результат.
    """
    with patch("integrations.facebook.get_adset_capacity", return_value=_guard_capacity()):
        guarded = get_cleanup_capacity("123", candidate_id="old-1")
    evidence = guarded["cleanup_guard_evidence"]
    authorization = _delete_authorization()
    candidates = [{"id": "old-1", "name": "Old"}]

    for attempt in range(2):
        with pytest.raises(ForbiddenMutation, match="DELETE_ARCHIVE_OPERATION_FORBIDDEN"):
            cleanup_stale_ads(
                candidates,
                1,
                guard_evidence=evidence,
                authorization=authorization,
            )
        assert attempt < 2

    # Протухшая по времени evidence — тот же отказ, без обращения к монотонным часам
    with patch("integrations.facebook.time.monotonic", side_effect=AssertionError("нет проверки TTL")):
        with pytest.raises(ForbiddenMutation, match="DELETE_ARCHIVE_OPERATION_FORBIDDEN"):
            cleanup_stale_ads(
                candidates,
                1,
                guard_evidence=evidence,
                authorization=authorization,
            )

    no_provider_mutation.assert_untouched()


@pytest.mark.parametrize("effective_status", ["PENDING_REVIEW", "WITH_ISSUES", "UNKNOWN"])
def test_cleanup_candidates_require_safe_effective_status(effective_status):
    old_date = (datetime.now().astimezone() - timedelta(days=45)).strftime(
        "%Y-%m-%dT%H:%M:%S+0000"
    )
    ads = [{
        "id": "old-1",
        "name": "Old",
        "status": "PAUSED",
        "effective_status": effective_status,
        "created_time": old_date,
    }]

    with patch("integrations.facebook._throttled_get") as mock_get:
        mock_get.side_effect = _mock_adset_response(3000, ads)
        capacity = get_adset_capacity("123")

    assert capacity["stale_ads"] == []


def test_cleanup_low_level_requires_explicit_guard_evidence():
    with pytest.raises(TypeError, match="guard_evidence"):
        cleanup_stale_ads([{"id": "old-1", "name": "Old"}], 1)


def test_cleanup_all_adsets_zero_active_blocks_every_delete(no_provider_mutation):
    """Legacy cleanup-all отключён до discovery и DELETE."""
    with patch("agent.adset_discovery.get_adsets_dict", return_value={"A": {"L1": "123"}}) as discovery, \
         patch("services.adset_pause_guard.adset_mutation_lock", return_value=nullcontext()) as lock, \
         patch("integrations.facebook.get_adset_capacity", return_value=_guard_capacity(active=False)) as capacity, \
         patch("services.notifications.send_critical_alert") as alert:
        with pytest.raises(CleanupGuardError, match="direct_cleanup_disabled"):
            cleanup_all_adsets()

    # Отключён именно ДО любых действий: ни discovery, ни чтения ёмкости, ни lock
    discovery.assert_not_called()
    capacity.assert_not_called()
    lock.assert_not_called()
    no_provider_mutation.assert_untouched()
    alert.assert_not_called()


def _account_inventory_response(rows, *, next_cursor=None):
    response = MagicMock(status_code=200)
    payload = {"data": rows}
    if next_cursor is not None:
        payload["paging"] = {
            "next": "https://graph.example/next",
            "cursors": {"after": next_cursor},
        }
    response.json.return_value = payload
    return response


def _account_inventory_ad(ad_id: str, *, account_id: str = "123") -> dict:
    return {
        "id": ad_id,
        "name": f"Ad {ad_id}",
        "account_id": account_id,
        "adset_id": "456",
        "created_time": "2026-07-10T10:00:00+0000",
        "status": "ACTIVE",
        "effective_status": "ACTIVE",
    }


def test_complete_account_inventory_reads_cursor_pages_and_is_read_only(no_provider_mutation):
    first = _account_inventory_response(
        [_account_inventory_ad("ad-1")],
        next_cursor="cursor-1",
    )
    final = _account_inventory_response([_account_inventory_ad("ad-2")])
    with patch(
        "services.fb_token_provider.fb_account", return_value=nullcontext()
    ), patch(
        "integrations.facebook.get_fb_account_id", return_value="act_123"
    ), patch(
        "integrations.facebook.get_fb_token", return_value="test-token"
    ), patch(
        "integrations.facebook._throttled_get", side_effect=[first, final]
    ) as get:
        rows = facebook_module.fetch_complete_account_ad_inventory("online", "123")

    assert [row["id"] for row in rows] == ["ad-1", "ad-2"]
    assert all(row["account_kind"] == "online" for row in rows)
    assert all(row["account_id"] == "123" for row in rows)
    assert all(row["inventory_complete"] is True for row in rows)
    assert rows.inventory_complete is True
    assert (rows.account_kind, rows.account_id) == ("online", "123")
    assert get.call_args_list[0].kwargs["params"].get("after") is None
    assert get.call_args_list[1].kwargs["params"]["after"] == "cursor-1"
    no_provider_mutation.assert_untouched()


@pytest.mark.parametrize(
    "responses",
    [
        [
            _account_inventory_response(
                [_account_inventory_ad("ad-1", account_id="999")]
            )
        ],
        [
            _account_inventory_response(
                [_account_inventory_ad("ad-1")], next_cursor="same"
            ),
            _account_inventory_response(
                [_account_inventory_ad("ad-2")], next_cursor="same"
            ),
        ],
        [
            _account_inventory_response(
                [_account_inventory_ad("ad-1")], next_cursor="next"
            ),
            _account_inventory_response([_account_inventory_ad("ad-1")]),
        ],
    ],
    ids=["wrong-account", "cursor-cycle", "duplicate-ad"],
)
def test_complete_account_inventory_fails_closed_without_partial_result(responses, no_provider_mutation):
    with patch(
        "services.fb_token_provider.fb_account", return_value=nullcontext()
    ), patch(
        "integrations.facebook.get_fb_account_id", return_value="123"
    ), patch(
        "integrations.facebook.get_fb_token", return_value="test-token"
    ), patch(
        "integrations.facebook._throttled_get", side_effect=responses
    ):
        with pytest.raises(AdsetInventoryError):
            facebook_module.fetch_complete_account_ad_inventory("offline", "123")

    no_provider_mutation.assert_untouched()


def test_complete_account_inventory_rejects_scope_before_http():
    with patch(
        "services.fb_token_provider.fb_account", return_value=nullcontext()
    ), patch(
        "integrations.facebook.get_fb_account_id", return_value="999"
    ), patch("integrations.facebook._throttled_get") as get:
        with pytest.raises(AdsetInventoryError, match="scope_mismatch"):
            facebook_module.fetch_complete_account_ad_inventory("offline", "123")

    get.assert_not_called()


def test_complete_account_inventory_allows_routed_offline_cabinet(no_provider_mutation):
    """Оффлайн-скан кабинета cabinet_b легален при process-default cabinet_a.

    Карта маршрутизации город→кабинет: один offline FB_TOKEN
    обслуживает оба кабинета, поэтому scope-страж принимает маршрутизированный
    кабинет, а не только process-default.
    """
    cabinet_b = "29716040622546856"
    final = _account_inventory_response(
        [_account_inventory_ad("ad-1", account_id=cabinet_b)]
    )
    with patch(
        "services.fb_token_provider.fb_account", return_value=nullcontext()
    ), patch(
        "integrations.facebook.get_fb_account_id", return_value="152882611033373"
    ), patch(
        "integrations.facebook.get_fb_token", return_value="test-token"
    ), patch(
        "agent.scheduler.load_settings", return_value={}
    ), patch(
        "integrations.facebook._throttled_get", side_effect=[final]
    ) as get:
        rows = facebook_module.fetch_complete_account_ad_inventory(
            "offline", cabinet_b
        )

    assert [row["id"] for row in rows] == ["ad-1"]
    assert (rows.account_kind, rows.account_id) == ("offline", cabinet_b)
    assert f"act_{cabinet_b}/ads" in get.call_args_list[0].args[0]
    no_provider_mutation.assert_untouched()


def test_complete_account_inventory_still_rejects_unrouted_offline_cabinet():
    """Кабинет вне карты маршрутизации — отказ до первого HTTP (fail-closed)."""
    with patch(
        "services.fb_token_provider.fb_account", return_value=nullcontext()
    ), patch(
        "integrations.facebook.get_fb_account_id", return_value="152882611033373"
    ), patch(
        "agent.scheduler.load_settings", return_value={}
    ), patch("integrations.facebook._throttled_get") as get:
        with pytest.raises(AdsetInventoryError, match="scope_mismatch"):
            facebook_module.fetch_complete_account_ad_inventory("offline", "555000111")

    get.assert_not_called()
