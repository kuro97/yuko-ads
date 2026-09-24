"""
Тесты мастер-выключателя авто-генератора ТЗ (_cron_brief_generator).

Флаг settings.brief_generator.enabled гейтит запуск крона (дефолт OFF — генератор
на редизайне, старой логикой автопостинг в Trello не должен улетать).
Ручной эндпоинт /api/autopilot/generate-briefs-now этим флагом НЕ гейтится.

Мокаем:
- agent.scheduler.load_settings — флаг мастер-выключателя
- web.app.datetime — фиксируем "текущее" время (понедельник 08:xx CityA)
- services.brief_generator.generate_and_push_briefs — сама генерация не важна
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

_TZ_LOCAL = timezone(timedelta(hours=5))
_MONDAY_08 = datetime(2026, 6, 29, 8, 15, tzinfo=_TZ_LOCAL)  # понедельник, 08:15 CityA
# Каденция x2 в неделю по решению владельца (2026-07-08) — второй слот, четверг
_THURSDAY_08 = datetime(2026, 7, 2, 8, 15, tzinfo=_TZ_LOCAL)  # четверг, 08:15 CityA
_TUESDAY_08 = datetime(2026, 6, 30, 8, 15, tzinfo=_TZ_LOCAL)  # вторник, 08:15 CityA (не рабочий день крона)


class _FixedDatetime(datetime):
    """Подмена datetime.now() — фиксирует конкретный момент времени для крона."""

    _fixed = _MONDAY_08

    @classmethod
    def now(cls, tz=None):
        return cls._fixed if tz else cls._fixed.replace(tzinfo=None)


@pytest.fixture
def brief_env(tmp_path):
    """Патчит state брифов на tmp_path, чтобы should_run_generator не блокировал по интервалу."""
    with patch("services.brief_generator.STATE_FILE", tmp_path / "brief_state.json"):
        yield


def test_cron_skips_when_flag_missing(brief_env):
    """Ключ brief_generator отсутствует в settings → крон НЕ зовёт generate_and_push_briefs."""
    from web.app import _cron_brief_generator

    with patch("agent.scheduler.load_settings", return_value={}), \
         patch("web.app.datetime", _FixedDatetime), \
         patch("services.brief_generator.generate_and_push_briefs") as mock_generate:
        _cron_brief_generator()

    mock_generate.assert_not_called()


def test_cron_skips_when_flag_disabled(brief_env):
    """brief_generator.enabled=False → крон НЕ зовёт generate_and_push_briefs."""
    from web.app import _cron_brief_generator

    settings = {"brief_generator": {"enabled": False}}
    with patch("agent.scheduler.load_settings", return_value=settings), \
         patch("web.app.datetime", _FixedDatetime), \
         patch("services.brief_generator.generate_and_push_briefs") as mock_generate:
        _cron_brief_generator()

    mock_generate.assert_not_called()


def test_cron_runs_when_flag_enabled_monday_8am(brief_env):
    """brief_generator.enabled=True + понедельник 08:xx → крон зовёт generate_and_push_briefs."""
    from web.app import _cron_brief_generator

    settings = {"brief_generator": {"enabled": True}}
    with patch("agent.scheduler.load_settings", return_value=settings), \
         patch("web.app.datetime", _FixedDatetime), \
         patch(
             "services.brief_generator.generate_and_push_briefs",
             return_value={"created": 0, "skipped": 0, "error": None},
         ) as mock_generate:
        _cron_brief_generator()

    mock_generate.assert_called_once()


def test_cron_enabled_but_wrong_hour_skips(brief_env):
    """brief_generator.enabled=True, но не понедельник 08:xx → крон НЕ зовёт генерацию."""
    from web.app import _cron_brief_generator

    class _WrongHour(_FixedDatetime):
        _fixed = _MONDAY_08.replace(hour=10)

    settings = {"brief_generator": {"enabled": True}}
    with patch("agent.scheduler.load_settings", return_value=settings), \
         patch("web.app.datetime", _WrongHour), \
         patch("services.brief_generator.generate_and_push_briefs") as mock_generate:
        _cron_brief_generator()

    mock_generate.assert_not_called()


def test_cron_runs_when_flag_enabled_thursday_8am(brief_env):
    """Каденция x2 (2026-07-08): brief_generator.enabled=True + четверг 08:xx →
    крон тоже зовёт generate_and_push_briefs (второй слот недели)."""
    from web.app import _cron_brief_generator

    class _Thursday(_FixedDatetime):
        _fixed = _THURSDAY_08

    settings = {"brief_generator": {"enabled": True}}
    with patch("agent.scheduler.load_settings", return_value=settings), \
         patch("web.app.datetime", _Thursday), \
         patch(
             "services.brief_generator.generate_and_push_briefs",
             return_value={"created": 0, "skipped": 0, "error": None},
         ) as mock_generate:
        _cron_brief_generator()

    mock_generate.assert_called_once()


def test_cron_enabled_but_tuesday_skips(brief_env):
    """Вторник 08:xx — не рабочий день крона (пн/чт) → генерация не вызывается."""
    from web.app import _cron_brief_generator

    class _Tuesday(_FixedDatetime):
        _fixed = _TUESDAY_08

    settings = {"brief_generator": {"enabled": True}}
    with patch("agent.scheduler.load_settings", return_value=settings), \
         patch("web.app.datetime", _Tuesday), \
         patch("services.brief_generator.generate_and_push_briefs") as mock_generate:
        _cron_brief_generator()

    mock_generate.assert_not_called()


def test_manual_endpoint_not_gated_by_flag():
    """Ручной эндпоинт generate_and_push_briefs НЕ проверяет флаг — вызывается напрямую."""
    from services.brief_generator import generate_and_push_briefs

    # Ручной путь (web/app.py:generate_briefs_now) зовёт generate_and_push_briefs
    # без какой-либо проверки settings — убеждаемся что сама функция не читает флаг.
    with patch("agent.scheduler.load_settings") as mock_load_settings, \
         patch("services.brief_generator._get_fresh_ads", return_value=[]):
        result = generate_and_push_briefs(max_briefs=1)

    mock_load_settings.assert_not_called()
    assert result is not None
