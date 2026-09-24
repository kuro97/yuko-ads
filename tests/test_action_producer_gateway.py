"""Durable producer command boundary: UUID4 и payload binding."""

from __future__ import annotations

import uuid

import pytest

from agent.database import init_db
from services.action_producer_gateway import (
    ProducerIdempotencyConflict,
    require_uuid4,
    reserve_idempotency,
)


def test_require_uuid4_rejects_noncanonical_and_non_v4() -> None:
    with pytest.raises(ValueError):
        require_uuid4(str(uuid.uuid1()))
    value = str(uuid.uuid4())
    with pytest.raises(ValueError):
        require_uuid4(value.upper())


def test_command_key_is_durable_and_globally_payload_bound(tmp_path) -> None:
    init_db(str(tmp_path / "decisions.db"), str(tmp_path / "missing.json"))
    key = str(uuid.uuid4())
    assert reserve_idempotency("web:key", {"kind": "PAUSE", "ad_id": "1"}, key) == key
    assert reserve_idempotency("web:key", {"kind": "PAUSE", "ad_id": "1"}, key) == key
    with pytest.raises(ProducerIdempotencyConflict):
        reserve_idempotency("web:key", {"kind": "PAUSE", "ad_id": "2"}, key)
    with pytest.raises(ProducerIdempotencyConflict):
        reserve_idempotency("web:other", {"kind": "UNPAUSE", "ad_id": "1"}, key)
