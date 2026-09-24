"""Фиксированный закрытый реестр provider adapters.

Импорты concrete adapters намеренно ленивые: shadow-проверка не должна даже
загружать provider mutation-модули.
"""

from __future__ import annotations

from services.action_gateway_core import ActionAdapter
from services.approval_checker_models import ActionKind


def _adapter_for_kind(kind: ActionKind) -> ActionAdapter:
    """Создаёт единственный разрешённый adapter для точного ActionKind."""

    if not isinstance(kind, ActionKind):
        raise TypeError("kind должен быть ActionKind")
    if kind is ActionKind.LAUNCH:
        from services.action_adapter_launch import LaunchActionAdapter

        return LaunchActionAdapter()
    if kind is ActionKind.ASSET_RECOVERY:
        from services.action_adapter_asset_recovery import AssetRecoveryActionAdapter

        return AssetRecoveryActionAdapter()
    if kind is ActionKind.PAUSE:
        from services.action_adapter_pause import PauseActionAdapter

        return PauseActionAdapter()
    if kind is ActionKind.UNPAUSE:
        from services.action_adapter_pause import UnpauseActionAdapter

        return UnpauseActionAdapter()
    if kind is ActionKind.SCALE:
        from services.action_adapter_scale import ScaleActionAdapter

        return ScaleActionAdapter()
    # ActionKind не содержит DELETE/ARCHIVE; защитная ветка нужна при порче enum.
    raise ValueError("Неподдерживаемый ActionKind")


class _FixedAdapterRegistry:
    """Внутренний registry для read-only crash reconciliation."""

    __slots__ = ()

    def for_kind(self, kind: ActionKind) -> ActionAdapter:
        return _adapter_for_kind(kind)
