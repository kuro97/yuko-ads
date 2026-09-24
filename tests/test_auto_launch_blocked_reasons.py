"""
Причина отказа по каждой карточке авто-запуска (волна B).

До фикса при заблокированных карточках в логе оставалось только «нет карточек,
разрешённых launch checker», а Telegram-отчёт печатал «Заблокировано: N» —
владелец не видел, ПОЧЕМУ каждая карточка не прошла. Проверяем:

- лог: warning на карточку с именем, id, кодом и причиной (причина ≤ ~200 симв.)
- Telegram (план и активный отчёт): под «Заблокировано: N» строки
  «• имя — коды: первая причина» (≤ 120 симв.), не больше 10, остаток «… и ещё K»
- state: last_run_blocked/last_run_at пишутся на завершении прогона — их читает
  утренний дайджест
"""

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта (как в tests/test_auto_launch.py)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from services.auto_launch import (  # noqa: E402
    _BLOCKED_REPORT_MAX_LINES,
    _blocked_entry,
    _blocked_report_lines,
    _build_active_report,
    _build_dry_run_report,
    _load_auto_launch_state,
    _log_blocked_entry,
    _send_active_telegram,
    _send_dry_run_telegram,
    run_auto_launch,
)
from services.launch_checker import LaunchCheckBlocked  # noqa: E402


_CARDS = [
    {"id": "card-veto", "name": "Бонус / Бесплатный доступ", "labels": ["PRODA"], "pos": 1.0},
    {"id": "card-cap", "name": "Сидорова / Заявка на PRODB", "labels": ["PRODB"], "pos": 2.0},
]

_CODES = {
    "card-veto": ("TOPIC_VETO", "Тема «бонус» — вето: мёртвая тема по журналу решений"),
    "card-cap": ("CAPACITY_BLOCKED", "В кабинете нет свободных слотов адсетов"),
}


class _BlockingChecker:
    """Checker-заглушка: режет каждую карточку своим кодом и причиной."""

    def __init__(self, codes_by_card: dict):
        self.codes_by_card = codes_by_card

    def prepare_and_reserve(self, card, request, state):
        code, reason = self.codes_by_card[card["id"]]
        raise LaunchCheckBlocked(code, (reason,), f"check-{card['id']}")


def _entry(name: str, code: str, reason: str, card_id: str = "card-x") -> dict:
    return {
        "card_id": card_id,
        "card_name": name,
        "check_id": None,
        "reason_codes": [code],
        "reasons": [reason],
    }


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """State и lock авто-запуска — во временный каталог (не трогаем реальный data/)."""
    state_file = tmp_path / "auto_launch_state.json"
    monkeypatch.setattr("services.auto_launch._AUTO_LAUNCH_STATE_FILE", state_file)
    monkeypatch.setattr(
        "services.auto_launch._AUTO_LAUNCH_RUN_LOCK_FILE",
        tmp_path / "auto-launch-active.lock",
    )
    return state_file


def _run_dry_run(cards, checker, sent: list) -> dict:
    def fake_send(text, channel="ads"):
        sent.append(text)
        return True

    with patch("services.auto_launch._get_autopilot_config", return_value={
        "enabled": False,
        "kill_switch": False,
        "launch_enabled": False,
        "max_launches_per_day": 1,
        "launch_checker": {"mode": "observe"},
    }), patch("services.auto_launch._analyze_coverage", return_value={}), \
         patch("services.auto_launch._get_done_list_id", return_value="list1"), \
         patch("services.auto_launch._get_unlaunched_cards", return_value=cards), \
         patch("services.auto_launch._get_launch_checker", return_value=checker), \
         patch("services.auto_launch._send_telegram", side_effect=fake_send):
        return run_auto_launch(mode="dry_run", max_launches=1)


# ---------------------------------------------------------------------------
# Сквозной прогон: лог + Telegram + state
# ---------------------------------------------------------------------------

class TestRunAutoLaunchBlockedReasons:

    def test_лог_содержит_warning_с_именем_id_кодом_и_причиной(self, isolated_state, caplog):
        caplog.set_level(logging.WARNING, logger="services.auto_launch")
        sent: list = []

        result = _run_dry_run(_CARDS, _BlockingChecker(_CODES), sent)

        assert result["blocked_count"] == 2
        assert result["skipped_reason"] == "нет карточек, разрешённых launch checker"
        warnings = [
            rec.getMessage() for rec in caplog.records
            if rec.levelno == logging.WARNING and "заблокирована" in rec.getMessage()
        ]
        assert len(warnings) == 2, caplog.text
        veto_line = next(line for line in warnings if "card-veto" in line)
        assert veto_line.startswith("auto_launch: карточка «Бонус / Бесплатный доступ» (card-veto) заблокирована: TOPIC_VETO — ")
        assert "мёртвая тема по журналу решений" in veto_line
        cap_line = next(line for line in warnings if "card-cap" in line)
        assert "CAPACITY_BLOCKED" in cap_line
        assert "Сидорова / Заявка на PRODB" in cap_line

    def test_telegram_содержит_имя_карточки_и_код(self, isolated_state):
        sent: list = []

        _run_dry_run(_CARDS, _BlockingChecker(_CODES), sent)

        assert len(sent) == 1, sent
        report = sent[0]
        assert "Заблокировано: 2" in report
        assert "• Бонус / Бесплатный доступ — TOPIC_VETO: Тема «бонус» — вето: мёртвая тема по журналу решений" in report
        assert "• Сидорова / Заявка на PRODB — CAPACITY_BLOCKED: В кабинете нет свободных слотов адсетов" in report
        # Расшифровка идёт СРАЗУ под строкой со счётчиком
        assert report.index("Заблокировано: 2") < report.index("• Бонус")

    def test_state_хранит_срез_last_run_blocked(self, isolated_state):
        sent: list = []

        _run_dry_run(_CARDS, _BlockingChecker(_CODES), sent)

        assert isolated_state.exists(), "state должен быть записан на завершении прогона"
        state = _load_auto_launch_state()
        assert state["last_run_mode"] == "dry_run"
        assert state["last_run_at"], "last_run_at обязателен — по нему дайджест отсекает старые прогоны"
        snapshot = state["last_run_blocked"]
        assert [item["card_name"] for item in snapshot] == [
            "Бонус / Бесплатный доступ", "Сидорова / Заявка на PRODB",
        ]
        assert snapshot[0]["reason_codes"] == ["TOPIC_VETO"]
        assert snapshot[0]["reason"] == "Тема «бонус» — вето: мёртвая тема по журналу решений"
        assert snapshot[1]["card_id"] == "card-cap"

    def test_без_блокировок_срез_пустой(self, isolated_state):
        """Нет карточек вообще → last_run_blocked=[] (дайджест ничего не покажет)."""
        sent: list = []

        _run_dry_run([], _BlockingChecker({}), sent)

        state = _load_auto_launch_state()
        assert state["last_run_blocked"] == []
        assert state["last_run_at"]


# ---------------------------------------------------------------------------
# Лог: формат и обрезка
# ---------------------------------------------------------------------------

class TestLogBlockedEntry:

    def test_несколько_кодов_и_причин_в_одну_строку(self, caplog):
        caplog.set_level(logging.WARNING, logger="services.auto_launch")
        entry = _entry("Иванов / Тема В", "A_CODE", "первая причина", card_id="c1")
        entry["reason_codes"] = ["A_CODE", "B_CODE"]
        entry["reasons"] = ["первая причина", "вторая причина"]

        _log_blocked_entry(entry)

        assert caplog.messages == [
            "auto_launch: карточка «Иванов / Тема В» (c1) заблокирована: "
            "A_CODE, B_CODE — первая причина; вторая причина"
        ]

    def test_длинная_причина_обрезается_до_200(self, caplog):
        caplog.set_level(logging.WARNING, logger="services.auto_launch")
        entry = _entry("Карточка", "LONG", "п" * 500)

        _log_blocked_entry(entry)

        reasons_part = caplog.messages[0].split(" — ", 1)[1]
        assert len(reasons_part) <= 200
        assert reasons_part.endswith("…")

    def test_причина_с_токеном_режется_в_blocked_entry(self):
        exc = LaunchCheckBlocked(
            "PROVIDER_ERROR",
            ("GET https://graph.facebook.com/x?access_token=TOP_SECRET&limit=1",),
            None,
        )
        entry = _blocked_entry({"id": "c1", "name": "Карточка"}, exc)
        assert "TOP_SECRET" not in entry["reasons"][0]
        assert "<redacted>" in entry["reasons"][0]


# ---------------------------------------------------------------------------
# Telegram-отчёты: строки отказов
# ---------------------------------------------------------------------------

class TestBlockedReportLines:

    def test_пусто(self):
        assert _blocked_report_lines(None) == []
        assert _blocked_report_lines([]) == []

    def test_не_больше_десяти_и_хвост(self):
        blocked = [_entry(f"Карточка {i}", "TOPIC_VETO", "вето") for i in range(13)]

        lines = _blocked_report_lines(blocked)

        assert len(lines) == _BLOCKED_REPORT_MAX_LINES + 1
        assert lines[0] == "• Карточка 0 — TOPIC_VETO: вето"
        assert lines[-2] == "• Карточка 9 — TOPIC_VETO: вето"
        assert lines[-1] == "… и ещё 3"

    def test_ровно_десять_без_хвоста(self):
        blocked = [_entry(f"К{i}", "X", "y") for i in range(10)]
        lines = _blocked_report_lines(blocked)
        assert len(lines) == 10
        assert not lines[-1].startswith("…")

    def test_первая_причина_обрезается_до_120(self):
        entry = _entry("Карточка", "LONG", "а" * 300)
        entry["reasons"] = ["а" * 300, "вторая — не показываем"]

        [line] = _blocked_report_lines([entry])

        reason = line.split(": ", 1)[1]
        assert len(reason) <= 120
        assert reason.endswith("…")
        assert "вторая" not in line

    def test_html_экранирование_имени_и_причины(self):
        entry = _entry("Тест <b>жирный</b>", "CODE", "a < b & c")
        [line] = _blocked_report_lines([entry])
        assert "&lt;b&gt;" in line
        assert "a &lt; b &amp; c" in line
        assert "<b>" not in line

    def test_без_причины_и_кода_честные_заглушки(self):
        entry = {"card_id": "c1", "card_name": "", "reason_codes": [], "reasons": []}
        [line] = _blocked_report_lines([entry])
        assert line == "• c1 — LAUNCH_CHECK_BLOCKED: причина не указана"


class TestDryRunReportBlocked:

    def test_план_пустой_с_отказами(self):
        blocked = [_entry("Бонус / Бесплатный доступ", "TOPIC_VETO", "Тема «бонус» — вето")]

        text = _build_dry_run_report(
            [], raw_count=1, eligible_count=0, blocked_count=1, blocked=blocked,
        )

        assert "Заблокировано: 1\n• Бонус / Бесплатный доступ — TOPIC_VETO: Тема «бонус» — вето" in text

    def test_план_с_рекомендациями_и_отказами(self):
        rec = {"card_id": "c1", "card_name": "Петров / Тема А",
               "campaign_type": "leadgen", "cities": None}
        blocked = [_entry("Бонус / Бесплатный доступ", "TOPIC_VETO", "вето")]

        text = _build_dry_run_report(
            [rec], raw_count=2, eligible_count=1, blocked_count=1, blocked=blocked,
        )

        assert "Петров / Тема А" in text
        assert text.index("Заблокировано: 1") < text.index("• Бонус / Бесплатный доступ — TOPIC_VETO: вето")

    def test_без_blocked_формат_прежний(self):
        text = _build_dry_run_report([], raw_count=3, eligible_count=0, blocked_count=3)
        assert text.endswith("Заблокировано: 3")
        assert "•" not in text

    def test_send_dry_run_пробрасывает_blocked(self):
        sent: list = []
        blocked = [_entry("Бонус / Бесплатный доступ", "TOPIC_VETO", "вето")]

        _send_dry_run_telegram(
            [], lambda text, channel="ads": sent.append(text),
            raw_count=1, eligible_count=0, blocked_count=1, blocked=blocked,
        )

        assert "• Бонус / Бесплатный доступ — TOPIC_VETO: вето" in sent[0]


class TestActiveReportBlocked:

    def _rec(self):
        return {
            "card_id": "c1",
            "card_name": "Петров / Тема А",
            "campaign_type": "leadgen",
            "cities": ["CityB"],
            "ad_ids_by_city": {"CityB": ["111"]},
            "ad_ids": ["111"],
            "daily_budget_usd": 10.0,
        }

    def test_отказы_под_строкой_счётчика(self):
        blocked = [_entry("Бонус / Бесплатный доступ", "TOPIC_VETO", "вето")]

        text, _ = _build_active_report(
            [self._rec()], 2, raw_count=4, eligible_count=3, blocked_count=1, blocked=blocked,
        )

        assert "Заблокировано: 1\n• Бонус / Бесплатный доступ — TOPIC_VETO: вето" in text
        # Строки отказов — часть футера, до оценки через 48 часов
        assert text.index("• Бонус") < text.index("Первая оценка")

    def test_send_active_пробрасывает_blocked(self):
        sent: list = []
        blocked = [_entry("Бонус / Бесплатный доступ", "TOPIC_VETO", "вето")]

        with patch("services.auto_launch._send_with_buttons", return_value=False):
            _send_active_telegram(
                [self._rec()], lambda text, channel="ads": sent.append(text),
                raw_count=4, eligible_count=3, blocked_count=1, blocked=blocked,
            )

        assert "• Бонус / Бесплатный доступ — TOPIC_VETO: вето" in sent[0]
