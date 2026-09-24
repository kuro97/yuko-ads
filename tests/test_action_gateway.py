from __future__ import annotations

import inspect
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest

import config
from services import action_adapter_registry, action_gateway
from services.action_manifests import build_action_batch
from services.approval_audit import reserve_operation
from services.approval_checker_models import (
    ActionBatchManifest,
    ActionKind,
    ActionOrigin,
    OperationState,
    UnpauseManifest,
)


NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
SHA = "a" * 64


def _item(index: int, key: str) -> UnpauseManifest:
    return UnpauseManifest(
        kind=ActionKind.UNPAUSE,
        manifest_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"item:{index}:{key}")),
        origin=ActionOrigin.WEB,
        idempotency_key=key,
        prepared_at=NOW,
        ad_id=f"ad-{index}",
        adset_id=f"adset-{index}",
        expected_before_status="PAUSED",
        expected_after_status="ACTIVE",
        pre_inventory_sha256=SHA,
    )


def _batch(count: int = 2) -> ActionBatchManifest:
    key = str(uuid.uuid4())
    return build_action_batch(
        tuple(_item(index, key) for index in range(count)),
        correlation_id=str(uuid.uuid4()),
        idempotency_key=key,
        now=NOW,
    )


@pytest.fixture(autouse=True)
def _audit_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORT_CHECKER_AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(config, "REPORT_CHECKER_ENFORCE_ACTIONS", True)


def test_public_gateway_signature_rejects_caller_dependencies() -> None:
    expected_signatures = {
        action_gateway.execute_action: ("manifest", "now"),
        action_gateway.execute_action_batch: ("manifest", "now"),
        action_gateway.reconcile_operation: ("operation_id", "now"),
        action_gateway.get_operation: ("operation_id",),
    }
    for function, expected in expected_signatures.items():
        assert tuple(inspect.signature(function).parameters) == expected
    with pytest.raises(TypeError):
        action_gateway.execute_action_batch(  # type: ignore[call-arg]
            _batch(1),
            NOW,
            adapter=object(),
        )


@pytest.mark.parametrize("raw_kind", ["DELETE", "ARCHIVE"])
def test_registry_cannot_construct_irreversible_action(raw_kind: str) -> None:
    with pytest.raises(ValueError):
        ActionKind(raw_kind)
    with pytest.raises(TypeError):
        action_adapter_registry._adapter_for_kind(raw_kind)  # type: ignore[arg-type]


@pytest.mark.parametrize("enforce_actions", [True, False])
def test_ownerless_batch_is_denied_before_review_permit_or_adapter(
    monkeypatch,
    enforce_actions: bool,
) -> None:
    calls = {"review": 0, "permit": 0, "adapter": 0, "execute": 0}
    monkeypatch.setattr(config, "REPORT_CHECKER_ENFORCE_ACTIONS", enforce_actions)
    monkeypatch.setattr(
        action_gateway,
        "review_action_item",
        lambda *args: calls.__setitem__("review", calls["review"] + 1),
    )
    monkeypatch.setattr(
        action_gateway,
        "issue_item_permit",
        lambda *args: calls.__setitem__("permit", calls["permit"] + 1),
    )
    monkeypatch.setattr(
        action_adapter_registry,
        "_adapter_for_kind",
        lambda *args: calls.__setitem__("adapter", calls["adapter"] + 1),
    )
    monkeypatch.setattr(
        action_gateway,
        "execute_approved_item",
        lambda *args: calls.__setitem__("execute", calls["execute"] + 1),
    )

    with pytest.raises(action_gateway.OwnerConsentRequired):
        action_gateway.execute_action_batch(_batch(3), NOW)

    assert calls == {"review": 0, "permit": 0, "adapter": 0, "execute": 0}


def test_ownerless_single_action_is_denied_before_adapter(monkeypatch) -> None:
    monkeypatch.setattr(
        action_adapter_registry,
        "_adapter_for_kind",
        lambda *args: pytest.fail("ownerless action не получает adapter"),
    )

    with pytest.raises(action_gateway.OwnerConsentRequired):
        action_gateway.execute_action(_batch(1).actions[0], NOW)


@pytest.mark.parametrize(
    "changed",
    [
        lambda batch: replace(batch, manifest_sha256="f" * 64),
        lambda batch: replace(
            replace(
                batch,
                actions=(
                    replace(
                        batch.actions[0],
                        idempotency_key=str(uuid.uuid4()),
                    ),
                ),
                manifest_sha256="0" * 64,
            ),
            manifest_sha256=action_gateway.manifest_sha256(
                replace(
                    batch,
                    actions=(
                        replace(
                            batch.actions[0],
                            idempotency_key=str(uuid.uuid4()),
                        ),
                    ),
                    manifest_sha256="0" * 64,
                )
            ),
        ),
        lambda batch: replace(
            replace(
                batch,
                subject_ids=("ad:another-ad",),
                manifest_sha256="0" * 64,
            ),
            manifest_sha256=action_gateway.manifest_sha256(
                replace(
                    batch,
                    subject_ids=("ad:another-ad",),
                    manifest_sha256="0" * 64,
                )
            ),
        ),
    ],
)
def test_even_malformed_ownerless_batch_cannot_reach_reservation(
    monkeypatch,
    changed,
) -> None:
    monkeypatch.setattr(
        action_gateway,
        "reserve_operation",
        lambda *args: pytest.fail("ownerless batch не резервирует legacy WAL"),
    )
    with pytest.raises(action_gateway.OwnerConsentRequired):
        action_gateway.execute_action_batch(changed(_batch(1)), NOW)


def test_get_and_reconcile_reserved_operation_are_read_only(monkeypatch) -> None:
    batch = _batch(1)
    record = reserve_operation(batch, NOW)
    monkeypatch.setattr(
        action_gateway,
        "recover_incomplete_operation",
        lambda *args: pytest.fail("RESERVED не требует provider reconciliation"),
    )

    loaded = action_gateway.get_operation(record.operation_id)
    reconciled = action_gateway.reconcile_operation(record.operation_id, NOW)

    assert loaded is not None
    assert loaded.operation_id == record.operation_id
    assert loaded.state is OperationState.RESERVED
    assert reconciled.operation_id == record.operation_id
    assert reconciled.state is OperationState.RESERVED
