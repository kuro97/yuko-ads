"""Truthful one-shot контракт typed PAUSE/UNPAUSE transport."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from agent.analyzer import _read_ad_status
from integrations import facebook_ads_mutation_transport as transport
from services.owner_action_models import ActionAttemptAttestation

_HASH = "a" * 64


def _resp(status_code: int, json_data: dict | None = None, text: str = ""):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    response.text = text
    return response


def _attestation(operation_kind: str) -> ActionAttemptAttestation:
    return ActionAttemptAttestation(
        attempt_id="attempt-1",
        permit_id="permit-1",
        proposal_id="proposal-1",
        decision_id="decision-1",
        claim_id="claim-1",
        operation_kind=operation_kind,
        account_id="act_456",
        resource_id="123",
        payload_sha256=_HASH,
        consumed_at=datetime.now(timezone.utc) - timedelta(seconds=1),
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


def _set_status(operation_kind: str, status: str) -> bool:
    attempt = _attestation(operation_kind)
    return transport.set_ad_status(
        attempt,
        account_id=attempt.account_id,
        ad_id=attempt.resource_id,
        status=status,
        payload_sha256=attempt.payload_sha256,
    )


@pytest.mark.parametrize(
    ("operation_kind", "target_status"),
    [("PAUSE_AD", "PAUSED"), ("UNPAUSE_AD", "ACTIVE")],
)
@pytest.mark.parametrize("status_code", [200, 201, 204])
def test_attested_status_accepts_2xx_with_one_write(
    monkeypatch,
    operation_kind,
    target_status,
    status_code,
):
    monkeypatch.setattr(
        transport,
        "_read_persisted_attempt",
        lambda value: _persisted(value),
    )
    remote_post = MagicMock(return_value=_resp(status_code))
    monkeypatch.setattr(transport, "_do_throttled_request", remote_post)
    monkeypatch.setattr(transport, "get_fb_token", lambda: "secret-token")

    assert _set_status(operation_kind, target_status) is True

    remote_post.assert_called_once()
    assert remote_post.call_args.args[1].endswith("/123")
    assert remote_post.call_args.kwargs["data"] == {
        "access_token": "secret-token",
        "status": target_status,
    }


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_definitive_4xx_is_sanitized_rejection(monkeypatch, status_code):
    monkeypatch.setattr(
        transport,
        "_read_persisted_attempt",
        lambda value: _persisted(value),
    )
    remote_post = MagicMock(
        return_value=_resp(status_code, text="token=secret ad=123")
    )
    monkeypatch.setattr(transport, "_do_throttled_request", remote_post)
    monkeypatch.setattr(transport, "get_fb_token", lambda: "secret-token")

    with pytest.raises(transport.MutationRejected) as caught:
        _set_status("PAUSE_AD", "PAUSED")

    remote_post.assert_called_once()
    assert str(caught.value) == "FACEBOOK_MUTATION_REJECTED"
    assert "secret" not in str(caught.value)
    assert "123" not in str(caught.value)


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 502, 503])
def test_ambiguous_http_result_never_reports_success(monkeypatch, status_code):
    monkeypatch.setattr(
        transport,
        "_read_persisted_attempt",
        lambda value: _persisted(value),
    )
    remote_post = MagicMock(
        return_value=_resp(status_code, text="token=secret ad=123")
    )
    monkeypatch.setattr(transport, "_do_throttled_request", remote_post)
    monkeypatch.setattr(transport, "get_fb_token", lambda: "secret-token")

    with pytest.raises(transport.MutationOutcomeUnknown):
        _set_status("PAUSE_AD", "PAUSED")

    remote_post.assert_called_once()


@pytest.mark.parametrize(
    "transport_error",
    [TimeoutError("raw timeout token=secret"), ConnectionError("raw network failure")],
)
def test_transport_error_is_ambiguous_and_sanitized(monkeypatch, transport_error):
    monkeypatch.setattr(
        transport,
        "_read_persisted_attempt",
        lambda value: _persisted(value),
    )
    remote_post = MagicMock(side_effect=transport_error)
    monkeypatch.setattr(transport, "_do_throttled_request", remote_post)
    monkeypatch.setattr(transport, "get_fb_token", lambda: "secret-token")

    with pytest.raises(transport.MutationOutcomeUnknown) as caught:
        _set_status("PAUSE_AD", "PAUSED")

    remote_post.assert_called_once()
    assert "secret" not in str(caught.value)


def test_token_failure_happens_after_attestation_but_before_remote_post(monkeypatch):
    monkeypatch.setattr(
        transport,
        "_read_persisted_attempt",
        lambda value: _persisted(value),
    )
    remote_post = MagicMock()
    monkeypatch.setattr(transport, "_do_throttled_request", remote_post)
    monkeypatch.setattr(
        transport,
        "get_fb_token",
        MagicMock(side_effect=RuntimeError("raw secret")),
    )

    with pytest.raises(
        transport.MutationRejected,
        match="FACEBOOK_CREDENTIALS_UNAVAILABLE",
    ):
        _set_status("PAUSE_AD", "PAUSED")

    remote_post.assert_not_called()


def test_analyzer_exposes_only_read_status_helper():
    from agent import analyzer

    assert not hasattr(analyzer, "pause_ad")
    assert not hasattr(analyzer, "unpause_ad")
    assert not hasattr(analyzer, "_set_ad_status_unchecked")


def test_read_ad_status_success():
    with patch(
        "agent.analyzer._throttled_get",
        return_value=_resp(200, {"status": "PAUSED"}),
    ), patch("agent.analyzer.get_fb_token", return_value="token"):
        assert _read_ad_status("123") == "PAUSED"


def test_read_ad_status_fb_error_returns_none():
    with patch(
        "agent.analyzer._throttled_get",
        return_value=_resp(400, text="error"),
    ), patch("agent.analyzer.get_fb_token", return_value="token"), patch(
        "agent.analyzer.logger"
    ) as mock_logger:
        assert _read_ad_status("123") is None
    assert mock_logger.error.called


def test_read_ad_status_exception_returns_none():
    with patch(
        "agent.analyzer._throttled_get",
        side_effect=ConnectionError("network down"),
    ), patch("agent.analyzer.get_fb_token", return_value="token"), patch(
        "agent.analyzer.logger"
    ) as mock_logger:
        assert _read_ad_status("123") is None
    assert mock_logger.error.called
