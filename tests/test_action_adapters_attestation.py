"""Adapters обязаны передавать owner attestation в typed transport."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (_PROJECT_ROOT / relative).read_text(encoding="utf-8")


def test_pause_and_scale_adapters_use_typed_transport() -> None:
    pause = _source("services/action_adapter_pause.py")
    scale = _source("services/action_adapter_scale.py")
    assert "set_ad_status(" in pause
    assert "set_adset_budget(" in scale
    assert "_pause_ad_unchecked" not in pause
    assert "_set_adset_budget_unchecked" not in scale
    assert "from services.owner_action_models import ActionAttemptAttestation" in pause
    assert "from services.owner_action_models import ActionAttemptAttestation" in scale


def test_launch_and_recovery_forward_attestation_to_create_boundary() -> None:
    launch_adapter = _source("services/action_adapter_launch.py")
    recovery_adapter = _source("services/action_adapter_asset_recovery.py")
    facebook = _source("integrations/facebook.py")
    assert "attempt=attempt" in launch_adapter
    assert "_execute_asset_recovery_manifest_unchecked(manifest, attempt)" in recovery_adapter
    assert facebook.count("create_attested_ad(") == 2


def test_adapter_mutators_do_not_discard_attempt() -> None:
    paths = (
        "services/action_adapter_pause.py",
        "services/action_adapter_scale.py",
        "services/action_adapter_launch.py",
        "services/action_adapter_asset_recovery.py",
    )
    violations: list[str] = []
    for relative in paths:
        tree = ast.parse(_source(relative))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "mutate":
                continue
            if any(
                isinstance(child, ast.Delete)
                and any(
                    isinstance(target, ast.Name) and target.id == "attempt"
                    for target in child.targets
                )
                for child in ast.walk(node)
            ):
                violations.append(relative)
    assert violations == []


def test_owner_and_operation_attestations_never_share_a_name() -> None:
    """Мина bug9: одноимённые классы в двух контурах.

    Owner-аттестация несёт account_id и принимается provider-границей;
    батч-аттестация consume_permit его не несёт — под общим именем подмена
    одной на другую давала бы мгновенный PROVIDER_SCOPE_DRIFT.
    """

    from services import approval_checker_models, owner_action_models

    owner_fields = {
        field.name
        for field in dataclasses.fields(owner_action_models.ActionAttemptAttestation)
    }
    assert "account_id" in owner_fields

    # Контур батч-проверок не должен снова завести owner-имя — ни классом,
    # ни алиасом на любой из двух типов.
    assert not hasattr(approval_checker_models, "ActionAttemptAttestation")

    operation_fields = {
        field.name
        for field in dataclasses.fields(
            approval_checker_models.OperationAttemptAttestation
        )
    }
    assert "operation_id" in operation_fields
    assert (
        approval_checker_models.OperationAttemptAttestation
        is not owner_action_models.ActionAttemptAttestation
    )


def test_consume_permit_returns_operation_attestation() -> None:
    """Core-путь отдаёт в adapter.mutate именно батч-аттестацию."""

    import typing

    from services import approval_audit, approval_checker_models

    hints = typing.get_type_hints(approval_audit.consume_permit)
    assert hints["return"] is approval_checker_models.OperationAttemptAttestation


def test_registry_has_no_delete_archive_or_rename_adapter() -> None:
    registry = _source("services/action_adapter_registry.py")
    assert "ActionKind.DELETE" not in registry
    assert "ActionKind.ARCHIVE" not in registry
    assert "ActionKind.RENAME" not in registry
