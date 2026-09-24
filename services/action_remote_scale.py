"""Совместимые типы результата SCALE без provider write boundary.

Модуль ничего не знает о producer-ах, checker-е, permit-ах и lock-ах. Все эти
проверки обязан завершить SCALE adapter до вызова функции ниже.
"""

from __future__ import annotations

import re
from decimal import Decimal

_ADSET_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_MINOR_UNITS_PER_USD = Decimal("100")


class BudgetMutationError(RuntimeError):
    """Безопасная базовая ошибка закрытой FB-мутации бюджета."""


class BudgetMutationRejected(BudgetMutationError):
    """Facebook однозначно отклонил запрос и не подтвердил изменение."""

    def __init__(self, *, status_code: int, provider_code: int | None) -> None:
        self.status_code = status_code
        self.provider_code = provider_code
        super().__init__("facebook_budget_rejected")


class BudgetMutationOutcomeUnknown(BudgetMutationError):
    """Ответ неоднозначен: результат нужно выяснять только чтением."""

    def __init__(self) -> None:
        super().__init__("facebook_budget_outcome_unknown")


def _budget_to_minor_units(new_budget_usd: Decimal) -> int:
    """Преобразует точную сумму USD в центы без скрытого округления."""
    if not isinstance(new_budget_usd, Decimal):
        raise TypeError("new_budget_usd_must_be_decimal")
    if not new_budget_usd.is_finite() or new_budget_usd <= 0:
        raise ValueError("new_budget_usd_invalid")

    minor_units = new_budget_usd * _MINOR_UNITS_PER_USD
    integral_minor_units = minor_units.to_integral_value()
    if minor_units != integral_minor_units:
        raise ValueError("new_budget_usd_has_fractional_cent")
    return int(integral_minor_units)

