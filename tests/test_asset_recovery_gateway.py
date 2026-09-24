"""Security contract отдельного existing-creative approval gateway."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from integrations.facebook import (
    AssetRecoveryCreateNotStarted,
    _execute_asset_recovery_manifest_unchecked,
    create_ad_from_existing_creative,
)
from services.action_manifests import build_action_batch
from services.approval_audit import (
    ApprovalAuditCorruptionError,
    ApprovalPermitInvalidError,
    find_operation,
    reserve_operation,
    verify_attempt_attestation,
)
from services.approval_checker_models import (
    ActionKind,
    ActionOrigin,
    AssetRecoveryManifest,
    OperationAttemptAttestation,
    SourceSystem,
    manifest_sha256,
)
from services.owner_action_models import (
    ActionAttemptAttestation as OwnerActionAttemptAttestation,
)
from integrations.facebook_ads_mutation_transport import AttestationRejected
from services.approval_sources import _action_request
from services.approval_source_facebook import (
    asset_recovery_source_sha256,
    read_action_postcondition,
    read_action_precondition,
)
from services.ad_asset_recovery import (
    PreparedAsset,
    ProductionRecoveryBackend,
    RecoveryGatewayRequired,
    RecoveryLedger,
    RecoveryManifest,
    recover_one,
)
from services.adset_pause_guard import adset_mutation_locks
from services.asset_recovery_repository import (
    AssetRecoveryRepositoryError,
    claim_create,
    get_attempt_state,
    reserve_authorization,
)
from services.creative_intelligence import init_kb


NOW = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _manifest() -> AssetRecoveryManifest:
    idempotency_key = str(uuid.uuid4())
    return AssetRecoveryManifest(
        kind=ActionKind.ASSET_RECOVERY,
        manifest_id=str(uuid.uuid4()),
        origin=ActionOrigin.ASSET_RECOVERY,
        idempotency_key=idempotency_key,
        prepared_at=NOW,
        account_kind="offline",
        account_id="10001",
        campaign_type="asset_recovery",
        source="RECOVERY",
        city="CityB",
        source_ad_id="20001",
        source_adset_id="30001",
        source_adset_name="L1 CityD",
        adset_type="L1",
        source_ad_name="CityD | Exact creative [PRODB]",
        source_creative_id="40001",
        source_identity_sha256=SHA_A,
        target_adset_id="30002",
        target_adset_name="L1 CityB",
        target_ad_name="CityB | Exact creative [PRODB]",
        target_identity_key="cityb | exact creative",
        pre_inventory_sha256=SHA_B,
        capacity_available=2,
        hard_reserve_slots=1,
    )


def _attestation(manifest: AssetRecoveryManifest) -> OperationAttemptAttestation:
    return OperationAttemptAttestation(
        operation_id=str(uuid.uuid4()),
        idempotency_key=manifest.idempotency_key,
        batch_manifest_id=str(uuid.uuid4()),
        batch_manifest_sha256="c" * 64,
        permit_id=str(uuid.uuid4()),
        attempt_id=str(uuid.uuid4()),
        item_id=manifest.manifest_id,
        item_index=0,
        action_kind=manifest.kind,
        item_manifest_sha256=manifest_sha256(manifest),
        precondition_sha256="d" * 64,
        subject_ids=(f"adset:{manifest.target_adset_id}",),
        attempted_at=NOW,
    )


def _owner_attestation(
    manifest: AssetRecoveryManifest,
) -> OwnerActionAttemptAttestation:
    return OwnerActionAttemptAttestation(
        attempt_id=str(uuid.uuid4()),
        permit_id=str(uuid.uuid4()),
        proposal_id=str(uuid.uuid4()),
        decision_id=str(uuid.uuid4()),
        claim_id=str(uuid.uuid4()),
        operation_kind="RECOVER_AD",
        account_id=manifest.account_id,
        resource_id=manifest.target_adset_id,
        payload_sha256="e" * 64,
        consumed_at=NOW,
    )


@pytest.fixture
def recovery_db(tmp_path):
    path = tmp_path / "gateway.db"
    init_kb(str(path))
    return path


def test_asset_recovery_requests_only_force_live_facebook() -> None:
    manifest = _manifest()
    request = _action_request(manifest, NOW)

    assert request.required_sources == (SourceSystem.FACEBOOK,)
    assert request.force_live is True
    assert request.include_full_inventory is True
    assert request.card_ids == ()
    assert request.staged_relative_paths == ()


def test_asset_recovery_manifest_rejects_non_uuid4_and_identity_drift() -> None:
    manifest = _manifest()
    values = {
        field: getattr(manifest, field)
        for field in manifest.__dataclass_fields__
    }
    values["idempotency_key"] = str(uuid.uuid1())
    with pytest.raises(ValueError, match="UUID4"):
        AssetRecoveryManifest(**values)

    values["idempotency_key"] = str(uuid.uuid4())
    values["target_identity_key"] = "tampered"
    with pytest.raises(ValueError, match="target_identity_key"):
        AssetRecoveryManifest(**values)


def test_wal_attestation_tamper_is_rejected(tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "approval.jsonl"
    monkeypatch.setattr("config.REPORT_CHECKER_AUDIT_PATH", audit_path)
    manifest = _manifest()
    batch = build_action_batch(
        (manifest,),
        correlation_id=manifest.manifest_id,
        idempotency_key=manifest.idempotency_key,
        now=NOW,
    )
    reserve_operation(batch, NOW, audit_path)
    attestation = _attestation(manifest)

    with pytest.raises(ApprovalPermitInvalidError):
        verify_attempt_attestation(attestation, audit_path)


def test_sql_authorization_is_single_use_and_claimed_before_post(recovery_db) -> None:
    manifest = _manifest()
    attempt = _attestation(manifest)
    with patch(
        "services.asset_recovery_repository.verify_attempt_attestation"
    ) as verify:
        authorization = reserve_authorization(manifest, attempt, NOW)
    verify.assert_called_once_with(attempt)
    claim_create(authorization, manifest, NOW)
    assert get_attempt_state(attempt.attempt_id)["phase"] == "CREATE_STARTED"

    with pytest.raises(AssetRecoveryRepositoryError, match="RESERVED"):
        claim_create(authorization, manifest, NOW)
    with patch(
        "services.asset_recovery_repository.verify_attempt_attestation"
    ), pytest.raises(AssetRecoveryRepositoryError, match="повторно"):
        reserve_authorization(manifest, attempt, NOW)


def test_provider_create_uses_typed_owner_attestation(recovery_db) -> None:
    del recovery_db
    manifest = _manifest()
    attempt = _owner_attestation(manifest)

    with (
        patch("integrations.facebook.get_fb_account_id", return_value="10001"),
        patch(
            "integrations.facebook_ads_mutation_transport.create_ad",
            return_value="90001",
        ) as typed_create,
    ):
        ad_id = _execute_asset_recovery_manifest_unchecked(manifest, attempt)

    assert ad_id == "90001"
    typed_create.assert_called_once_with(
        attempt,
        account_id=attempt.account_id,
        adset_id=manifest.target_adset_id,
        name=manifest.target_ad_name,
        creative={"creative_id": manifest.source_creative_id},
        payload_sha256=attempt.payload_sha256,
        url_tags=(
            "utm_source=facebook&utm_medium=cpc&"
            "utm_content={{ad.id}}&utm_campaign={{campaign.name}}"
        ),
    )


def test_invalid_wal_attestation_causes_zero_graph_posts(recovery_db) -> None:
    del recovery_db
    manifest = _manifest()
    attempt = _owner_attestation(manifest)
    with (
        patch("integrations.facebook.get_fb_account_id", return_value="10001"),
        patch(
            "integrations.facebook_ads_mutation_transport.create_ad",
            side_effect=AttestationRejected("ATTESTATION_NOT_PERSISTED"),
        ) as typed_create,
        pytest.raises(AssetRecoveryCreateNotStarted),
    ):
        _execute_asset_recovery_manifest_unchecked(manifest, attempt)
    typed_create.assert_called_once()


def test_migration_stores_no_plaintext_secret(recovery_db) -> None:
    manifest = _manifest()
    attempt = _attestation(manifest)
    with patch("services.asset_recovery_repository.verify_attempt_attestation"):
        authorization = reserve_authorization(manifest, attempt, NOW)
    with sqlite3.connect(recovery_db) as connection:
        stored = connection.execute(
            "SELECT secret_sha256 FROM asset_recovery_authorizations WHERE attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()[0]
    assert stored != authorization.secret
    assert stored == hashlib.sha256(authorization.secret.encode()).hexdigest()


def _source_row(manifest: AssetRecoveryManifest) -> dict[str, str]:
    return {
        "ad_id": manifest.source_ad_id,
        "name": manifest.source_ad_name,
        "account_id": manifest.account_id,
        "adset_id": manifest.source_adset_id,
        "adset_name": manifest.source_adset_name,
        "status": "ACTIVE",
        "effective_status": "ACTIVE",
        "creative_id": manifest.source_creative_id,
    }


def _target_info(
    manifest: AssetRecoveryManifest,
    *,
    adset_status: str = "ACTIVE",
    ads: list[dict] | None = None,
) -> dict:
    rows = list(ads or [])
    return {
        "adset_id": manifest.target_adset_id,
        "account_id": manifest.account_id,
        "name": manifest.target_adset_name,
        "adset_effective_status": adset_status,
        "inventory_complete": True,
        "unknown_effective_status_ids": [],
        "ads": rows,
        "ad_count": len(rows),
        "effective_active_count": sum(
            row.get("effective_status") == "ACTIVE" for row in rows
        ),
    }


def test_exact_active_postcondition_requires_same_creative_id() -> None:
    draft = _manifest()
    source = _source_row(draft)
    manifest = replace(
        draft,
        source_identity_sha256=asset_recovery_source_sha256(source),
    )
    target = _target_info(
        manifest,
        ads=[{
            "id": "90001",
            "name": manifest.target_ad_name,
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "creative": {"id": manifest.source_creative_id},
        }],
    )
    with (
        patch("services.approval_source_facebook.get_fb_account_id", return_value="10001"),
        patch(
            "integrations.facebook.get_existing_ad_creative_source",
            return_value=source,
        ),
        patch("integrations.facebook.get_adset_info", return_value=target),
    ):
        observation = read_action_postcondition(manifest, ("90001",), NOW)
    assert observation.target_state == "ACTIVE:90001"

    target["ads"][0]["creative"] = {"id": "99999"}
    with (
        patch("services.approval_source_facebook.get_fb_account_id", return_value="10001"),
        patch("integrations.facebook.get_existing_ad_creative_source", return_value=source),
        patch("integrations.facebook.get_adset_info", return_value=target),
        pytest.raises(Exception, match="POSTCONDITION_DRIFT"),
    ):
        read_action_postcondition(manifest, ("90001",), NOW)


def test_paused_target_adset_blocks_precondition() -> None:
    draft = _manifest()
    source = _source_row(draft)
    manifest = replace(
        draft,
        source_identity_sha256=asset_recovery_source_sha256(source),
    )
    target = _target_info(manifest, adset_status="PAUSED")
    with (
        patch("services.approval_source_facebook.get_fb_account_id", return_value="10001"),
        patch("integrations.facebook.get_existing_ad_creative_source", return_value=source),
        patch("integrations.facebook.get_adset_info", return_value=target),
        pytest.raises(Exception, match="TARGET_INCOMPLETE"),
    ):
        read_action_precondition(manifest, NOW)


def test_wrong_attestation_is_blocked_before_provider_network(recovery_db) -> None:
    del recovery_db
    manifest = _manifest()
    attempt = _attestation(manifest)
    with (
        patch("integrations.facebook.get_fb_account_id", return_value="10001"),
        patch(
            "integrations.facebook_ads_mutation_transport.create_ad",
            side_effect=AttestationRejected("ATTESTATION_TYPE_INVALID"),
        ) as typed_create,
        pytest.raises(AssetRecoveryCreateNotStarted),
    ):
        _execute_asset_recovery_manifest_unchecked(manifest, attempt)
    typed_create.assert_not_called()


def test_hard_reserve_blocks_manifest_before_any_post() -> None:
    manifest = _manifest()
    values = {
        field: getattr(manifest, field)
        for field in manifest.__dataclass_fields__
    }
    values["capacity_available"] = 1
    with patch(
        "integrations.facebook_ads_mutation_transport.create_ad"
    ) as graph_post, pytest.raises(
        ValueError, match="обязательный резерв"
    ):
        AssetRecoveryManifest(**values)
    graph_post.assert_not_called()


def test_wal_manifest_payload_cannot_be_rehashed_away(tmp_path, monkeypatch) -> None:
    audit_path = tmp_path / "approval.jsonl"
    monkeypatch.setattr("config.REPORT_CHECKER_AUDIT_PATH", audit_path)
    manifest = _manifest()
    batch = build_action_batch(
        (manifest,),
        correlation_id=manifest.manifest_id,
        idempotency_key=manifest.idempotency_key,
        now=NOW,
    )
    reserve_operation(batch, NOW, audit_path)
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    metadata = payload["item_metadata"][0]
    metadata["action_manifest_payload"]["target_adset_id"] = "39999"
    encoded = json.dumps(
        metadata["action_manifest_payload"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    metadata["action_manifest_payload_sha256"] = hashlib.sha256(encoded).hexdigest()
    audit_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(ApprovalAuditCorruptionError, match="item_manifest_sha256"):
        find_operation(manifest.idempotency_key, audit_path)


def test_real_mutation_locks_use_numeric_source_target_order(monkeypatch) -> None:
    events: list[tuple[str, str]] = []

    @contextmanager
    def fake_lock(adset_id: str):
        events.append(("enter", adset_id))
        try:
            yield
        finally:
            events.append(("exit", adset_id))

    monkeypatch.setattr("services.adset_pause_guard.adset_mutation_lock", fake_lock)
    with adset_mutation_locks(("30002", "29999")):
        events.append(("inside", ""))
    assert events == [
        ("enter", "29999"),
        ("enter", "30002"),
        ("inside", ""),
        ("exit", "30002"),
        ("exit", "29999"),
    ]


def _legacy_recovery_manifest() -> RecoveryManifest:
    return RecoveryManifest.from_dict({
        "account_kind": "offline",
        "account_id": "10001",
        "card_id": "card-1",
        "card_title": "Exact creative",
        "media_basename": "creative.mp4",
        "city": "CityB",
        "adset_id": "30002",
        "expected_adset_name": "L1 CityB",
        "adset_type": "L1",
        "expected_ad_name": "CityB | Exact creative",
        "sibling_expected_ad_name": "",
        "product": "PRODB",
        "window_start": "2026-07-22T00:00:00+00:00",
        "window_end": "2026-07-22T23:59:59+00:00",
        "drive_url": None,
        "source_ad_id": "20001",
        "source_adset_id": "30001",
        "expected_source_adset_name": "L1 CityD",
        "name_mode": "card_only",
    })


def _legacy_inventory(manifest: RecoveryManifest, ads: list[dict]) -> dict:
    return {
        "adset_id": manifest.adset_id,
        "name": manifest.expected_adset_name,
        "adset_effective_status": "ACTIVE",
        "inventory_complete": True,
        "unknown_effective_status_ids": [],
        "ads": ads,
        "ad_count": len(ads),
        "effective_active_count": sum(
            row.get("effective_status") == "ACTIVE" for row in ads
        ),
    }


def test_gateway_unknown_cannot_be_promoted_by_legacy_reconcile(tmp_path) -> None:
    manifest = _legacy_recovery_manifest()
    backend = ProductionRecoveryBackend()
    inventory = _legacy_inventory(
        manifest,
        [{
            "id": "80001",
            "name": "Other active",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "created_time": "2026-07-22T10:00:00+0000",
            "creative": {"id": "40001"},
        }],
    )
    ledger = RecoveryLedger(tmp_path / "ledger.json")
    prepared = PreparedAsset(
        "existing_creative",
        "",
        creative_id="40001",
        source_name="CityD | Exact creative",
        gateway_idempotency_key=str(uuid.uuid4()),
    )
    with (
        patch.object(backend, "current_account_id", return_value=manifest.account_id),
        patch.object(backend, "now", return_value=NOW),
        patch.object(backend, "get_adset_info", return_value=inventory),
        patch.object(backend, "prepare_asset", return_value=prepared),
        patch.object(backend, "revalidate_prepared"),
        patch.object(
            backend,
            "create_asset",
            side_effect=RecoveryGatewayRequired(str(uuid.uuid4())),
        ),
    ):
        result = recover_one(manifest, backend=backend, ledger=ledger)
    assert result.status == "BLOCKED"
    assert result.reason == "asset_recovery_gateway_reconciliation_required"
    assert ledger.get(manifest.key)["phase"] == "BLOCKED"


def test_preexisting_wrong_creative_is_never_accepted(tmp_path) -> None:
    manifest = _legacy_recovery_manifest()
    backend = ProductionRecoveryBackend()
    exact = {
        "id": "90001",
        "name": manifest.expected_ad_name,
        "status": "ACTIVE",
        "effective_status": "ACTIVE",
        "created_time": "2026-07-22T10:00:00+0000",
        "creative": {"id": "wrong-creative"},
    }
    inventory = _legacy_inventory(manifest, [exact])
    ledger = RecoveryLedger(tmp_path / "ledger.json")
    with (
        patch.object(backend, "current_account_id", return_value=manifest.account_id),
        patch.object(backend, "now", return_value=NOW),
        patch.object(backend, "get_adset_info", return_value=inventory),
        patch.object(backend, "confirm_existing_target", return_value=False),
        patch.object(backend, "prepare_asset") as prepare,
    ):
        result = recover_one(manifest, backend=backend, ledger=ledger)
    assert result.status == "BLOCKED"
    assert result.reason == "existing_target_creative_mismatch"
    prepare.assert_not_called()


def test_appeared_before_create_wrong_creative_is_blocked(tmp_path) -> None:
    manifest = _legacy_recovery_manifest()
    backend = ProductionRecoveryBackend()
    initial = _legacy_inventory(
        manifest,
        [{
            "id": "80001",
            "name": "Other active",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "created_time": "2026-07-22T10:00:00+0000",
            "creative": {"id": "40001"},
        }],
    )
    appeared = _legacy_inventory(
        manifest,
        [{
            "id": "90001",
            "name": manifest.expected_ad_name,
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "created_time": "2026-07-22T10:00:00+0000",
            "creative": {"id": "wrong-creative"},
        }],
    )
    prepared = PreparedAsset(
        "existing_creative",
        "",
        creative_id="40001",
        source_name="CityD | Exact creative",
        gateway_idempotency_key=str(uuid.uuid4()),
    )
    ledger = RecoveryLedger(tmp_path / "ledger.json")
    with (
        patch.object(backend, "current_account_id", return_value=manifest.account_id),
        patch.object(backend, "now", return_value=NOW),
        patch.object(backend, "get_adset_info", side_effect=[initial, appeared]),
        patch.object(backend, "prepare_asset", return_value=prepared),
        patch.object(backend, "confirm_existing_target", return_value=False),
        patch.object(backend, "create_asset") as create,
    ):
        result = recover_one(manifest, backend=backend, ledger=ledger)
    assert result.status == "BLOCKED"
    assert result.reason == "appeared_target_creative_mismatch"
    create.assert_not_called()


def test_public_existing_creative_create_is_fail_closed() -> None:
    with patch(
        "integrations.facebook_ads_mutation_transport.create_ad"
    ) as graph_post, pytest.raises(
        AssetRecoveryCreateNotStarted, match="gateway_required"
    ):
        create_ad_from_existing_creative("name", "30002", "40001")
    graph_post.assert_not_called()


def test_target_account_mismatch_blocks_live_evidence() -> None:
    draft = _manifest()
    source = _source_row(draft)
    manifest = replace(
        draft,
        source_identity_sha256=asset_recovery_source_sha256(source),
    )
    target = _target_info(manifest)
    target["account_id"] = "99999"
    with (
        patch("services.approval_source_facebook.get_fb_account_id", return_value="10001"),
        patch("services.ad_asset_recovery._asset_recovery_hard_reserve_slots", return_value=1),
        patch("integrations.facebook.get_existing_ad_creative_source", return_value=source),
        patch("integrations.facebook.get_adset_info", return_value=target),
        patch("integrations.facebook_ads_mutation_transport.create_ad") as graph_post,
        pytest.raises(Exception, match="TARGET_INCOMPLETE"),
    ):
        read_action_precondition(manifest, NOW)
    graph_post.assert_not_called()


def test_legacy_existing_target_rejects_wrong_account() -> None:
    manifest = _legacy_recovery_manifest()
    backend = ProductionRecoveryBackend()
    source = {
        "ad_id": manifest.source_ad_id,
        "name": "CityD | Exact creative",
        "account_id": manifest.account_id,
        "adset_id": manifest.source_adset_id,
        "adset_name": manifest.expected_source_adset_name,
        "status": "ACTIVE",
        "effective_status": "ACTIVE",
        "creative_id": "40001",
    }
    target = _legacy_inventory(
        manifest,
        [{
            "id": "90001",
            "name": manifest.expected_ad_name,
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "creative": {"id": "40001"},
        }],
    )
    target["account_id"] = "99999"
    with (
        patch("integrations.facebook.get_existing_ad_creative_source", return_value=source),
        patch.object(backend, "get_adset_info", return_value=target),
    ):
        assert backend.confirm_existing_target(manifest, "90001") is False


def test_claim_rejects_manifest_digest_drift(recovery_db) -> None:
    manifest = _manifest()
    attempt = _attestation(manifest)
    with patch("services.asset_recovery_repository.verify_attempt_attestation"):
        authorization = reserve_authorization(manifest, attempt, NOW)
    tampered = replace(manifest, capacity_available=3)
    with pytest.raises(AssetRecoveryRepositoryError, match="scope"):
        claim_create(authorization, tampered, NOW)
    assert get_attempt_state(attempt.attempt_id)["phase"] == "RESERVED"


def test_server_owned_hard_reserve_rejects_caller_zero() -> None:
    manifest = replace(_manifest(), hard_reserve_slots=0)
    with (
        patch("services.approval_source_facebook.get_fb_account_id", return_value="10001"),
        patch("services.ad_asset_recovery._asset_recovery_hard_reserve_slots", return_value=1),
        patch("integrations.facebook_ads_mutation_transport.create_ad") as graph_post,
        pytest.raises(Exception, match="HARD_RESERVE_DRIFT"),
    ):
        read_action_precondition(manifest, NOW)
    graph_post.assert_not_called()
