"""Security tests единственной attested Facebook Ads write boundary."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from integrations import facebook_ads_mutation_transport as transport
from services.owner_action_models import ActionAttemptAttestation

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_HASH = "a" * 64
_NOW = datetime.now(timezone.utc) - timedelta(seconds=1)


def _attestation(
    *,
    operation_kind: str = "PAUSE_AD",
    resource_id: str = "123",
) -> ActionAttemptAttestation:
    return ActionAttemptAttestation(
        attempt_id="attempt-1",
        permit_id="permit-1",
        proposal_id="proposal-1",
        decision_id="decision-1",
        claim_id="claim-1",
        operation_kind=operation_kind,
        account_id="act_456",
        resource_id=resource_id,
        payload_sha256=_HASH,
        consumed_at=_NOW,
    )


def _persisted(attestation: ActionAttemptAttestation) -> dict[str, object]:
    return {
        "attempt_id": attestation.attempt_id,
        "permit_id": attestation.permit_id,
        "proposal_id": attestation.proposal_id,
        "decision_id": attestation.decision_id,
        "claim_id": attestation.claim_id,
        "operation_kind": attestation.operation_kind,
        "account_id": attestation.account_id,
        "resource_id": attestation.resource_id,
        "exact_payload_sha256": attestation.payload_sha256,
        "intended_payload_sha256": attestation.payload_sha256,
        "state": "ATTEMPT_STARTED",
        "permit_phase": "CONSUMED",
        "started_at": attestation.consumed_at.isoformat(),
        "consumed_at": attestation.consumed_at.isoformat(),
    }


def test_status_write_requires_exact_persisted_attestation(monkeypatch) -> None:
    attestation = _attestation()
    response = MagicMock(status_code=200)
    monkeypatch.setattr(
        transport,
        "_read_persisted_attempt",
        lambda value: _persisted(value),
    )
    request = MagicMock(return_value=response)
    monkeypatch.setattr(transport, "_do_throttled_request", request)
    monkeypatch.setattr(transport, "get_fb_token", lambda: "test-token")

    assert transport.set_ad_status(
        attestation,
        account_id="act_456",
        ad_id="123",
        status="PAUSED",
        payload_sha256=_HASH,
    )
    request.assert_called_once()


def test_forged_attestation_fails_before_network(monkeypatch) -> None:
    attestation = _attestation()
    request = MagicMock()
    monkeypatch.setattr(
        transport,
        "_read_persisted_attempt",
        lambda value: {**_persisted(value), "state": "CONFIRMED"},
    )
    monkeypatch.setattr(transport, "_do_throttled_request", request)

    with pytest.raises(transport.AttestationRejected):
        transport.set_ad_status(
            attestation,
            account_id="act_456",
            ad_id="123",
            status="PAUSED",
            payload_sha256=_HASH,
        )
    request.assert_not_called()


def test_scope_drift_fails_before_durable_lookup_or_network(monkeypatch) -> None:
    lookup = MagicMock()
    request = MagicMock()
    monkeypatch.setattr(transport, "_read_persisted_attempt", lookup)
    monkeypatch.setattr(transport, "_do_throttled_request", request)

    with pytest.raises(transport.AttestationRejected, match="SCOPE_MISMATCH"):
        transport.set_ad_status(
            _attestation(),
            account_id="act_456",
            ad_id="999",
            status="PAUSED",
            payload_sha256=_HASH,
        )
    lookup.assert_not_called()
    request.assert_not_called()


def test_budget_and_create_use_only_typed_payloads(monkeypatch) -> None:
    request = MagicMock()
    budget_response = MagicMock(status_code=200)
    create_response = MagicMock(status_code=200)
    create_response.json.return_value = {"id": "ad-created"}
    request.side_effect = [budget_response, create_response]
    monkeypatch.setattr(transport, "_do_throttled_request", request)
    monkeypatch.setattr(transport, "get_fb_token", lambda: "test-token")
    monkeypatch.setattr(
        transport,
        "_read_persisted_attempt",
        lambda value: _persisted(value),
    )
    budget_attempt = _attestation(
        operation_kind="SET_ADSET_BUDGET",
        resource_id="adset-1",
    )
    create_attempt = _attestation(
        operation_kind="CREATE_AD",
        resource_id="adset-1",
    )

    assert transport.set_adset_budget(
        budget_attempt,
        account_id="act_456",
        adset_id="adset-1",
        daily_budget_minor_units=2500,
        payload_sha256=_HASH,
    )
    assert (
        transport.create_ad(
            create_attempt,
            account_id="act_456",
            adset_id="adset-1",
            name="Approved ad",
            creative={"creative_id": "creative-1"},
            payload_sha256=_HASH,
        )
        == "ad-created"
    )
    assert request.call_count == 2


def test_active_graph_writes_exist_only_in_transport() -> None:
    paths = (
        _PROJECT_ROOT / "agent/fb_common.py",
        _PROJECT_ROOT / "agent/analyzer.py",
        _PROJECT_ROOT / "integrations/facebook.py",
        *(
            _PROJECT_ROOT / relative
            for relative in (
                "services/adset_pause_guard.py",
                "services/ad_renamer.py",
                "services/action_adapter_pause.py",
                "services/action_adapter_scale.py",
                "services/action_adapter_launch.py",
                "services/action_adapter_asset_recovery.py",
                "services/action_remote_scale.py",
            )
        ),
    )
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"post", "delete"}
            ):
                violations.append(str(path.relative_to(_PROJECT_ROOT)))
    assert violations == []


def test_transport_has_no_delete_archive_or_rename_api() -> None:
    public_names = set(transport.__all__)
    assert not any(
        fragment in name.lower()
        for name in public_names
        for fragment in ("delete", "archive", "rename")
    )


def test_producers_cannot_import_mutation_transport() -> None:
    allowed = {
        Path("integrations/facebook.py"),
        Path("services/action_adapter_pause.py"),
        Path("services/action_adapter_scale.py"),
        Path("services/ad_renamer.py"),
    }
    violations: list[str] = []
    for path in _PROJECT_ROOT.rglob("*.py"):
        relative = path.relative_to(_PROJECT_ROOT)
        if (
            relative.parts[0] in {"tests", "venv", ".venv"}
            # Скрытые каталоги — служебные чекауты, а не код этого дерева
            # (например .claude/worktrees/* с полными копиями других веток):
            # их проверяет их собственный прогон, здесь они дают ложные срабатывания.
            or relative.parts[0].startswith(".")
            or relative in allowed
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module == "integrations.facebook_ads_mutation_transport"
            for node in ast.walk(tree)
        ):
            violations.append(str(relative))
    assert violations == []


def test_legacy_pause_guard_is_read_only(monkeypatch) -> None:
    from services import adset_pause_guard

    inventory = {
        "adset-1": {
            "complete": True,
            "active_ids": {"ad-1", "ad-2"},
            "candidate_context": {
                "ad-1": {
                    "adset_id": "adset-1",
                    "effective_status": "ACTIVE",
                    "configured_status": "ACTIVE",
                }
            },
        }
    }
    monkeypatch.setattr(
        adset_pause_guard,
        "fetch_pause_inventory",
        lambda _ids: inventory,
    )
    network = MagicMock()
    monkeypatch.setattr(transport, "session", network)

    outcome = adset_pause_guard.safe_pause_ad("ad-1", source="legacy")

    assert outcome.ok is False
    assert outcome.reason == "owner_approval_required"
    network.post.assert_not_called()


def test_launch_claim_id_with_colons_passes_validation(monkeypatch) -> None:
    """Регрессия: LAUNCH-клейм «launch:<uuid>:<idx>» отбивался
    общим идентификаторным регэкспом (ATTESTATION_CLAIM_INVALID), и создание
    объявлений падало до первого сетевого вызова."""
    attestation = ActionAttemptAttestation(
        attempt_id="attempt-1",
        permit_id="permit-1",
        proposal_id="proposal-1",
        decision_id="decision-1",
        claim_id="launch:787798b3-3eff-43c8-90e5-1767ece98219:0",
        operation_kind="PAUSE_AD",
        account_id="act_456",
        resource_id="123",
        payload_sha256=_HASH,
        consumed_at=_NOW,
    )
    response = MagicMock(status_code=200)
    monkeypatch.setattr(
        transport,
        "_read_persisted_attempt",
        lambda value: _persisted(value),
    )
    monkeypatch.setattr(transport, "_do_throttled_request", MagicMock(return_value=response))
    monkeypatch.setattr(transport, "get_fb_token", lambda: "test-token")

    assert transport.set_ad_status(
        attestation,
        account_id="act_456",
        ad_id="123",
        status="PAUSED",
        payload_sha256=_HASH,
    )


@pytest.mark.parametrize(
    "claim_id",
    [
        "",
        "launch:не-uuid:0",
        "launch:787798b3-3eff-43c8-90e5-1767ece98219:",
        "pause:787798b3-3eff-43c8-90e5-1767ece98219:0",
        "claim/../1",
    ],
)
def test_malformed_claim_id_rejected(claim_id: str) -> None:
    attestation = ActionAttemptAttestation(
        attempt_id="attempt-1",
        permit_id="permit-1",
        proposal_id="proposal-1",
        decision_id="decision-1",
        claim_id=claim_id,
        operation_kind="PAUSE_AD",
        account_id="act_456",
        resource_id="123",
        payload_sha256=_HASH,
        consumed_at=_NOW,
    )
    with pytest.raises(transport.AttestationRejected, match="CLAIM_INVALID"):
        transport._validate_attestation(
            attestation,
            operation_kind="PAUSE_AD",
            account_id="act_456",
            resource_id="123",
            payload_sha256=_HASH,
        )
