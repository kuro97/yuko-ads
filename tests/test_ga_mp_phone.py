"""Нормализация телефона для GA4 MP (integrations/ga_mp._normalize_phone).

Правило общее для любой страны: «+» и только цифры, без переписывания
префиксов и без дописывания кода страны.
"""

import pytest

from integrations.ga_mp import _normalize_phone


@pytest.mark.parametrize("raw, expected", [
    ("+10000000001", "+10000000001"),
    ("+1 (000) 000-00-01", "+10000000001"),
    ("10000000001", "+10000000001"),
    (" +44 20 0000 0000 ", "+442000000000"),
    # Национальный формат не переписывается: код страны не угадываем
    ("80000000001", "+80000000001"),
    ("0000000001", "+0000000001"),
])
def test_normalize_phone_keeps_digits_without_country_rewrite(raw, expected):
    assert _normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "   ", "нет номера", "+"])
def test_normalize_phone_empty_without_digits(raw):
    assert _normalize_phone(raw) == ""
