"""Fail-closed настройки персонального Telegram approval."""

from __future__ import annotations

import pytest

from config import (
    OWNER_APPROVAL_REQUIRED,
    OwnerApprovalConfigError,
    load_owner_approval_config,
)


VALID_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456789:AAOwnerApprovalBotTokenForTests",
    "TELEGRAM_CHAT_ID": "-1001234567890",
    "TELEGRAM_OWNER_USER_ID": "424242",
    "APPROVAL_CALLBACK_SECRET": "callback-7Cq9_Zv2-Nm8!Lp4-Rs6@Wx3-Kd5",
    "TELEGRAM_WEBHOOK_SECRET": "webhook-2Jf8_Yt4-Pq6!Bn9-Hm3@Vs7-Lx5",
    "OWNER_ACTION_DB_PATH": "/tmp/owner-actions-test.db",
}


def test_owner_approval_config_is_typed_and_cannot_be_disabled() -> None:
    settings = load_owner_approval_config(VALID_ENV)

    assert OWNER_APPROVAL_REQUIRED is True
    assert settings.owner_user_id == 424242
    assert settings.chat_id == -1001234567890
    assert str(settings.db_path) == "/tmp/owner-actions-test.db"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TELEGRAM_OWNER_USER_ID", ""),
        ("TELEGRAM_OWNER_USER_ID", "not-an-int"),
        ("TELEGRAM_OWNER_USER_ID", "0"),
        ("APPROVAL_CALLBACK_SECRET", "short"),
        ("APPROVAL_CALLBACK_SECRET", "x" * 64),
        ("APPROVAL_CALLBACK_SECRET", "<generate-random-secret>"),
        ("TELEGRAM_WEBHOOK_SECRET", "change-me-" + "a1B2c3D4" * 4),
    ],
)
def test_owner_approval_config_rejects_missing_malformed_or_weak_values(
    name: str,
    value: str,
) -> None:
    environment = {**VALID_ENV, name: value}

    with pytest.raises(OwnerApprovalConfigError):
        load_owner_approval_config(environment)


def test_owner_approval_config_rejects_reused_secrets() -> None:
    environment = {
        **VALID_ENV,
        "TELEGRAM_WEBHOOK_SECRET": VALID_ENV["APPROVAL_CALLBACK_SECRET"],
    }

    with pytest.raises(OwnerApprovalConfigError, match="должен отличаться"):
        load_owner_approval_config(environment)
