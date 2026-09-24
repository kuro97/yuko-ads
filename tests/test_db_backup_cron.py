"""
Тесты FIX 6: ежедневный крон-бэкап data/decisions.db (sqlite3 Connection.backup()) с ротацией.

Раньше единственная БД с памятью бота (decisions.db) не бэкапилась вообще.
Затем бэкап делали через VACUUM INTO — но на старом SQLite (<3.27) VACUUM INTO
ещё не поддерживается, крон падал бы каждую ночь.
Теперь бэкап делает sqlite3.Connection.backup() (доступен с Python 3.7,
работает на любой версии SQLite) в data/backups/, хранит последние 7 копий,
ошибки не роняют остальные кроны.

Изоляция от реального data/backups/: патчим web.app.__file__ на путь внутри
tmp_path — backup_dir вычисляется как Path(__file__).parent.parent / "data" / "backups",
поэтому подмена __file__ модуля перенаправляет запись в tmp_path/data/backups.
Мокаем services.autopilot._load_state/_save_state (импортируются локально внутри
_cron_db_backup) и services.notifications.send_telegram. Не ходим в сеть.
"""

import sys
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

_google_mock = MagicMock()
_genai_mock = MagicMock()
sys.modules.setdefault("google", _google_mock)
sys.modules.setdefault("google.genai", _genai_mock)
sys.modules.setdefault("google.genai.types", MagicMock())
_google_mock.genai = _genai_mock

from web.app import _should_run_db_backup, _cron_db_backup, _DB_BACKUP_KEEP  # noqa: E402

_TZ_LOCAL = timezone(timedelta(hours=5))


def _make_fake_web_app_file(tmp_path: Path) -> str:
    """Возвращает путь вида tmp_path/web/app.py — Path(__file__).parent.parent
    внутри _cron_db_backup станет равен tmp_path, backup_dir = tmp_path/data/backups."""
    fake_web_dir = tmp_path / "web"
    fake_web_dir.mkdir(parents=True, exist_ok=True)
    return str(fake_web_dir / "app.py")


def _make_sqlite_db(path: Path) -> None:
    """Создаёт минимальную валидную SQLite БД по указанному пути."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO decisions (name) VALUES ('test')")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Гейт по часам/дате — _should_run_db_backup
# ---------------------------------------------------------------------------

def test_should_run_backup_gate_correct_hour_not_run_today():
    """Час совпадает с _DB_BACKUP_HOUR (4), сегодня ещё не бэкапили → True."""
    now = datetime(2026, 7, 2, 4, 15, tzinfo=_TZ_LOCAL)
    assert _should_run_db_backup(now, last_backup_date=None) is True
    assert _should_run_db_backup(now, last_backup_date="2026-07-01") is True


def test_should_run_backup_gate_wrong_hour():
    """Час НЕ совпадает с _DB_BACKUP_HOUR → False, независимо от даты бэкапа."""
    now = datetime(2026, 7, 2, 10, 0, tzinfo=_TZ_LOCAL)
    assert _should_run_db_backup(now, last_backup_date=None) is False


def test_should_run_backup_gate_already_backed_up_today():
    """Час совпадает, но сегодня уже бэкапили → False (не бэкапим дважды в день)."""
    now = datetime(2026, 7, 2, 4, 45, tzinfo=_TZ_LOCAL)
    assert _should_run_db_backup(now, last_backup_date="2026-07-02") is False


# ---------------------------------------------------------------------------
# Happy path: бэкап создаётся + ротация
# ---------------------------------------------------------------------------

def test_backup_creates_file_and_rotates(tmp_path):
    """8 старых копий + новый бэкап → создаётся новый файл, остаётся ровно 7."""
    fake_file = _make_fake_web_app_file(tmp_path)
    db_path = tmp_path / "decisions.db"
    _make_sqlite_db(db_path)

    backup_dir = tmp_path / "data" / "backups"
    backup_dir.mkdir(parents=True)
    # 8 старых "бэкапов" (пустые файлы — ротация работает по сортировке имени)
    old_dates = [f"202606{d:02d}" for d in range(20, 28)]  # 8 штук, 20..27 июня
    for d in old_dates:
        (backup_dir / f"decisions-{d}.db").write_bytes(b"fake old backup")

    fixed_now = datetime(2026, 7, 2, 4, 10, tzinfo=_TZ_LOCAL)

    with patch("web.app.__file__", fake_file), \
         patch("web.app.datetime") as mock_dt, \
         patch("config.CREATIVE_KB_PATH", str(db_path)), \
         patch("services.autopilot._load_state", return_value={"last_backup_date": None}) as mock_load, \
         patch("services.autopilot._save_state") as mock_save:
        mock_dt.now.return_value = fixed_now
        _cron_db_backup()

    new_backup = backup_dir / "decisions-20260702.db"
    assert new_backup.exists()

    remaining = sorted(backup_dir.glob("decisions-*.db"))
    assert len(remaining) == _DB_BACKUP_KEEP  # ровно 7 — 8 старых + 1 новый, минус 2 самых старых
    # Самые старые (20, 21 июня) должны быть удалены — остались только последние 7
    remaining_names = {p.name for p in remaining}
    assert "decisions-20260620.db" not in remaining_names
    assert "decisions-20260702.db" in remaining_names

    mock_load.assert_called_once()
    assert mock_save.called
    saved_state = mock_save.call_args.args[0]
    assert saved_state["last_backup_date"] == "2026-07-02"


def test_backup_gate_prevents_run_outside_hour(tmp_path):
    """Час не совпадает с _DB_BACKUP_HOUR → бэкап вообще не запускается, файл не создаётся."""
    fake_file = _make_fake_web_app_file(tmp_path)
    db_path = tmp_path / "decisions.db"
    _make_sqlite_db(db_path)

    fixed_now = datetime(2026, 7, 2, 12, 0, tzinfo=_TZ_LOCAL)  # не 4 часа

    with patch("web.app.__file__", fake_file), \
         patch("web.app.datetime") as mock_dt, \
         patch("config.CREATIVE_KB_PATH", str(db_path)), \
         patch("services.autopilot._load_state", return_value={"last_backup_date": None}), \
         patch("services.autopilot._save_state") as mock_save:
        mock_dt.now.return_value = fixed_now
        _cron_db_backup()

    backup_dir = tmp_path / "data" / "backups"
    assert not backup_dir.exists() or not list(backup_dir.glob("decisions-*.db"))
    mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# Ошибка не роняет крон
# ---------------------------------------------------------------------------

def test_backup_error_does_not_raise_when_db_missing(tmp_path):
    """data/decisions.db отсутствует → logger.error + Telegram, БЕЗ исключения наружу."""
    fake_file = _make_fake_web_app_file(tmp_path)
    missing_db_path = tmp_path / "does_not_exist.db"

    fixed_now = datetime(2026, 7, 2, 4, 0, tzinfo=_TZ_LOCAL)

    with patch("web.app.__file__", fake_file), \
         patch("web.app.datetime") as mock_dt, \
         patch("config.CREATIVE_KB_PATH", str(missing_db_path)), \
         patch("services.autopilot._load_state", return_value={"last_backup_date": None}), \
         patch("services.autopilot._save_state") as mock_save, \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("web.app.logger") as mock_logger:
        mock_dt.now.return_value = fixed_now
        # Не должно бросать исключение наружу
        _cron_db_backup()

    assert mock_logger.error.called
    assert mock_tg.called
    mock_save.assert_not_called()  # state не сохраняем при провале


def test_backup_error_connect_does_not_raise(tmp_path):
    """sqlite3.connect бросил исключение (например БД повреждена/диск полон) →
    logger.error + Telegram, крон не падает наружу (edge case)."""
    fake_file = _make_fake_web_app_file(tmp_path)
    db_path = tmp_path / "decisions.db"
    _make_sqlite_db(db_path)

    fixed_now = datetime(2026, 7, 2, 4, 0, tzinfo=_TZ_LOCAL)

    with patch("web.app.__file__", fake_file), \
         patch("web.app.datetime") as mock_dt, \
         patch("config.CREATIVE_KB_PATH", str(db_path)), \
         patch("services.autopilot._load_state", return_value={"last_backup_date": None}), \
         patch("services.autopilot._save_state") as mock_save, \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("web.app.logger") as mock_logger, \
         patch("web.app.sqlite3.connect", side_effect=sqlite3.OperationalError("disk full")):
        mock_dt.now.return_value = fixed_now
        _cron_db_backup()

    assert mock_logger.error.called
    assert mock_tg.called
    mock_save.assert_not_called()


def test_backup_telegram_failure_is_swallowed(tmp_path):
    """Даже если сам send_telegram упал при отправке алерта об ошибке — крон не падает
    (двойная защита: try вокруг отправки алерта, edge case)."""
    fake_file = _make_fake_web_app_file(tmp_path)
    missing_db_path = tmp_path / "does_not_exist.db"

    fixed_now = datetime(2026, 7, 2, 4, 0, tzinfo=_TZ_LOCAL)

    with patch("web.app.__file__", fake_file), \
         patch("web.app.datetime") as mock_dt, \
         patch("config.CREATIVE_KB_PATH", str(missing_db_path)), \
         patch("services.autopilot._load_state", return_value={"last_backup_date": None}), \
         patch("services.autopilot._save_state"), \
         patch("services.notifications.send_telegram", side_effect=RuntimeError("TG down")):
        mock_dt.now.return_value = fixed_now
        # Не должно бросать исключение, даже если Telegram тоже недоступен
        _cron_db_backup()


def test_backup_existing_target_file_overwritten(tmp_path):
    """Целевой файл decisions-YYYYMMDD.db уже существует → удаляется перед VACUUM INTO
    (иначе SQLite бросит 'database ... already exists')."""
    fake_file = _make_fake_web_app_file(tmp_path)
    db_path = tmp_path / "decisions.db"
    _make_sqlite_db(db_path)

    backup_dir = tmp_path / "data" / "backups"
    backup_dir.mkdir(parents=True)
    existing_target = backup_dir / "decisions-20260702.db"
    existing_target.write_bytes(b"stale leftover file")

    fixed_now = datetime(2026, 7, 2, 4, 0, tzinfo=_TZ_LOCAL)

    with patch("web.app.__file__", fake_file), \
         patch("web.app.datetime") as mock_dt, \
         patch("config.CREATIVE_KB_PATH", str(db_path)), \
         patch("services.autopilot._load_state", return_value={"last_backup_date": None}), \
         patch("services.autopilot._save_state"):
        mock_dt.now.return_value = fixed_now
        _cron_db_backup()

    # Файл перезаписан валидным бэкапом через Connection.backup() (не тот же "stale" контент)
    assert existing_target.read_bytes() != b"stale leftover file"
    assert existing_target.stat().st_size > 0


def test_backup_content_is_valid_copy_of_source_db(tmp_path):
    """Connection.backup() создаёт РАБОЧУЮ копию БД — данные читаются из бэкапа."""
    fake_file = _make_fake_web_app_file(tmp_path)
    db_path = tmp_path / "decisions.db"
    _make_sqlite_db(db_path)

    fixed_now = datetime(2026, 7, 2, 4, 0, tzinfo=_TZ_LOCAL)

    with patch("web.app.__file__", fake_file), \
         patch("web.app.datetime") as mock_dt, \
         patch("config.CREATIVE_KB_PATH", str(db_path)), \
         patch("services.autopilot._load_state", return_value={"last_backup_date": None}), \
         patch("services.autopilot._save_state"):
        mock_dt.now.return_value = fixed_now
        _cron_db_backup()

    backup_path = tmp_path / "data" / "backups" / "decisions-20260702.db"
    conn = sqlite3.connect(str(backup_path))
    rows = conn.execute("SELECT name FROM decisions").fetchall()
    conn.close()
    assert rows == [("test",)]


def test_backup_rotation_does_not_touch_manual_backups(tmp_path):
    """Ротация крона НЕ должна удалять ручные бэкапы вида
    decisions-pre-mig010-*.db, лежащие в той же папке (узкий шаблон имени)."""
    fake_file = _make_fake_web_app_file(tmp_path)
    db_path = tmp_path / "decisions.db"
    _make_sqlite_db(db_path)

    backup_dir = tmp_path / "data" / "backups"
    backup_dir.mkdir(parents=True)
    # Ручной бэкап миграции — не должен участвовать в ротации крона.
    manual_backup = backup_dir / "decisions-pre-mig010-20260615.db"
    manual_backup.write_bytes(b"manual migration backup, keep forever")

    # 7 крон-бэкапов, уже на пределе лимита — новый 8-й должен вытеснить самый старый.
    old_dates = [f"202606{d:02d}" for d in range(21, 28)]  # 7 штук, 21..27 июня
    for d in old_dates:
        (backup_dir / f"decisions-{d}.db").write_bytes(b"fake old backup")

    fixed_now = datetime(2026, 7, 2, 4, 10, tzinfo=_TZ_LOCAL)

    with patch("web.app.__file__", fake_file), \
         patch("web.app.datetime") as mock_dt, \
         patch("config.CREATIVE_KB_PATH", str(db_path)), \
         patch("services.autopilot._load_state", return_value={"last_backup_date": None}), \
         patch("services.autopilot._save_state"):
        mock_dt.now.return_value = fixed_now
        _cron_db_backup()

    # Ручной бэкап цел и невредим.
    assert manual_backup.exists()
    assert manual_backup.read_bytes() == b"manual migration backup, keep forever"

    # Крон-бэкапов ровно 7 (самый старый 21 июня вытеснен новым от 2 июля).
    cron_backups = sorted(
        p.name for p in backup_dir.glob("decisions-*.db")
        if p.name != manual_backup.name
    )
    assert len(cron_backups) == _DB_BACKUP_KEEP
    assert "decisions-20260621.db" not in cron_backups
    assert "decisions-20260702.db" in cron_backups
