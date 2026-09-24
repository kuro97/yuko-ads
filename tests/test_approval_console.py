"""T24: Telegram console публикует только проверенные typed результаты."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _function_calls(name: str) -> set[str]:
    tree = ast.parse((ROOT / "services/telegram_console.py").read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    )
    calls: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return calls


def test_status_and_ads_use_checked_report_delivery() -> None:
    for function_name in ("_cmd_status", "_cmd_ads"):
        calls = _function_calls(function_name)
        assert "send_checked_report" in calls
        assert "send_telegram" not in calls
        assert "send_with_buttons" not in calls


def test_scale_and_launch_create_owner_proposals_only() -> None:
    for function_name in ("_run_scale_async", "_run_launch_async"):
        calls = _function_calls(function_name)
        assert "_persist_telegram_proposal" in calls
        assert "_deliver_owner_proposals" in calls
        assert "send_action_outcome" not in calls
        assert "execute_action" not in calls
        assert "send_telegram" not in calls


def test_only_code_owned_fact_free_progress_is_sent_directly() -> None:
    for function_name in ("_cmd_scale", "_cmd_launch"):
        calls = _function_calls(function_name)
        assert "send_fact_free" in calls
        assert "send_telegram" not in calls
