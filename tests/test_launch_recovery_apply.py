"""Fresh recovery apply: CAS, drift, replay, daily cap и безопасный CLI."""

from __future__ import annotations

import sqlite3
import threading
import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services import creative_intelligence as ci
from services import launch_recovery as recovery
from services import launch_recovery_apply as apply
from services.approval_checker_models import (
    ActionExecution,
    ActionResult,
    ActionRun,
    OperationState,
)
from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import (
    blocked_outcome,
    init_action_db,
    install_proposal_recorder,
    install_provider_mutation_guard,
)


LOCAL_TZ = timezone(timedelta(hours=5))
MANIFEST = "a" * 64
NOW = datetime(2026, 7, 21, 12, tzinfo=LOCAL_TZ)
PROVIDER_MEDIA_SHA = recovery._provider_media_sha256(
    "video",
    (recovery.MediaFileEvidence("asset.mp4", 3, "b" * 64),),
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    ci.DB_PATH = None
    db_path = str(tmp_path / "recovery-apply.db")
    ci.init_kb(db_path)
    # Recovery-apply стал producer'ом: он пишет owner proposal, а для этого
    # нужна инициализированная БД действий (idempotency binding + proposal).
    init_action_db(tmp_path)
    from services import auto_launch

    monkeypatch.setattr(
        auto_launch,
        "_AUTO_LAUNCH_STATE_FILE",
        tmp_path / "auto-launch-state.json",
    )
    monkeypatch.setattr(
        auto_launch,
        "_AUTO_LAUNCH_RUN_LOCK_FILE",
        tmp_path / "auto-launch.lock",
    )
    yield db_path
    ci.DB_PATH = None


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_case(
    db_path: str,
    *,
    suffix: str = "1",
    phase: str = "DISCOVERED",
    plan_phase: str = "MISSING",
) -> tuple[str, str]:
    case_id = f"case-{suffix}"
    plan_id = f"plan-{suffix}"
    created = datetime(2026, 7, 20, 12, tzinfo=LOCAL_TZ).isoformat()
    completed = datetime(2026, 7, 20, 10, tzinfo=LOCAL_TZ).isoformat()
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO launch_recovery_cases (
                case_id, tenant_id, trello_action_id, card_id, card_name,
                source_completed_at, scan_since, campaign_type, phase,
                account_kind, account_id, target_cities_json,
                missing_cities_json, expected_names_json,
                media_manifest_sha256, created_at, updated_at
            ) VALUES (?, 'default', ?, ?, 'Креатив', ?, ?, 'leadgen', ?,
                      'offline', '123', '["CityA"]', '["CityA"]',
                      '{"CityA":["CityA | Креатив"]}', ?, ?, ?)
            """,
            (
                case_id,
                f"action-{suffix}",
                f"card-{suffix}",
                completed,
                recovery.RECOVERY_CUTOFF.isoformat(),
                phase,
                MANIFEST,
                created,
                created,
            ),
        )
        conn.execute(
            """
            INSERT INTO launch_recovery_city_plans (
                plan_id, case_id, city, account_kind, account_id, adset_id,
                expected_ad_names_json, expected_ad_count, reconcile_from,
                reconcile_until, phase, media_manifest_sha256,
                evidence_json, created_at, updated_at
            ) VALUES (?, ?, 'CityA', 'offline', '123', '456',
                      '["CityA | Креатив"]', 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                case_id,
                datetime(2026, 7, 19, 10, tzinfo=LOCAL_TZ).isoformat(),
                created,
                plan_phase,
                MANIFEST,
                json.dumps(
                    {
                        "provider_media_sha256": PROVIDER_MEDIA_SHA,
                        "provider_media_type": "video",
                    },
                    ensure_ascii=False,
                ),
                created,
                created,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return case_id, plan_id


def _card(suffix: str = "1") -> dict:
    return {
        "id": f"card-{suffix}",
        "name": "Креатив",
        "desc": "",
        "dueComplete": True,
        "closed": False,
        "campaign_type": "leadgen",
        "labels": ["PRODA"],
    }


def _manifest() -> recovery.MediaManifest:
    return recovery.MediaManifest(
        manifest_sha256=MANIFEST,
        files=(recovery.MediaFileEvidence("asset.mp4", 3, "b" * 64),),
        expected_names_by_city={"CityA": ("CityA | Креатив",)},
        provider_media_sha256=PROVIDER_MEDIA_SHA,
        media_type="video",
    )


def _launch_manifest():
    from integrations.facebook import LaunchMediaFileEvidence, LaunchMediaManifest

    return LaunchMediaManifest(
        manifest_sha256="c" * 64,
        media_type="video",
        card_name_sha256=hashlib.sha256("Креатив".encode("utf-8")).hexdigest(),
        city="CityA",
        account_kind="offline",
        account_id="123",
        adset_id="456",
        expected_ad_names=("CityA | Креатив",),
        files=(LaunchMediaFileEvidence("asset.mp4", 3, "b" * 64),),
    )


def _live_adsets() -> dict[str, recovery.LiveAdsetDiscovery]:
    return {
        "offline": recovery.LiveAdsetDiscovery(
            account_kind="offline",
            account_id="123",
            leadgen={"CityA": {"L2": "456", "L1": "457"}},
            mql={"CityA": "458"},
        ),
        "online": recovery.LiveAdsetDiscovery(
            account_kind="online",
            account_id="999",
            leadgen={"Онлайн": {"L2": "956", "L1": "957"}},
            mql={},
        ),
    }


def _mark_approved(db_path: str, case_id: str, plan_id: str) -> None:
    conn = _connect(db_path)
    try:
        approved_at = datetime(2026, 7, 21, 11, tzinfo=LOCAL_TZ).isoformat()
        conn.execute(
            "UPDATE launch_recovery_cases SET approved_by = 'operator', approved_at = ? "
            "WHERE case_id = ?",
            (approved_at, case_id),
        )
        conn.execute(
            "UPDATE launch_recovery_city_plans SET approved_by = 'operator', "
            "approved_at = ? WHERE plan_id = ?",
            (approved_at, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _patch_fresh(monkeypatch, *, inventory=None, scope=("offline", "123", "456")):
    from integrations import trello

    monkeypatch.setattr(trello, "get_card", lambda _card_id: _card())
    monkeypatch.setattr(recovery, "build_media_manifest", lambda _card: _manifest())
    monkeypatch.setattr(
        recovery,
        "discover_live_recovery_adsets",
        lambda _kinds: _live_adsets(),
    )
    monkeypatch.setattr(
        recovery,
        "_resolve_city_scope",
        lambda _card, _city, *_live: scope,
    )
    monkeypatch.setattr(
        recovery,
        "fetch_complete_launch_inventory",
        lambda _kinds: (
            inventory if inventory is not None else {"offline": [], "online": []}
        ),
    )
    monkeypatch.setattr(
        apply,
        "_other_reserved_slots",
        lambda _adset_id, **_kwargs: 0,
    )
    monkeypatch.setattr(apply, "_hard_reserve_slots", lambda: 1)


def _patch_exact_card(monkeypatch):
    """Trello-чтение карточки в _load_apply_context — без сети."""
    from integrations import trello

    monkeypatch.setattr(trello, "get_card", lambda _card_id: _card())


def _patch_apply_gates(monkeypatch):
    monkeypatch.setattr(apply, "_validate_apply_config", lambda _kind: ({}, 1))
    monkeypatch.setattr(apply, "_normal_launch_slot_is_available", lambda _card: None)
    monkeypatch.setattr(apply, "_record_daily_launch_slot", lambda _card: None)
    monkeypatch.setattr(apply, "_acquire_launch_lease", lambda: object())
    monkeypatch.setattr(apply, "_release_launch_lease", lambda _lease: None)


def _patch_prepared_media_and_issuer(monkeypatch):
    from services import launch_repository

    monkeypatch.setattr(
        apply,
        "_build_prepared_recovery_media",
        lambda _context, _approved: (
            {"type": "video", "paths": ["asset.mp4"]},
            _manifest(),
            _launch_manifest(),
        ),
    )
    # Spy вызывает реальный repository reserve: тест не подменяет durable state.
    reserve = MagicMock(wraps=launch_repository.reserve_trusted_recovery_authorization)
    monkeypatch.setattr(
        launch_repository,
        "reserve_trusted_recovery_authorization",
        reserve,
    )
    return reserve


def _gateway_run(
    result: ActionResult,
    *,
    created_ids: tuple[str, ...] = (),
    stop_reason_code: str | None = None,
) -> ActionRun:
    execution = ActionExecution(
        attempt_id="attempt-recovery",
        item_id="item-recovery",
        item_index=0,
        action_manifest_id="manifest-recovery",
        result=result,
        started_at=NOW.astimezone(timezone.utc),
        completed_at=NOW.astimezone(timezone.utc),
        created_ids=created_ids,
        reason_code=stop_reason_code or result.value,
        remote_may_have_changed=result in {ActionResult.PARTIAL, ActionResult.UNKNOWN},
    )
    return ActionRun(
        operation_id="operation-recovery",
        idempotency_key=str(uuid.uuid4()),
        batch_manifest_id="batch-recovery",
        batch_manifest_sha256="d" * 64,
        state=OperationState(result.value),
        reviews=(),
        executions=(execution,),
        result=result,
        shadow_evaluation=None,
        shadow_items=(),
        dry_run=False,
        provider_mutation_count=1,
        first_unprocessed_index=None,
        stop_reason_code=stop_reason_code,
        reconciliation_required=result in {ActionResult.PARTIAL, ActionResult.UNKNOWN},
    )


def _patch_staged_gateway(
    monkeypatch,
    *,
    action_run: ActionRun,
):
    """Подменяет только I/O staging/gateway, сохраняя sealed вызов recovery."""
    prepared = SimpleNamespace(
        manifest_id="staged-recovery",
        destinations=(
            SimpleNamespace(
                city="CityA",
                adset_id="456",
                account_id="123",
                creatives=(SimpleNamespace(ad_name="CityA | Креатив"),),
            ),
        ),
    )
    action_manifest = SimpleNamespace(
        manifest_id="action-recovery",
        prepared_at=NOW.astimezone(timezone.utc),
    )
    stage = MagicMock(return_value=prepared)
    build = MagicMock(return_value=action_manifest)
    execute = MagicMock(return_value=action_run)
    release = MagicMock()
    monkeypatch.setattr("services.launch_staging.stage_launch", stage)
    monkeypatch.setattr("services.action_manifests.build_launch_manifest", build)
    monkeypatch.setattr("services.action_gateway.execute_action", execute)
    monkeypatch.setattr("services.launch_staging.release_staging", release)
    return stage, build, execute, release


def test_fresh_recheck_all_zero_commits_evidence_before_launching(
    isolated_db, monkeypatch
):
    _case_id, plan_id = _insert_case(isolated_db)
    _patch_fresh(monkeypatch)

    plan = apply.fresh_recheck_city_plan(plan_id, MANIFEST, NOW)

    assert plan.phase == "LAUNCHING"
    assert plan.reconcile_until == NOW
    assert plan.last_rechecked_at == NOW
    parsed_key = uuid.UUID(plan.launch_attempt_key)
    assert parsed_key.version == 4
    assert str(parsed_key) == plan.launch_attempt_key
    assert plan.evidence["inventory_complete"] is True


def test_fresh_recheck_zero_without_create_started_allows_controlled_retry(
    isolated_db,
    monkeypatch,
):
    """LAUNCHING до provider boundary остаётся retryable по exact zero snapshot."""
    case_id, plan_id = _insert_case(
        isolated_db,
        phase="LAUNCHING",
        plan_phase="LAUNCHING",
    )
    _mark_approved(isolated_db, case_id, plan_id)
    _patch_fresh(monkeypatch)

    plan = apply.fresh_recheck_city_plan(plan_id, MANIFEST, NOW)

    assert plan.phase == "LAUNCHING"
    assert plan.last_error is None
    assert not plan.evidence.get("provider_create_started_at")


def test_fresh_recheck_manifest_or_scope_drift_blocks_before_create(
    isolated_db, monkeypatch
):
    _case_id, plan_id = _insert_case(isolated_db)
    _patch_fresh(monkeypatch, scope=("offline", "999", "456"))

    plan = apply.fresh_recheck_city_plan(plan_id, MANIFEST, NOW)

    assert plan.phase == "BLOCKED"
    assert "scope_drift" in str(plan.last_error)


def test_wrong_approved_hash_blocks_before_any_provider_call(isolated_db, monkeypatch):
    _case_id, plan_id = _insert_case(isolated_db)
    provider = MagicMock()
    monkeypatch.setattr(recovery, "fetch_complete_launch_inventory", provider)

    plan = apply.fresh_recheck_city_plan(plan_id, "b" * 64, NOW)

    assert plan.phase == "BLOCKED"
    assert plan.last_error == "manifest_changed"
    provider.assert_not_called()


def test_fresh_recheck_partial_or_duplicate_never_launches(isolated_db, monkeypatch):
    _case_id, plan_id = _insert_case(isolated_db)
    duplicate = {
        "offline": [
            {
                "id": f"ad-{index}",
                "name": "CityA | Креатив",
                "account_kind": "offline",
                "account_id": "123",
                "adset_id": "456",
                "created_time": NOW.isoformat(),
            }
            for index in (1, 2)
        ],
        "online": [],
    }
    _patch_fresh(monkeypatch, inventory=duplicate)

    plan = apply.fresh_recheck_city_plan(plan_id, MANIFEST, NOW)

    assert plan.phase == "REVIEW_REQUIRED"
    assert plan.found_ad_ids == ()


def test_waiting_active_plan_is_skipped_without_new_proposal(
    isolated_db, monkeypatch
):
    """Уже активный план не порождает повторное предложение.

    Раньше защита от второго CREATE стояла на inventory-рекчеке внутри apply.
    Provider CREATE из apply удалён, поэтому «не сделать дважды» теперь означает
    «не предложить дважды»: план в WAITING_ACTIVE пропускается по фазе, до
    producer'а дело не доходит.
    """
    case_id, plan_id = _insert_case(
        isolated_db, phase="LAUNCHING", plan_phase="WAITING_ACTIVE"
    )
    _mark_approved(isolated_db, case_id, plan_id)
    _patch_exact_card(monkeypatch)
    _patch_apply_gates(monkeypatch)
    recorded = install_proposal_recorder(monkeypatch)

    result = apply.apply_recovery_case(case_id, MANIFEST, actor="operator")

    assert recorded.plans == []
    recorded.assert_no_direct_provider_mutation()
    assert result.skipped_plan_ids == (plan_id,)
    assert result.created_ad_ids_by_plan == {}


def test_repeated_apply_yields_one_proposal_never_second_create(
    isolated_db, monkeypatch
):
    """Повторный apply одного плана попадает в тот же proposal.

    Идемпотентность держится на стабильном scope `launch-recovery:{plan_id}`,
    поэтому крон/оператор могут повторять команду без риска отправить владельцу
    второе предложение и без единой провайдерской мутации.
    """
    _case_id, plan_id = _insert_case(isolated_db)
    _patch_exact_card(monkeypatch)
    _patch_apply_gates(monkeypatch)
    recorded = install_proposal_recorder(monkeypatch)

    first = apply.apply_recovery_city(plan_id, MANIFEST, actor="operator")
    second = apply.apply_recovery_city(plan_id, MANIFEST, actor="operator")

    # Один и тот же idempotency-ключ на оба вызова
    assert len({plan.idempotency_key for plan in recorded.plans}) == 1
    assert set(recorded.source_refs) == {f"launch-recovery:{plan_id}"}
    recorded.assert_no_direct_provider_mutation()
    assert first.errors and first.errors[0].startswith("OWNER_PROPOSAL_PENDING:")
    assert second.errors and second.errors[0].startswith("OWNER_PROPOSAL_PENDING:")
    assert first.created_ad_ids_by_plan == {}
    assert second.created_ad_ids_by_plan == {}


def test_apply_case_creates_exact_recovery_proposal_per_plan(isolated_db, monkeypatch):
    """apply_recovery_case создаёт по одному ASSET_RECOVERY-предложению на план.

    Проверяем именно adressность target: аккаунт, adset и город берутся из
    durable-плана, иначе execution boundary после одобрения не смог бы
    проверить область и мог бы создать объявление не там.
    """
    case_id, plan_id = _insert_case(isolated_db)
    _patch_exact_card(monkeypatch)
    _patch_apply_gates(monkeypatch)
    recorded = install_proposal_recorder(monkeypatch)

    result = apply.apply_recovery_case(case_id, MANIFEST, actor="operator")

    plan = recorded.assert_proposed(
        plan_id,
        kind=ProposalKind.ASSET_RECOVERY,
        origin=ProposalOrigin.RECOVERY,
        action_kind="RECOVER_AD",
    )
    target = plan.targets[0]
    assert target.account_id == "123"
    assert target.adset_id == "456"
    assert target.city == "CityA"
    assert plan.actor == "operator"
    recorded.assert_no_direct_provider_mutation()
    # Реальных ad_id нет — они появятся только после одобрения
    assert result.created_ad_ids_by_plan == {}
    assert result.skipped_plan_ids == (plan_id,)
    assert any(error.startswith("OWNER_PROPOSAL_PENDING:") for error in result.errors)


def test_execute_launch_proposes_without_staging_or_provider_io(
    isolated_db, monkeypatch
):
    """_execute_launch только предлагает: ни staging, ни gateway, ни Trello.

    Раньше эта функция проходила весь T6-путь (stage → manifest → gateway →
    запись ad_id → дневной слот). Теперь provider CREATE вынесен за границу
    одобрения, поэтому здесь обязано быть ровно одно предложение и ноль
    побочных эффектов: иначе владелец увидел бы «восстановлено» до решения.
    """
    case_id, plan_id = _insert_case(
        isolated_db, phase="LAUNCHING", plan_phase="LAUNCHING"
    )
    _mark_approved(isolated_db, case_id, plan_id)
    plan = apply._plan_from_row(apply._load_plan_row(plan_id))
    case = apply._load_case_row(case_id)
    context = apply._ApplyContext(case=case, plan=plan, card=_card())
    record_created = MagicMock()
    record_daily = MagicMock()
    stage = MagicMock()
    build = MagicMock()
    release = MagicMock()
    monkeypatch.setattr("services.launch_staging.stage_launch", stage)
    monkeypatch.setattr("services.action_manifests.build_launch_manifest", build)
    monkeypatch.setattr("services.launch_staging.release_staging", release)
    monkeypatch.setattr(apply, "_record_created_ids", record_created)
    monkeypatch.setattr(apply, "_record_daily_launch_slot", record_daily)
    recorded = install_proposal_recorder(monkeypatch)

    proposal_id = apply._execute_launch(context, MANIFEST, actor="operator")

    assert isinstance(proposal_id, str) and proposal_id
    proposed = recorded.assert_proposed(
        plan_id,
        kind=ProposalKind.ASSET_RECOVERY,
        origin=ProposalOrigin.RECOVERY,
        action_kind="RECOVER_AD",
    )
    payload = proposed.targets[0].intended_payload
    assert payload["operation"] == "RECOVER_AD"
    assert payload["recovery_kind"] == "LAUNCH_CITY"
    assert payload["case_id"] == case_id
    assert payload["plan_id"] == plan_id
    assert payload["card_id"] == "card-1"
    # Одобрение владельца привязано к КОНКРЕТНОМУ утверждённому манифесту
    assert payload["approved_manifest_sha256"] == MANIFEST

    stage.assert_not_called()
    build.assert_not_called()
    release.assert_not_called()
    record_created.assert_not_called()
    record_daily.assert_not_called()
    recorded.assert_no_direct_provider_mutation()


def test_new_completion_audit_between_approval_and_prepare_creates_nothing(
    isolated_db,
    monkeypatch,
):
    """Новый Trello-completion не превращает apply в создание объявлений.

    Раньше эта гонка ловилась гейтом newer_trello_completion прямо перед CREATE.
    CREATE из apply удалён, поэтому инвариант «ничего не создано» держится
    структурно; проверяем, что apply остаётся read-only producer'ом и не трогает
    ни провайдера, ни staging, даже когда карточка успела завершиться заново.
    """
    case_id, plan_id = _insert_case(
        isolated_db,
        phase="LAUNCHING",
        plan_phase="LAUNCHING",
    )
    _mark_approved(isolated_db, case_id, plan_id)
    plan = apply._plan_from_row(apply._load_plan_row(plan_id))
    case = apply._load_case_row(case_id)
    context = apply._ApplyContext(case=case, plan=plan, card=_card())
    newer = recovery.TrelloCompletion(
        action_id="action-newer",
        board_id="board-test",
        card_id="card-1",
        card_name="Креатив",
        completed_at=datetime(2026, 7, 21, 11, 30, tzinfo=LOCAL_TZ),
    )
    monkeypatch.setattr(
        recovery,
        "scan_completed_cards_since",
        lambda _since: [newer],
    )
    launch_single = MagicMock()
    monkeypatch.setattr("agent.launcher.launch_single", launch_single)
    recorded = install_proposal_recorder(monkeypatch)

    apply._execute_launch(context, MANIFEST, actor="operator")

    launch_single.assert_not_called()
    recorded.assert_no_direct_provider_mutation()
    assert len(recorded.plans) == 1
    # Фазы плана и кейса не «доезжают» до запущенных сами по себе
    assert apply._plan_from_row(apply._load_plan_row(plan_id)).phase == "LAUNCHING"
    assert str(apply._load_case_row(case_id)["phase"]) == "LAUNCHING"


def test_final_precreate_parent_phase_cas_blocks_create(isolated_db, monkeypatch):
    case_id, plan_id = _insert_case(
        isolated_db,
        phase="LAUNCHING",
        plan_phase="LAUNCHING",
    )
    _mark_approved(isolated_db, case_id, plan_id)
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "UPDATE launch_recovery_city_plans SET launch_attempt_key = ? WHERE plan_id = ?",
            ("recovery-launch-exact", plan_id),
        )
        conn.commit()
    finally:
        conn.close()
    plan = apply._plan_from_row(apply._load_plan_row(plan_id))
    case = apply._load_case_row(case_id)
    context = apply._ApplyContext(case=case, plan=plan, card=_card())
    monkeypatch.setattr(
        recovery,
        "scan_completed_cards_since",
        lambda _since: [
            recovery.TrelloCompletion(
                action_id="action-1",
                board_id="board-test",
                card_id="card-1",
                card_name="Креатив",
                completed_at=datetime(2026, 7, 20, 10, tzinfo=LOCAL_TZ),
            )
        ],
    )
    monkeypatch.setattr(
        recovery,
        "discover_live_recovery_adsets",
        lambda _kinds: _live_adsets(),
    )
    monkeypatch.setattr(
        apply,
        "_current_scope_and_manifest",
        lambda _case, _plan, *_live: (_card(), _manifest()),
    )
    monkeypatch.setattr(
        apply,
        "_fresh_inventory",
        lambda _plan, _now: ("MISSING", (), 10),
    )
    monkeypatch.setattr(
        apply,
        "_other_reserved_slots",
        lambda _adset_id, **_kwargs: 0,
    )
    monkeypatch.setattr(apply, "_hard_reserve_slots", lambda: 1)
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "UPDATE launch_recovery_cases SET phase = 'BLOCKED' WHERE case_id = ?",
            (case_id,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(apply.RecoveryApplyBlocked, match="parent_pre_create_cas"):
        apply._final_precreate_recheck_and_bind(
            context,
            MANIFEST,
            _launch_manifest(),
        )


def test_apply_is_read_only_and_does_no_live_discovery(isolated_db, monkeypatch):
    """Producer не делает live-I/O: область проверит execution boundary.

    Раньше apply сам поднимал live-adsets и падал в BLOCKED, если discovery не
    живое. Теперь apply только фиксирует предложение по durable-плану, поэтому
    он обязан не дёргать FB вовсе — иначе недоступность Facebook блокировала бы
    саму возможность спросить владельца.
    """
    case_id, plan_id = _insert_case(isolated_db)
    _patch_exact_card(monkeypatch)
    _patch_apply_gates(monkeypatch)
    discovery = MagicMock(
        side_effect=recovery.RecoveryReviewRequired("adset_discovery_not_live")
    )
    monkeypatch.setattr(recovery, "discover_live_recovery_adsets", discovery)
    recorded = install_proposal_recorder(monkeypatch)

    result = apply.apply_recovery_case(case_id, MANIFEST, actor="operator")

    discovery.assert_not_called()
    recorded.assert_no_direct_provider_mutation()
    # Область действия зафиксирована в target предложения, а не прочитана из FB
    target = recorded.assert_proposed(
        plan_id,
        kind=ProposalKind.ASSET_RECOVERY,
        origin=ProposalOrigin.RECOVERY,
        action_kind="RECOVER_AD",
    ).targets[0]
    assert (target.account_id, target.adset_id) == ("123", "456")
    assert result.created_ad_ids_by_plan == {}


def test_concurrent_apply_race_creates_single_proposal(isolated_db, monkeypatch):
    """Гонка двух apply по одному плану даёт ровно одно предложение.

    Инвариант «один план — одно действие» сохранён: раньше он защищал от двух
    CREATE, теперь — от двух предложений владельцу. Ключ идемпотентности один и
    тот же, поэтому второй поток попадает в уже созданное предложение.
    """
    _case_id, plan_id = _insert_case(isolated_db)
    _patch_exact_card(monkeypatch)
    _patch_apply_gates(monkeypatch)
    recorded = install_proposal_recorder(monkeypatch)
    keys_lock = threading.Lock()
    keys: list[str] = []

    def apply_once(_index):
        result = apply.apply_recovery_city(plan_id, MANIFEST, actor="operator")
        with keys_lock:
            keys.append(result.errors[0] if result.errors else "")
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply_once, range(2)))

    assert len({plan.idempotency_key for plan in recorded.plans}) == 1
    recorded.assert_no_direct_provider_mutation()
    assert all(result.created_ad_ids_by_plan == {} for result in results)
    assert all(key.startswith("OWNER_PROPOSAL_PENDING:") for key in keys)
    # Фаза плана не переводится в активную без одобрения владельца
    assert apply._plan_from_row(apply._load_plan_row(plan_id)).phase == "MISSING"


def test_apply_never_calls_cleaner_and_never_creates(isolated_db, monkeypatch):
    """apply не запускает cleaner и не создаёт объявления ни при каких условиях.

    Самая опасная ветка старого кода: при нехватке слотов можно было соблазниться
    почистить adset. Проверка ёмкости ушла за границу одобрения, а cleaner
    (необратимое удаление) обязан остаться нетронутым — даже если в adset
    свободен всего один слот.
    """
    case_id, plan_id = _insert_case(isolated_db)
    _patch_exact_card(monkeypatch)
    _patch_apply_gates(monkeypatch)
    cleaner = MagicMock()
    monkeypatch.setattr("services.adset_cleaner.run_cleaner", cleaner)
    recorded = install_proposal_recorder(monkeypatch)

    result = apply.apply_recovery_case(case_id, MANIFEST, actor="operator")

    cleaner.assert_not_called()
    recorded.assert_no_direct_provider_mutation()
    assert result.created_ad_ids_by_plan == {}
    assert [target.subject_id for plan in recorded.plans for target in plan.targets] == [
        plan_id
    ]


def test_one_recovery_case_per_local_day_is_transactional(isolated_db):
    case_1, _plan_1 = _insert_case(isolated_db, suffix="1")
    case_2, _plan_2 = _insert_case(isolated_db, suffix="2")

    apply._claim_case_approval(case_1, MANIFEST, "operator", NOW, 1)
    with pytest.raises(apply.RecoveryApplyBlocked, match="daily_case_cap"):
        apply._claim_case_approval(case_2, MANIFEST, "operator", NOW, 1)


def test_cli_default_is_audit_only_and_apply_flag_alone_is_insufficient(monkeypatch):
    from scripts import recover_launches_since as cli

    audit = MagicMock(
        return_value=recovery.LaunchRecoverySummary(
            audit_run_id="audit-1",
            since=recovery.RECOVERY_CUTOFF,
            audited_at=NOW,
            discovered_cards=0,
            no_action_cases=0,
            review_required_cases=0,
            missing_city_plans=0,
            case_ids=(),
            errors=(),
        )
    )
    apply_case = MagicMock()
    monkeypatch.setattr(cli, "audit_missing_launches", audit)
    monkeypatch.setattr(cli, "apply_recovery_case", apply_case)

    assert cli.main([]) == 0
    assert cli.main(["--apply"]) == 2
    audit.assert_called_once()
    apply_case.assert_not_called()


def test_new_manifest_persists_provider_canonical_sha(tmp_path, monkeypatch):
    from integrations import facebook

    media_path = tmp_path / "asset.mp4"
    media_path.write_bytes(b"exact-media")
    card = {
        "id": "card-canonical",
        "name": "Креатив",
        "desc": "",
        "campaign_type": "leadgen",
        "cities": ["CityA"],
        "labels": ["PRODA"],
        "media": {"type": "video", "paths": [str(media_path)]},
    }
    manifest = recovery.build_media_manifest(card)
    monkeypatch.setattr(
        recovery,
        "_resolve_city_scope",
        lambda *_args, **_kwargs: ("offline", "123", "456"),
    )
    completion = recovery.TrelloCompletion(
        action_id="canonical-action",
        board_id="board-test",
        card_id="card-canonical",
        card_name="Креатив",
        completed_at=NOW - timedelta(days=1),
    )
    plan = recovery.build_recovery_city_plans(
        completion,
        manifest,
        {"offline": [], "online": []},
        NOW,
        card=card,
    )[0]

    assert manifest.provider_media_sha256 == facebook.calculate_launch_media_sha256(
        card["media"]
    )
    assert plan.provider_media_sha256 == manifest.provider_media_sha256
    assert plan.evidence["provider_media_type"] == "video"


def test_placement_pair_provider_sha_uses_exact_provider_order(tmp_path):
    from integrations import facebook

    paths = {}
    for name in ("a-feed.png", "a-story.png", "b-feed.png", "b-story.png"):
        path = tmp_path / name
        path.write_bytes(name.encode("utf-8"))
        paths[name] = str(path)
    media = {
        "type": "placement_pairs",
        "paths": [
            {
                "label": "b",
                "feed": paths["b-feed.png"],
                "story": paths["b-story.png"],
            },
            {
                "label": "a",
                "feed": paths["a-feed.png"],
                "story": paths["a-story.png"],
            },
        ],
        "singles": [],
    }
    card = {
        "id": "card-pairs",
        "name": "Креатив",
        "desc": "",
        "campaign_type": "leadgen",
        "cities": ["CityA"],
        "labels": ["PRODA"],
        "media": media,
    }

    manifest = recovery.build_media_manifest(card)

    assert manifest.provider_media_sha256 == facebook.calculate_launch_media_sha256(
        media
    )


def test_legacy_plan_without_provider_sha_fails_closed_before_live_reads(
    isolated_db,
    monkeypatch,
):
    _case_id, plan_id = _insert_case(isolated_db)
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "UPDATE launch_recovery_city_plans SET evidence_json = '{}' WHERE plan_id = ?",
            (plan_id,),
        )
        conn.commit()
    finally:
        conn.close()
    live_inventory = MagicMock()
    monkeypatch.setattr(recovery, "fetch_complete_launch_inventory", live_inventory)

    plan = apply.fresh_recheck_city_plan(plan_id, MANIFEST, NOW)

    assert plan.phase == "BLOCKED"
    assert plan.last_error == "legacy_recovery_media_sha_requires_audit"
    live_inventory.assert_not_called()


def test_trusted_recovery_hook_issues_ordinary_provider_proof_atomically(
    isolated_db,
    monkeypatch,
):
    from services.launch_checker import ProviderLaunchAuthorization

    case_id, plan_id = _insert_case(
        isolated_db,
        phase="LAUNCHING",
        plan_phase="LAUNCHING",
    )
    _mark_approved(isolated_db, case_id, plan_id)
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "UPDATE launch_recovery_city_plans SET launch_attempt_key = ? WHERE plan_id = ?",
            ("recovery-launch-trusted", plan_id),
        )
        conn.commit()
    finally:
        conn.close()
    plan = apply._plan_from_row(apply._load_plan_row(plan_id))
    context = apply._ApplyContext(
        case=apply._load_case_row(case_id),
        plan=plan,
        card=_card(),
    )
    real_other_reserved_slots = apply._other_reserved_slots
    _patch_fresh(monkeypatch)
    monkeypatch.setattr(
        recovery,
        "scan_completed_cards_since",
        lambda _since: [
            recovery.TrelloCompletion(
                action_id="action-1",
                board_id="board-test",
                card_id="card-1",
                card_name="Креатив",
                completed_at=NOW - timedelta(days=1),
            )
        ],
    )
    apply._final_precreate_recheck_and_bind(context, MANIFEST, _launch_manifest())

    proof = apply._issue_trusted_recovery_proof(
        context,
        MANIFEST,
        PROVIDER_MEDIA_SHA,
        "operator",
    )
    monkeypatch.setattr(
        "integrations.facebook._get_other_launch_reserved_slots",
        lambda _workflow_id, _adset_id: 0,
    )
    monkeypatch.setattr(
        apply,
        "_other_reserved_slots",
        real_other_reserved_slots,
    )
    # Собственная reservation вычитается, а остальные продолжили бы учитываться.
    apply._final_precreate_recheck_and_bind(
        context,
        MANIFEST,
        _launch_manifest(),
        authorization_reserved=True,
    )

    assert isinstance(proof, ProviderLaunchAuthorization)
    conn = _connect(isolated_db)
    try:
        auth = conn.execute(
            "SELECT source, recovery_plan_id, media_sha256, phase "
            "FROM launch_authorizations WHERE auth_id = ?",
            (proof.auth_id,),
        ).fetchone()
    finally:
        conn.close()
    assert tuple(auth) == (
        "RECOVERY",
        plan_id,
        PROVIDER_MEDIA_SHA,
        "RESERVED",
    )


def test_producer_refusal_becomes_durable_recovery_block(
    isolated_db,
    monkeypatch,
):
    """Отказ producer'а — это блок, а не тихий пропуск.

    Раньше drift ловился по stop_reason_code от gateway. Теперь единственный
    способ «не получилось» — producer не создал proposal; тогда apply обязан
    поднять RecoveryApplyBlocked и НЕ двигать фазу плана вперёд.
    """
    case_id, plan_id = _insert_case(
        isolated_db,
        phase="LAUNCHING",
        plan_phase="LAUNCHING",
    )
    _mark_approved(isolated_db, case_id, plan_id)
    context = apply._ApplyContext(
        case=apply._load_case_row(case_id),
        plan=apply._plan_from_row(apply._load_plan_row(plan_id)),
        card=_card(),
    )
    guard = install_provider_mutation_guard(monkeypatch)
    monkeypatch.setattr(
        "services.action_producer_gateway.propose_asset_recovery",
        lambda **_kwargs: blocked_outcome("RECOVERY_PLAN_DRIFT"),
    )

    with pytest.raises(
        apply.RecoveryApplyBlocked,
        match="recovery_proposal_missing",
    ):
        apply._execute_launch(context, MANIFEST, actor="operator")

    guard.assert_untouched()
    durable = apply._plan_from_row(apply._load_plan_row(plan_id))
    assert durable.phase == "LAUNCHING"


def test_producer_exception_blocks_apply_without_side_effects(
    isolated_db,
    monkeypatch,
):
    """Исключение producer'а не оставляет полу-применённого состояния.

    Раньше эта ситуация означала «CREATE начался, нужен ручной разбор». Теперь
    провайдера никто не трогал, поэтому ручной reconcile не нужен — важно лишь,
    что ошибка не проглочена и durable-фаза плана не изменилась.
    """
    case_id, plan_id = _insert_case(
        isolated_db,
        phase="LAUNCHING",
        plan_phase="LAUNCHING",
    )
    _mark_approved(isolated_db, case_id, plan_id)
    _patch_exact_card(monkeypatch)
    _patch_apply_gates(monkeypatch)
    guard = install_provider_mutation_guard(monkeypatch)
    monkeypatch.setattr(
        "services.action_producer_gateway.propose_asset_recovery",
        MagicMock(side_effect=RuntimeError("owner store down")),
    )

    with pytest.raises(RuntimeError, match="owner store down"):
        apply.apply_recovery_city(plan_id, MANIFEST, actor="operator")

    guard.assert_untouched()
    durable = apply._plan_from_row(apply._load_plan_row(plan_id))
    assert durable.phase == "LAUNCHING"
    assert str(apply._load_case_row(case_id)["phase"]) == "LAUNCHING"
