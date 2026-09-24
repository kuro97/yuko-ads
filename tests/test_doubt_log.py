"""
Тесты для services/doubt_log.py — журнал сомнений Budget Scaler.

Проверяем:
- append_doubt_entry: запись сохраняется на диск с нужными полями
- ротация: записи старше _RETENTION_DAYS отбрасываются
- get_doubt_entries_for_date: фильтр по конкретной дате, несколько записей за день
- пустой/битый файл не роняет чтение
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import doubt_log

_TZ_LOCAL = timezone(timedelta(hours=5))


@pytest.fixture
def clean_log(tmp_path, monkeypatch):
    """Чистый файл журнала сомнений для каждого теста."""
    log_file = tmp_path / "doubt_log.json"
    monkeypatch.setattr(doubt_log, "_DOUBT_LOG_FILE", log_file)
    return log_file


class TestAppendDoubtEntry:
    def test_запись_сохраняется_с_нужными_полями(self, clean_log):
        now = datetime(2026, 7, 7, 13, 0, 0, tzinfo=_TZ_LOCAL)
        doubt_log.append_doubt_entry(
            ["ДРР почти у цели", "CDP лёг — считаю по листу"],
            "поднимаю бюджет (гейты прошли)",
            now=now,
        )

        raw = json.loads(clean_log.read_text(encoding="utf-8"))
        assert len(raw) == 1
        entry = raw[0]
        assert entry["date"] == "2026-07-07"
        assert entry["triggers"] == ["ДРР почти у цели", "CDP лёг — считаю по листу"]
        assert entry["decision"] == "поднимаю бюджет (гейты прошли)"

    def test_несколько_записей_за_один_день_накапливаются(self, clean_log):
        now = datetime(2026, 7, 7, 9, 0, 0, tzinfo=_TZ_LOCAL)
        doubt_log.append_doubt_entry(["триггер 1"], "решение 1", now=now)
        doubt_log.append_doubt_entry(["триггер 2"], "решение 2", now=now.replace(hour=15))

        entries = doubt_log.get_doubt_entries_for_date("2026-07-07")
        assert len(entries) == 2
        assert entries[0]["decision"] == "решение 1"
        assert entries[1]["decision"] == "решение 2"

    def test_ротация_удаляет_записи_старше_30_дней(self, clean_log):
        now = datetime(2026, 7, 7, 13, 0, 0, tzinfo=_TZ_LOCAL)
        old_date = (now - timedelta(days=45)).date().isoformat()
        recent_date = (now - timedelta(days=10)).date().isoformat()

        # Кладём напрямую старую и недавнюю запись, затем добавляем ещё одну —
        # append_doubt_entry должен отротировать файл при следующей записи.
        clean_log.write_text(json.dumps([
            {"date": old_date, "triggers": ["старое"], "decision": "старое решение"},
            {"date": recent_date, "triggers": ["недавнее"], "decision": "недавнее решение"},
        ]), encoding="utf-8")

        doubt_log.append_doubt_entry(["новое"], "новое решение", now=now)

        raw = json.loads(clean_log.read_text(encoding="utf-8"))
        dates = [e["date"] for e in raw]
        assert old_date not in dates
        assert recent_date in dates
        assert now.date().isoformat() in dates


class TestGetDoubtEntriesForDate:
    def test_пустой_день_возвращает_пустой_список(self, clean_log):
        assert doubt_log.get_doubt_entries_for_date("2026-07-07") == []

    def test_фильтрует_только_нужную_дату(self, clean_log):
        clean_log.write_text(json.dumps([
            {"date": "2026-07-06", "triggers": ["вчера"], "decision": "вчерашнее"},
            {"date": "2026-07-07", "triggers": ["сегодня"], "decision": "сегодняшнее"},
        ]), encoding="utf-8")

        entries = doubt_log.get_doubt_entries_for_date("2026-07-07")
        assert len(entries) == 1
        assert entries[0]["decision"] == "сегодняшнее"

    def test_битый_файл_не_роняет_чтение(self, clean_log):
        clean_log.write_text("{не json", encoding="utf-8")
        assert doubt_log.get_doubt_entries_for_date("2026-07-07") == []

    def test_отсутствующий_файл_не_роняет_чтение(self, tmp_path, monkeypatch):
        missing_file = tmp_path / "no_such_file.json"
        monkeypatch.setattr(doubt_log, "_DOUBT_LOG_FILE", missing_file)
        assert doubt_log.get_doubt_entries_for_date("2026-07-07") == []
