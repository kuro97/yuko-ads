"""
Тест Задачи B: пункт логической консистентности в промпте самокритики сценария.

Владелец поймал противоречие («всё шло отлично с самого начала» vs «решаем проблему клиента»).
В промпт гейта self_critique добавлен явный пункт проверки консистентности
истории/персонажа: проблема vs результат, сегмент vs продукт, город vs продукт,
дедлайны vs текущая дата. Проверяем, что пункт реально присутствует в промпте.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.scenario_validator import _CRITIQUE_SYSTEM_PROMPT


def test_промпт_содержит_пункт_консистентности():
    prompt = _CRITIQUE_SYSTEM_PROMPT.lower()

    # Явное упоминание проверки консистентности/противоречий
    assert "консистентн" in prompt or "противореч" in prompt

    # Все четыре связки из ТЗ
    assert "проблем" in prompt and "результат" in prompt, "проблема vs результат"
    assert "сегмент" in prompt and "prodb" in prompt, "сегмент vs продукт"
    assert "город" in prompt and "продукт" in prompt, "город vs продукт"
    assert "дедлайн" in prompt and ("текущ" in prompt or "прошед" in prompt), "дедлайны vs текущая дата"


def test_формат_вердикта_сохранён():
    """Пункт добавлен, но контракт ответа PASS/BLOCK не сломан."""
    assert "PASS:" in _CRITIQUE_SYSTEM_PROMPT
    assert "BLOCK:" in _CRITIQUE_SYSTEM_PROMPT
