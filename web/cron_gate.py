"""Общий крон-гейт: «час в допустимом наборе + дедуп по дневному слоту».

Обобщает контракт, который сейчас продублирован в 9+ копиях `_should_run_*`
внутри `web/app.py` (_should_run_match_outcomes, _should_run_guardian_sweep,
_should_run_autopilot_live и т.д.): крон бежит только в заданных часах CityA
и только один раз за час (дедуп по ключу "YYYY-MM-DD-HH" в state["slots"]).

Хелпер живёт в `web/` (а не в `services/`), чтобы не создавать импорт-цикл —
он не импортирует ничего из `web.app`, только `datetime` из stdlib.

ВАЖНО: в этой задаче (T-GATE-1) существующие `_should_run_*` в web/app.py
НЕ переключаются на этот хелпер — только сам хелпер + тесты (см. спеку,
docs/specs/ARCH-phase6-engineering.md, T-GATE-1). Миграция вызовов — опционально
и отдельной задачей.
"""

from datetime import datetime


def should_run_hourly_slot(
    now: datetime,
    allowed_hours: frozenset[int] | set[int],
    state: dict,
) -> bool:
    """Проверяет, нужно ли запускать крон в текущий тик.

    Условия (обе обязательны):
    - `now.hour` входит в `allowed_hours`;
    - часовой слот `"{дата}-{час}"` ещё не отмечен как выполненный в `state`.

    State хранит формат `{"slots": {"YYYY-MM-DD-HH": true, ...}}` — тот же,
    что используют существующие `_should_run_*_cron` в web/app.py.

    Args:
        now: текущее время (таймзона на усмотрение вызывающего, обычно CityA).
        allowed_hours: множество часов (0-23), в которые крону разрешено бежать.
        state: словарь состояния крона (обычно загружен из JSON-файла).

    Returns:
        True — час допустим и слот свободен (крону можно запускаться).
        False — час не в наборе, либо слот уже отмечен выполненным.
    """
    if now.hour not in allowed_hours:
        return False
    slot_key = f"{now.date().isoformat()}-{now.hour}"
    slots = state.get("slots", {})
    return not slots.get(slot_key, False)
