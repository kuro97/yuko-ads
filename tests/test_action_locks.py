from __future__ import annotations

import ast
from pathlib import Path

import pytest

import config
from services import action_locks, adset_pause_guard
from services.action_locks import (
    ActionLockOrderError,
    adset_locks,
    launch_execution_lease,
    operation_lock,
)


def test_gateway_and_pause_guard_use_same_physical_adset_lock() -> None:
    assert action_locks._adset_lock_path("123").resolve() == (
        adset_pause_guard._LOCKS_DIR / "adset-123.lock"
    ).resolve()


@pytest.fixture
def isolated_lock_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "approval-operation.lock"
    monkeypatch.setattr(config, "REPORT_CHECKER_OPERATION_LOCK_PATH", path)
    return path


def test_neutral_lock_order_accepts_operation_launch_sorted_adsets(
    isolated_lock_root: Path,
) -> None:
    with operation_lock():
        with launch_execution_lease("operation-1"):
            with adset_locks(("adset-1", "adset-2")):
                assert True
    assert isolated_lock_root.exists()


def test_inverse_repeat_and_unsorted_locks_are_rejected(
    isolated_lock_root: Path,
) -> None:
    with adset_locks(("adset-1",)):
        with pytest.raises(ActionLockOrderError, match="порядок"):
            with launch_execution_lease("operation-1"):
                pass
        with pytest.raises(ActionLockOrderError, match="Вложенный"):
            with adset_locks(("adset-2",)):
                pass

    # Public helper сам нормализует порядок, чтобы caller не мог ошибиться.
    with adset_locks(("adset-2", "adset-1")):
        assert True
    with pytest.raises(ActionLockOrderError, match="Повторный adset_id"):
        with adset_locks(("adset-1", "adset-1")):
            pass


def test_provider_neutral_module_has_no_business_or_provider_imports() -> None:
    source = Path("services/action_locks.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert all("facebook" not in name for name in imported)
    assert all("auto_launch" not in name for name in imported)
    assert all("adset_pause_guard" not in name for name in imported)
