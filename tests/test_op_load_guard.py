"""Тесты services/op_load_guard.py — страж загрузки отдела продаж."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services import op_load_guard
from services.op_load_guard import (
    MIN_SAMPLES,
    _fetch_window_share,
    check_op_load,
    untouched_status_ids,
)

_NOW = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)


def _lead(status, service=False):
    return {
        "status_id": status,
        "custom_fields_values": [],
        "_embedded": {"tags": [{"id": 1, "name": "Автосделка"}] if service else []},
    }


def test_default_untouched_ids():
    assert untouched_status_ids() == frozenset({32364429, 44520175})


def test_window_share_counts_touched_and_skips_service(monkeypatch):
    captured = {}

    def _get(endpoint, params=None):
        captured.update(params or {})
        return {"_embedded": {"leads": [
            _lead(32364429), _lead(44520175), _lead(99), _lead(100), _lead(99, service=True),
        ]}}

    monkeypatch.setattr("integrations.amo._amo_get", _get)
    monkeypatch.setattr("config.AMO_PIPELINE_ID", 3480844, raising=False)
    share, total = _fetch_window_share(_NOW)
    assert total == 4 and share == 0.5
    assert captured["filter[created_at][from]"] == int((_NOW - timedelta(hours=96)).timestamp())
    assert captured["filter[created_at][to]"] == int((_NOW - timedelta(hours=72)).timestamp())
    assert captured["filter[pipeline_id]"] == 3480844


def _seed(path, shares, leads=50, hours_ago_start=48):
    samples = []
    for i, s in enumerate(shares):
        at = _NOW - timedelta(hours=hours_ago_start - i)
        samples.append({"at": at.isoformat(), "share": s, "leads": leads})
    path.write_text(json.dumps({"samples": samples}), encoding="utf-8")


def test_no_baseline_does_not_block(tmp_path):
    state = tmp_path / "g.json"
    with patch.object(op_load_guard, "_fetch_window_share", return_value=(0.05, 40)):
        v = check_op_load(now=_NOW, state_path=state)
    assert v.ok is True and v.reason == "no_baseline" and v.share == 0.05
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert len(saved["samples"]) == 1


def test_overloaded_when_share_below_half_of_baseline(tmp_path):
    state = tmp_path / "g.json"
    _seed(state, [0.4] * MIN_SAMPLES)
    with patch.object(op_load_guard, "_fetch_window_share", return_value=(0.1, 40)):
        v = check_op_load(now=_NOW, state_path=state)
    assert v.ok is False and v.reason == "overloaded" and v.baseline == 0.4


def test_ok_when_share_normal(tmp_path):
    state = tmp_path / "g.json"
    _seed(state, [0.4] * MIN_SAMPLES)
    with patch.object(op_load_guard, "_fetch_window_share", return_value=(0.35, 40)):
        v = check_op_load(now=_NOW, state_path=state)
    assert v.ok is True and v.reason == "ok"


def test_few_leads_does_not_block(tmp_path):
    state = tmp_path / "g.json"
    _seed(state, [0.4] * MIN_SAMPLES)
    with patch.object(op_load_guard, "_fetch_window_share", return_value=(0.0, 5)):
        v = check_op_load(now=_NOW, state_path=state)
    assert v.ok is True and v.reason == "few_leads"


def test_fresh_sample_reused_without_amo(tmp_path):
    state = tmp_path / "g.json"
    _seed(state, [0.4], hours_ago_start=0)  # замер только что
    with patch.object(op_load_guard, "_fetch_window_share") as fetch:
        v = check_op_load(now=_NOW + timedelta(minutes=10), state_path=state)
    fetch.assert_not_called()
    assert v.share == 0.4


def test_amo_error_is_unknown_and_blocks(tmp_path):
    state = tmp_path / "g.json"
    with patch.object(op_load_guard, "_fetch_window_share", side_effect=RuntimeError("amo down")):
        v = check_op_load(now=_NOW, state_path=state)
    assert v.ok is False and v.reason == "unknown"
