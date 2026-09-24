"""CLI-выгрузка датасета обучения: содержимое JSONL, границы периода, режим ro.

Сидирование БД переиспользует готовый харнесс из tests/test_owner_training_export
(та же миграция 022 и та же полная линия proposal → решение → исполнение →
верификация → метрики по горизонтам), чтобы не дублировать десятки FK-вставок.

Скрипт обязан быть строго read-only: проверяется и то, что файл БД не меняется,
и то, что соединение mode=ro отвергает запись.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts.export_approval_dataset import main
from services.database_migrations import apply_runtime_migrations
from services.owner_training_export import _connect_read_only
from tests.test_owner_training_export import BASE, _create_export_fixture


def _iso_day(value: datetime) -> str:
    return value.date().isoformat()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "approvals.sqlite3"
    apply_runtime_migrations(str(db_path))
    _create_export_fixture(db_path)
    return db_path


def _export(
    seeded_db: Path,
    out_path: Path,
    *,
    since: datetime = BASE,
    until: datetime = BASE + timedelta(days=30),
) -> list[dict]:
    exit_code = main(
        [
            "--db",
            str(seeded_db),
            "--since",
            _iso_day(since),
            "--until",
            _iso_day(until),
            "--out",
            str(out_path),
        ]
    )
    assert exit_code == 0
    lines = out_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# Содержимое выгрузки
# ---------------------------------------------------------------------------


def test_export_writes_jsonl_with_full_approval_lineage(
    seeded_db: Path,
    tmp_path: Path,
) -> None:
    documents = _export(seeded_db, tmp_path / "dataset.jsonl")

    assert documents, "выгрузка не должна быть пустой"
    by_id = {document["proposal"]["proposal_id"]: document for document in documents}
    verified = by_id["proposal-verified"]

    # Сам proposal: kind, origin, payload.
    assert verified["proposal"]["proposal_kind"] == "LAUNCH"
    assert verified["proposal"]["origin"] == "CRON"
    assert "plan" in verified["proposal"]

    # Claims: город и adset присутствуют.
    claim = verified["claims"][0]
    assert claim["city"] == "CityA"
    assert claim["language"] == "L1"
    assert claim["adset_id"]
    assert claim["account_id"]

    # Решение владельца: kind + кто + когда.
    decision_kinds = [item["decision_kind"] for item in verified["owner_decisions"]]
    assert "APPROVE" in decision_kinds
    approve = next(
        item for item in verified["owner_decisions"] if item["decision_kind"] == "APPROVE"
    )
    assert isinstance(approve["owner_user_id"], int)
    assert approve["recorded_at"]

    # Исполнение: job + попытки.
    assert verified["execution_jobs"]
    assert claim["attempts"]
    assert claim["attempts"][0]["state"] == "CONFIRMED"

    # Верификация запуска и события.
    assert verified["launch_verification"][0]["state"] == "VERIFIED"
    assert verified["events"]
    assert verified["lifecycle"]


def test_export_contains_outcome_metrics_by_horizon(
    seeded_db: Path,
    tmp_path: Path,
) -> None:
    documents = _export(seeded_db, tmp_path / "dataset.jsonl")
    verified = next(
        document
        for document in documents
        if document["proposal"]["proposal_id"] == "proposal-verified"
    )

    attempt = verified["claims"][0]["attempts"][0]
    horizons = [outcome["horizon"] for outcome in attempt["outcomes"]]

    assert "IMMEDIATE" in horizons
    assert "D1" in horizons
    assert set(horizons) <= {"IMMEDIATE", "D1", "D3", "D7", "D30"}
    assert attempt["outcomes"][0]["metrics"]["source"] == "fixture"


def test_export_covers_reject_and_postpone_decisions(
    seeded_db: Path,
    tmp_path: Path,
) -> None:
    documents = _export(seeded_db, tmp_path / "dataset.jsonl")
    kinds = {
        document["proposal"]["proposal_id"]: {
            item["decision_kind"] for item in document["owner_decisions"]
        }
        for document in documents
    }

    assert kinds["proposal-rejected"] == {"REJECT"}
    assert kinds["proposal-postponed"] == {"POSTPONE"}


# ---------------------------------------------------------------------------
# Границы периода и вывод
# ---------------------------------------------------------------------------


def test_export_respects_since_and_until_bounds(
    seeded_db: Path,
    tmp_path: Path,
) -> None:
    """Интервал полуоткрытый: since включается, until — нет."""
    window = _export(
        seeded_db,
        tmp_path / "window.jsonl",
        since=BASE,
        until=BASE + timedelta(days=3),
    )
    ids = {document["proposal"]["proposal_id"] for document in window}

    # proposal-before создан за секунду ДО BASE — не попадает.
    assert "proposal-before" not in ids
    assert "proposal-rejected" in ids
    assert "proposal-postponed" in ids
    # verified создан на BASE+3 дня — это граница until, значит исключён.
    assert "proposal-verified" not in ids


def test_export_to_stdout_when_out_is_omitted(
    seeded_db: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--db",
            str(seeded_db),
            "--since",
            _iso_day(BASE),
            "--until",
            _iso_day(BASE + timedelta(days=30)),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    documents = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    assert documents
    assert "Экспортировано: proposals=" in captured.err


def test_export_rejects_missing_database(tmp_path: Path) -> None:
    exit_code = main(["--db", str(tmp_path / "нет-такой.db"), "--since", "2026-07-01"])

    assert exit_code == 2


def test_export_rejects_range_over_90_days(seeded_db: Path, tmp_path: Path) -> None:
    exit_code = main(
        [
            "--db",
            str(seeded_db),
            "--since",
            _iso_day(BASE),
            "--until",
            _iso_day(BASE + timedelta(days=200)),
            "--out",
            str(tmp_path / "too-wide.jsonl"),
        ]
    )

    assert exit_code == 1


def test_export_rejects_unparsable_date(seeded_db: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--db", str(seeded_db), "--since", "не-дата"])


# ---------------------------------------------------------------------------
# Read-only гарантии
# ---------------------------------------------------------------------------


def test_export_does_not_modify_database_file(
    seeded_db: Path,
    tmp_path: Path,
) -> None:
    before = _file_digest(seeded_db)

    _export(seeded_db, tmp_path / "dataset.jsonl")

    assert _file_digest(seeded_db) == before


def test_read_only_connection_rejects_writes(seeded_db: Path) -> None:
    """Соединение экспорта открыто в mode=ro — любая запись падает."""
    connection = _connect_read_only(seeded_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "INSERT INTO owner_action_events ("
                "event_id, proposal_id, event_seq, event_type, actor,"
                " payload_json, payload_sha256, created_at"
                ") VALUES ('x', 'proposal-verified', 99, 'HACK', 'test',"
                " '{}', 'a', '2026-07-27T00:00:00+00:00')"
            )
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM owner_action_proposals")
    finally:
        connection.close()


def test_export_script_has_no_mutating_sql() -> None:
    """В самом скрипте не должно быть ни одного пишущего SQL-глагола."""
    source = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "export_approval_dataset.py"
    ).read_text(encoding="utf-8")
    lowered = source.lower()

    for verb in ("insert into", "update ", "delete from", "drop ", "alter "):
        assert verb not in lowered, f"скрипт не должен содержать {verb!r}"
    assert "mode=ro" in lowered or "_connect_read_only" in source
