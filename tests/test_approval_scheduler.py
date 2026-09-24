"""T21: legacy scheduler больше не обходит approval gateway."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from agent import scheduler


ROOT = Path(__file__).resolve().parents[1]


def _candidate(ad_id: str) -> dict:
    return {
        "id": ad_id,
        "name": f"Реклама {ad_id}",
        "recommendation": "ОТКЛЮЧИТЬ",
        "reason": "дорого",
    }


def _prepare(monkeypatch) -> None:
    monkeypatch.setattr(
        scheduler, "load_settings", lambda: {"auto_apply": True, "thresholds": {}}
    )
    monkeypatch.setattr(scheduler, "_log_batch", Mock())
    monkeypatch.setattr(scheduler, "save_settings", Mock())


def test_scheduler_has_no_raw_pause_import_or_call() -> None:
    tree = ast.parse((ROOT / "agent/scheduler.py").read_text(encoding="utf-8"))
    forbidden = {"pause_ad", "safe_pause_or_enqueue_replacement"}
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert imported.isdisjoint(forbidden)
    assert called.isdisjoint(forbidden)
    assert "execute_pause" in imported


def test_scheduler_stops_batch_after_first_non_confirmed(monkeypatch) -> None:
    _prepare(monkeypatch)
    monkeypatch.setattr(scheduler, "analyze_all", lambda **_: [_candidate("1"), _candidate("2")])
    execute = Mock(
        return_value=SimpleNamespace(
            action="DENIED", confirmed=False, run=None, reason="APPROVAL_DENIED"
        )
    )
    save = Mock()
    monkeypatch.setattr("services.action_producer_gateway.execute_pause", execute)
    monkeypatch.setattr(scheduler, "save_decision", save)

    result = scheduler.daily_analysis()

    execute.assert_called_once()
    assert execute.call_args.args == ("1",)
    save.assert_not_called()
    assert [item["ad_id"] for item in result["actions"]] == ["1"]
    assert result["actions"][0]["success"] is False


def test_scheduler_records_only_confirmed_operation_effect(monkeypatch) -> None:
    _prepare(monkeypatch)
    monkeypatch.setattr(scheduler, "analyze_all", lambda **_: [_candidate("1")])
    run = SimpleNamespace(operation_id="operation-1")
    monkeypatch.setattr(
        "services.action_producer_gateway.execute_pause",
        Mock(
            return_value=SimpleNamespace(
                action="CONFIRMED", confirmed=True, run=run, reason=None
            )
        ),
    )
    save = Mock(return_value=True)
    monkeypatch.setattr(scheduler, "save_decision", save)

    result = scheduler.daily_analysis()

    assert result["actions"][0]["success"] is True
    save.assert_called_once()
    assert save.call_args.kwargs["effect_id"] == "operation-1:decision:PAUSED"

