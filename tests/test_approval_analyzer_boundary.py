"""Регрессии read-only analyzer и единственной typed mutation boundary."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from integrations import facebook_ads_mutation_transport as transport

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_analyzer_has_no_mutator_surface() -> None:
    from agent import analyzer

    forbidden = {
        "pause_ad",
        "unpause_ad",
        "_pause_ad_unchecked",
        "_unpause_ad_unchecked",
        "_set_ad_status_unchecked",
    }
    assert forbidden.isdisjoint(vars(analyzer))


def test_analyzer_ast_contains_no_provider_write() -> None:
    source = (_PROJECT_ROOT / "agent/analyzer.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    write_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"post", "delete"}
    }
    assert write_calls == set()


def test_transport_public_api_is_typed_and_has_no_irreversible_operation() -> None:
    assert set(transport.__all__) >= {
        "create_ad",
        "set_ad_status",
        "set_adset_budget",
    }
    assert {"delete", "archive", "rename", "post"}.isdisjoint(transport.__all__)
    for function_name in ("create_ad", "set_ad_status", "set_adset_budget"):
        parameters = tuple(inspect.signature(getattr(transport, function_name)).parameters)
        assert parameters[0] == "attestation"


def test_legacy_scale_module_has_no_provider_writer() -> None:
    source = (_PROJECT_ROOT / "services/action_remote_scale.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.FunctionDef) and "unchecked" in node.name
        for node in ast.walk(tree)
    )
    assert "_throttled_post" not in source
