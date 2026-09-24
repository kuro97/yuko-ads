"""T22: scaler использует gateway и одну durable cap reservation."""

from __future__ import annotations

import ast
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from services.action_adapter_scale import ScaleActionAdapter
from services.action_gateway_core import AdapterPostconditionResult
from services.action_manifests import build_scale_manifest
from services.approval_checker_models import (
    ActionOrigin,
    ActionResult,
    FactCategory,
    FactClaim,
    Metric,
    ScaleCandidate,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
)
from services.owner_action_models import ActionAttemptAttestation


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 22, 8, tzinfo=timezone.utc)
PAYLOAD_SHA256 = "a" * 64


def _manifest():
    window = TimeWindow(
        NOW - timedelta(days=30), NOW, "Etc/GMT-5", "TRAILING_30D"
    )
    subject = SubjectRef(SubjectKind.AD, "ad-1", "adset-1")
    facts = (
        FactClaim("fb", None, FactCategory.BUSINESS_METRIC, subject, Metric.SPEND, Decimal("10"), SourceSystem.FACEBOOK, window, "USD"),
        FactClaim("amo", None, FactCategory.BUSINESS_METRIC, subject, Metric.QUALS, 2, SourceSystem.AMO, window),
        FactClaim("cdp", None, FactCategory.BUSINESS_METRIC, subject, Metric.REVENUE, Decimal("100000"), SourceSystem.CDP_ERP, window, "LCY"),
    )
    candidate = ScaleCandidate(
        adset_id="adset-1",
        expected_status="ACTIVE",
        current_budget=Decimal("100"),
        target_budget=Decimal("115"),
        currency="USD",
        facebook_window=window,
        outcome_window=window,
        candidate_ad_ids=("ad-1",),
        facts=facts,
    )
    return build_scale_manifest(
        candidate,
        origin=ActionOrigin.BUDGET_SCALER,
        idempotency_key=str(uuid.uuid4()),
        now=NOW,
    )


def _attestation() -> ActionAttemptAttestation:
    return ActionAttemptAttestation(
        attempt_id=str(uuid.uuid4()),
        permit_id=str(uuid.uuid4()),
        proposal_id=str(uuid.uuid4()),
        decision_id=str(uuid.uuid4()),
        claim_id=str(uuid.uuid4()),
        operation_kind="SET_ADSET_BUDGET",
        account_id="act_1",
        resource_id="adset-1",
        payload_sha256=PAYLOAD_SHA256,
        consumed_at=NOW,
    )


def test_scaler_producer_never_calls_raw_budget_mutator() -> None:
    """Scaler — producer: он только ПРЕДЛАГАЕТ подъём владельцу.

    Проверяем имя в импорте (`propose_scale`), а не legacy-алиас `execute_scale`,
    потому что боевой код импортирует producer-функцию локально по имени: тест,
    привязанный к алиасу, «зеленел» бы даже если прод дёргает совсем другое.
    Плюс прямой FB-транспорт мутаций не должен импортироваться в producer вовсе —
    единственная точка реальной мутации бюджета живёт в execution boundary,
    который срабатывает только после одобрения в Telegram.
    """
    tree = ast.parse((ROOT / "services/budget_scaler.py").read_text(encoding="utf-8"))
    forbidden_calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "set_adset_budget"
    }
    imports = {
        (node.module or "", alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    imported = {name for _, name in imports}
    mutation_transport_imports = {
        module for module, _ in imports
        if module.startswith("integrations.facebook_ads_mutation_transport")
    }

    assert not forbidden_calls
    assert "_set_adset_budget_unchecked" not in imported
    assert ("services.action_producer_gateway", "propose_scale") in imports, (
        "budget_scaler обязан импортировать propose_scale из "
        f"services.action_producer_gateway, найдено: {sorted(imports)}"
    )
    assert not mutation_transport_imports, (
        f"producer тянет прямой FB-транспорт мутаций: {mutation_transport_imports}"
    )


def test_scale_confirmed_uses_one_reservation_and_one_commit(monkeypatch) -> None:
    manifest = _manifest()
    reserve = Mock(return_value="reservation-1")
    commit = Mock()
    release = Mock()

    @contextmanager
    def unlocked(_ids):
        yield

    monkeypatch.setattr("services.action_adapter_scale.budget_daily_cap.reserve_raise", reserve)
    monkeypatch.setattr("services.action_adapter_scale.budget_daily_cap.commit_reservation", commit)
    monkeypatch.setattr("services.action_adapter_scale.budget_daily_cap.release_reservation", release)
    monkeypatch.setattr("services.action_adapter_scale.adset_locks", unlocked)
    typed_budget = Mock(return_value=True)
    monkeypatch.setattr(
        "services.action_adapter_scale.set_adset_budget",
        typed_budget,
    )

    adapter = ScaleActionAdapter()
    attempt = _attestation()
    with adapter.execution_scope(manifest, NOW):
        mutation = adapter.mutate(manifest, NOW, attempt=attempt)
    post = AdapterPostconditionResult(
        result=ActionResult.CONFIRMED,
        observation=None,
        reason_code="TARGET_BUDGET_CONFIRMED",
    )
    adapter.finalize_after_scope(manifest, mutation, post, NOW)

    reserve.assert_called_once_with("adset-1", 100.0, 115.0, 15.0, now=NOW)
    typed_budget.assert_called_once_with(
        attempt,
        account_id=attempt.account_id,
        adset_id="adset-1",
        daily_budget_minor_units=11500,
        payload_sha256=PAYLOAD_SHA256,
    )
    commit.assert_called_once_with("adset-1", "reservation-1", now=NOW)
    release.assert_not_called()


def _install_cap_mocks(monkeypatch) -> dict[str, Mock]:
    """Мокает cap-резервацию и adset lock; возвращает именованные Mock'и."""
    reserve = Mock(return_value="reservation-1")
    commit = Mock()
    release = Mock()

    @contextmanager
    def unlocked(_ids):
        yield

    monkeypatch.setattr("services.action_adapter_scale.budget_daily_cap.reserve_raise", reserve)
    monkeypatch.setattr("services.action_adapter_scale.budget_daily_cap.commit_reservation", commit)
    monkeypatch.setattr("services.action_adapter_scale.budget_daily_cap.release_reservation", release)
    monkeypatch.setattr("services.action_adapter_scale.adset_locks", unlocked)
    monkeypatch.setattr(
        "services.action_adapter_scale.set_adset_budget", Mock(return_value=True)
    )
    return {"reserve": reserve, "commit": commit, "release": release}


def _finalize(adapter: ScaleActionAdapter, manifest, result: ActionResult) -> None:
    """Прогоняет scope→mutate→finalize с заданным итогом postcondition."""
    attempt = _attestation()
    with adapter.execution_scope(manifest, NOW):
        mutation = adapter.mutate(manifest, NOW, attempt=attempt)
    adapter.finalize_after_scope(
        manifest,
        mutation,
        AdapterPostconditionResult(
            result=result,
            observation=None,
            reason_code="TEST",
        ),
        NOW,
    )


def test_executed_scale_writes_budget_raised_decision(monkeypatch) -> None:
    """Подтверждённый подъём пишет BUDGET_RAISED — иначе отчёты врут «0 подъёмов».

    Читатели журнала (services/evening_report.py «⬆️ Подняли бюджет»,
    services/morning_digest.py) считают подъёмы ровно по этому решению. Producer
    его писать не имеет права (предложение — ещё не подъём), поэтому запись
    обязана появляться здесь, в точке подтверждённой мутации.
    """
    _install_cap_mocks(monkeypatch)
    save = Mock(return_value=True)
    monkeypatch.setattr("agent.repositories.decisions_repo.save_decision", save)

    manifest = _manifest()
    _finalize(ScaleActionAdapter(), manifest, ActionResult.CONFIRMED)

    save.assert_called_once()
    assert save.call_args.args[1:4] == ("ad-1", "ad-1", "BUDGET_RAISED")
    assert save.call_args.kwargs["confirmed_by"] == "budget_pilot"
    assert (
        save.call_args.kwargs["effect_id"]
        == f"{manifest.manifest_id}:decision:BUDGET_RAISED"
    )
    reason = save.call_args.kwargs["reason"]
    assert "adset-1" in reason and "100" in reason and "115" in reason


def test_unconfirmed_scale_never_writes_budget_raised(monkeypatch) -> None:
    """PARTIAL/UNKNOWN подъём в журнал не попадает: факта подъёма нет."""
    _install_cap_mocks(monkeypatch)
    save = Mock(return_value=True)
    monkeypatch.setattr("agent.repositories.decisions_repo.save_decision", save)

    for result in (ActionResult.PARTIAL, ActionResult.UNKNOWN):
        _finalize(ScaleActionAdapter(), _manifest(), result)

    save.assert_not_called()


def test_budget_raised_write_failure_does_not_break_execution(monkeypatch) -> None:
    """Сбой журнала не отменяет исполненный подъём и не роняет finalize."""
    caps = _install_cap_mocks(monkeypatch)
    monkeypatch.setattr(
        "agent.repositories.decisions_repo.save_decision",
        Mock(side_effect=RuntimeError("БД недоступна")),
    )

    manifest = _manifest()
    _finalize(ScaleActionAdapter(), manifest, ActionResult.CONFIRMED)

    caps["commit"].assert_called_once_with("adset-1", "reservation-1", now=NOW)


def test_scale_failed_before_provider_releases_single_reservation(monkeypatch) -> None:
    manifest = _manifest()
    reserve = Mock(return_value="reservation-1")
    release = Mock()

    @contextmanager
    def unlocked(_ids):
        yield

    monkeypatch.setattr("services.action_adapter_scale.budget_daily_cap.reserve_raise", reserve)
    monkeypatch.setattr("services.action_adapter_scale.budget_daily_cap.release_reservation", release)
    monkeypatch.setattr("services.action_adapter_scale.adset_locks", unlocked)

    with ScaleActionAdapter().execution_scope(manifest, NOW):
        pass

    reserve.assert_called_once()
    release.assert_called_once_with("adset-1", "reservation-1", now=NOW)
