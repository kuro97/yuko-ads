"""«Запущено» только при реальном ACTIVE: пост-исполнение, отчёт, проводка.

Требование владельца: запуск не считается успешным и не рапортуется как
«запущено», пока живой effective_status созданного объявления не ACTIVE в
ЦЕЛЕВОМ адсете. При неудаче ошибка видимая, проверка продолжается.

Проверяются три звена:
  * finalize_executed_launch — ставит durable ACTIVE-гейт после исполнения и НЕ
    объявляет успех;
  * verify_and_report_launch_watchdogs — «✅ Запущено» только на VERIFIED,
    провал/сверка → критический алерт, VERIFYING → тишина;
  * execute_owner_approved — вызывает пост-исполнение для EXECUTED-запуска.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services import launch_verify
from services.launch_verify import (
    VerificationRun,
    finalize_executed_launch,
    verify_and_report_launch_watchdogs,
    verify_launch_watchdogs,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)


def _launch_run(state: str = "EXECUTED", *, reconcile: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        proposal_id="proposal-1",
        decision_id="decision-1",
        job_id="job-1",
        state=state,
        reconciliation_required=reconcile,
    )


def _launch_proposal() -> SimpleNamespace:
    return SimpleNamespace(proposal_kind=SimpleNamespace(value="LAUNCH"))


def _transition(
    state: str,
    *,
    verified: int = 1,
    expected: int = 1,
    reason_code: str = "LAUNCH_OK",
) -> SimpleNamespace:
    return SimpleNamespace(
        watchdog_id="watchdog-1",
        proposal_id="proposal-1",
        state=state,
        outcome=state,
        verified_count=verified,
        expected_count=expected,
        next_verify_at=None,
        reason_code=reason_code,
    )


# ---------------------------------------------------------------------------
# Пост-исполнение: гейт ставится, успех не объявляется
# ---------------------------------------------------------------------------


def test_finalize_registers_watchdog_and_does_not_claim_launched() -> None:
    sent: list[str] = []

    with (
        patch(
            "services.owner_action_repository.get_proposal",
            return_value=_launch_proposal(),
        ),
        patch.object(
            launch_verify,
            "register_executed_launch",
            return_value="watchdog-1",
        ) as register,
    ):
        result = finalize_executed_launch(
            _launch_run(),
            now=NOW,
            send_telegram_fn=sent.append,
        )

    assert result == {"registered": True, "watchdog_id": "watchdog-1", "reason": None}
    register.assert_called_once()
    assert len(sent) == 1
    message = sent[0]
    # Ключевое: на этом шаге успех не утверждается — только обещание проверки.
    assert "✅" not in message
    assert "ждём ACTIVE" in message
    assert "«Запущено» будет объявлено только после" in message
    assert "effective_status=ACTIVE" in message


@pytest.mark.parametrize(
    ("state", "reconcile"),
    [
        ("FAILED_NO_EFFECT", False),
        ("RECONCILE_REQUIRED", False),
        ("EXECUTED", True),
    ],
)
def test_finalize_skips_watchdog_for_non_executed_run(
    state: str,
    reconcile: bool,
) -> None:
    sent: list[str] = []

    with patch.object(launch_verify, "register_executed_launch") as register:
        result = finalize_executed_launch(
            _launch_run(state, reconcile=reconcile),
            now=NOW,
            send_telegram_fn=sent.append,
        )

    assert result["registered"] is False
    register.assert_not_called()
    assert sent == []


def test_finalize_skips_non_launch_proposal() -> None:
    pause_proposal = SimpleNamespace(proposal_kind=SimpleNamespace(value="PAUSE"))

    with (
        patch(
            "services.owner_action_repository.get_proposal",
            return_value=pause_proposal,
        ),
        patch.object(launch_verify, "register_executed_launch") as register,
    ):
        result = finalize_executed_launch(_launch_run(), now=NOW, send_telegram_fn=lambda _t: None)

    assert result == {"registered": False, "watchdog_id": None, "reason": "NOT_LAUNCH"}
    register.assert_not_called()


def test_finalize_makes_failed_gate_visible_without_claiming_success() -> None:
    critical = MagicMock()
    sent: list[str] = []

    with (
        patch(
            "services.owner_action_repository.get_proposal",
            return_value=_launch_proposal(),
        ),
        patch.object(
            launch_verify,
            "register_executed_launch",
            side_effect=ValueError("Exact provider semantic binding не найден"),
        ),
        patch("services.notifications.send_critical_alert", critical),
    ):
        result = finalize_executed_launch(
            _launch_run(),
            now=NOW,
            send_telegram_fn=sent.append,
        )

    assert result["registered"] is False
    assert result["reason"] == "WATCHDOG_REGISTER_FAILED"
    critical.assert_called_once()
    assert "ручная проверка" in critical.call_args.args[1]
    # Без гейта «запущено» не рапортуется вообще.
    assert sent == []


# ---------------------------------------------------------------------------
# Отчёт: «запущено» только на VERIFIED
# ---------------------------------------------------------------------------


def _run_report(transitions: list[SimpleNamespace]):
    """Гоняет отчёт на подставных переходах watchdog."""

    def _fake_verify(*, worker_id, now=None, limit=50, on_transition=None):
        del worker_id, now, limit
        for transition in transitions:
            on_transition(transition)
        states = [item.state for item in transitions]
        return VerificationRun(
            checked=len(transitions),
            verified=states.count("VERIFIED"),
            pending=states.count("VERIFYING"),
            failed=states.count("FAILED_VERIFICATION"),
            reconcile_required=states.count("RECONCILE_REQUIRED"),
            errors=(),
        )

    telegram = MagicMock()
    critical = MagicMock()
    with (
        patch.object(launch_verify, "verify_launch_watchdogs", _fake_verify),
        patch("services.notifications.send_telegram", telegram),
        patch("services.notifications.send_critical_alert", critical),
    ):
        result = verify_and_report_launch_watchdogs(worker_id="test", now=NOW)
    return result, telegram, critical


def test_verified_watchdog_is_the_only_source_of_launched_claim() -> None:
    result, telegram, critical = _run_report([_transition("VERIFIED")])

    assert result["verified"] == 1
    telegram.assert_called_once()
    critical.assert_not_called()
    message = telegram.call_args.args[0]
    assert message.startswith("✅ Запущено")
    assert "effective_status=ACTIVE" in message


def test_failed_verification_is_visible_and_never_claims_launched() -> None:
    result, telegram, critical = _run_report(
        [_transition("FAILED_VERIFICATION", verified=0, reason_code="LAUNCH_VERIFY_DEADLINE")]
    )

    assert result["failed"] == 1
    telegram.assert_not_called()
    critical.assert_called_once()
    title, detail = critical.call_args.args[0], critical.call_args.args[1]
    assert "не подтверждён" in title
    assert "LAUNCH_VERIFY_DEADLINE" in detail
    assert "«Запущено» не рапортуем" in detail


def test_reconcile_required_raises_critical_alert() -> None:
    result, telegram, critical = _run_report([_transition("RECONCILE_REQUIRED")])

    assert result["reconcile_required"] == 1
    telegram.assert_not_called()
    critical.assert_called_once()
    assert "сверки" in critical.call_args.args[0]


def test_pending_verification_stays_silent_and_keeps_checking() -> None:
    result, telegram, critical = _run_report([_transition("VERIFYING", verified=0)])

    assert result["pending"] == 1
    assert result["reported"] == []
    # Ни успеха, ни провала: проверка продолжается до дедлайна watchdog.
    telegram.assert_not_called()
    critical.assert_not_called()


# ---------------------------------------------------------------------------
# Проводка колбэка и вызова из исполнения
# ---------------------------------------------------------------------------


class _FakeVerificationRepository:
    def __init__(self, state: str) -> None:
        self.state = state
        self.lease = SimpleNamespace(watchdog_id="watchdog-1", targets=())

    def claim_due(self, **_kwargs):
        return (self.lease,)

    def record_observation(self, _lease, *, observations, fetch_complete, now):
        del observations, fetch_complete, now
        return _transition(self.state)


def test_on_transition_callback_receives_durable_transition() -> None:
    repository = _FakeVerificationRepository("VERIFIED")
    seen: list[SimpleNamespace] = []

    with (
        patch.object(
            launch_verify,
            "_owner_launch_repository",
            return_value=repository,
        ),
        patch.object(launch_verify, "_fetch_watchdog_live_ads", return_value={}),
    ):
        run = verify_launch_watchdogs(
            worker_id="verify-a",
            now=NOW,
            on_transition=seen.append,
        )

    assert run.verified == 1
    assert [item.state for item in seen] == ["VERIFIED"]


def test_broken_report_callback_does_not_hide_durable_result() -> None:
    repository = _FakeVerificationRepository("VERIFIED")

    def _boom(_transition):
        raise RuntimeError("telegram down")

    with (
        patch.object(
            launch_verify,
            "_owner_launch_repository",
            return_value=repository,
        ),
        patch.object(launch_verify, "_fetch_watchdog_live_ads", return_value={}),
    ):
        run = verify_launch_watchdogs(
            worker_id="verify-a",
            now=NOW,
            on_transition=_boom,
        )

    assert run.verified == 1
    assert run.errors == ("watchdog-1:REPORT_RuntimeError",)


def test_executor_registers_active_gate_after_executed_launch() -> None:
    from services import owner_action_executor

    run = _launch_run()
    finalize = MagicMock(return_value={"registered": True})

    with (
        patch.object(
            owner_action_executor,
            "_execute_owner_approved_claims",
            return_value=run,
        ),
        patch.object(launch_verify, "finalize_executed_launch", finalize),
    ):
        result = owner_action_executor.execute_owner_approved(
            "proposal-1",
            worker_id="worker-1",
            now=NOW,
        )

    assert result is run
    finalize.assert_called_once_with(run, now=NOW)


def test_executor_skips_active_gate_when_reconciliation_required() -> None:
    from services import owner_action_executor

    run = _launch_run(reconcile=True)
    finalize = MagicMock()

    with (
        patch.object(
            owner_action_executor,
            "_execute_owner_approved_claims",
            return_value=run,
        ),
        patch.object(launch_verify, "finalize_executed_launch", finalize),
    ):
        owner_action_executor.execute_owner_approved(
            "proposal-1",
            worker_id="worker-1",
            now=NOW,
        )

    finalize.assert_not_called()


# ---------------------------------------------------------------------------
# Watchdog по-claim'но: run после ретраев несёт исполнения только своего тика
# ---------------------------------------------------------------------------


def _register_proposal_two_targets() -> SimpleNamespace:
    def _target(ordinal: int) -> SimpleNamespace:
        return SimpleNamespace(
            claim_id=f"launch:m:{ordinal}",
            ordinal=ordinal,
            account_id="111",
            adset_id=f"adset-{ordinal}",
            intended_payload={
                "manifest_id": f"m:{ordinal}",
                "destinations": (
                    {
                        "adset_id": f"adset-{ordinal}",
                        "creatives": ({"ad_name": f"Город {ordinal} | Тест"},),
                    },
                ),
            },
        )

    return SimpleNamespace(
        proposal_kind=SimpleNamespace(value="LAUNCH"),
        targets=(_target(0), _target(1)),
    )


def _execution(ordinal: int, ad_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        action_manifest_id=f"m:{ordinal}",
        created_ids=(ad_id,),
    )


def test_register_creates_watchdog_for_partial_tick() -> None:
    captured: dict[str, object] = {}

    class _Repo:
        def create_watchdog(self, **kwargs):
            captured.update(kwargs)
            return "watchdog-partial"

    run = SimpleNamespace(
        proposal_id="proposal-1",
        decision_id="decision-1",
        job_id="job-1",
        state="EXECUTED",
        reconciliation_required=False,
        executions=(_execution(1, "ad-2"),),
    )
    with (
        patch(
            "services.owner_action_repository.get_proposal",
            return_value=_register_proposal_two_targets(),
        ),
        patch(
            "services.launch_repository.get_provider_ad_bindings",
            return_value=[
                {
                    "ad_id": "ad-2",
                    "phase": "VERIFIED",
                    "expected_fingerprint": "f" * 64,
                }
            ],
        ),
        patch.object(launch_verify, "_owner_launch_repository", return_value=_Repo()),
    ):
        watchdog_id = launch_verify.register_executed_launch(run, now=NOW)

    assert watchdog_id == "watchdog-partial"
    targets = captured["targets"]
    assert len(targets) == 1
    assert targets[0].claim_id == "launch:m:1"
    assert targets[0].created_ad_id == "ad-2"


def test_register_requires_at_least_one_execution() -> None:
    run = SimpleNamespace(
        proposal_id="proposal-1",
        decision_id="decision-1",
        job_id="job-1",
        state="EXECUTED",
        reconciliation_required=False,
        executions=(),
    )
    with patch(
        "services.owner_action_repository.get_proposal",
        return_value=_register_proposal_two_targets(),
    ):
        with pytest.raises(ValueError):
            launch_verify.register_executed_launch(run, now=NOW)


def test_register_skips_executions_without_created_ads() -> None:
    captured: dict[str, object] = {}

    class _Repo:
        def create_watchdog(self, **kwargs):
            captured.update(kwargs)
            return "watchdog-skip"

    run = SimpleNamespace(
        proposal_id="proposal-1",
        decision_id="decision-1",
        job_id="job-1",
        state="EXECUTED",
        reconciliation_required=False,
        executions=(
            SimpleNamespace(action_manifest_id="m:0", created_ids=()),
            _execution(1, "ad-2"),
        ),
    )
    with (
        patch(
            "services.owner_action_repository.get_proposal",
            return_value=_register_proposal_two_targets(),
        ),
        patch(
            "services.launch_repository.get_provider_ad_bindings",
            return_value=[
                {
                    "ad_id": "ad-2",
                    "phase": "VERIFIED",
                    "expected_fingerprint": "f" * 64,
                }
            ],
        ),
        patch.object(launch_verify, "_owner_launch_repository", return_value=_Repo()),
    ):
        assert launch_verify.register_executed_launch(run, now=NOW) == "watchdog-skip"

    targets = captured["targets"]
    assert [target.claim_id for target in targets] == ["launch:m:1"]
