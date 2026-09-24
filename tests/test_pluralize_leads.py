"""
Тесты services/formatting.py::pluralize_leads — русское склонение слова «лид».

Часть редизайна Live-отчёта (ARCH-live-report-redesign): «8 лидов» / «1 лид» / «2 лида» должны читаться грамматически верно.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.formatting import pluralize_leads


def test_pluralize_leads_singular():
    """1, 21, 101 → «лид» (заканчивается на 1, кроме 11)."""
    assert pluralize_leads(1) == "лид"
    assert pluralize_leads(21) == "лид"
    assert pluralize_leads(101) == "лид"


def test_pluralize_leads_few():
    """2, 3, 4, 22 → «лида»."""
    assert pluralize_leads(2) == "лида"
    assert pluralize_leads(3) == "лида"
    assert pluralize_leads(4) == "лида"
    assert pluralize_leads(22) == "лида"


def test_pluralize_leads_many():
    """5, 11, 12, 14, 25, 0 → «лидов» (в т.ч. исключение 11-14)."""
    assert pluralize_leads(5) == "лидов"
    assert pluralize_leads(11) == "лидов"
    assert pluralize_leads(12) == "лидов"
    assert pluralize_leads(14) == "лидов"
    assert pluralize_leads(25) == "лидов"
    assert pluralize_leads(0) == "лидов"


def test_pluralize_leads_none():
    """None трактуем как 0 → «лидов»."""
    assert pluralize_leads(None) == "лидов"


def test_pluralize_leads_negative_uses_abs():
    """Отрицательное число — берём abs, склонение как для положительного."""
    assert pluralize_leads(-1) == "лид"
    assert pluralize_leads(-2) == "лида"
    assert pluralize_leads(-5) == "лидов"
