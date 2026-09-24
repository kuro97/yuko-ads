"""
Тесты измерялки готовности автопилота:
- save_feedback / get_feedback_stats (services/autopilot_feedback.py)
- _handle_callback на apfb: (services/telegram_bot.py)
- build_scorecard / format_scorecard (services/scorecard.py)
- Крон-гейт should_send_scorecard_this_week
"""

import sys
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

# Мокаем google.genai до импорта модулей проекта
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
    sys.modules["google.genai.types"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

# Локальное время (UTC+5 по умолчанию)
_TZ_LOCAL = timezone(timedelta(hours=5))

# ============================================================================
# Фикстуры
# ============================================================================

@pytest.fixture()
def tmp_db(tmp_path):
    """Создаёт временную БД decisions.db и инициализирует agent.database."""
    db_path = str(tmp_path / "decisions.db")
    import agent.database as db_mod
    db_mod.init_db(db_path=db_path)
    yield db_path
    # После теста сбрасываем глобальный путь
    db_mod.DB_PATH = None


@pytest.fixture()
def tmp_scorecard_state(tmp_path, monkeypatch):
    """Перенаправляет _SCORECARD_STATE_FILE во временный каталог."""
    import services.scorecard as sc_mod
    state_file = tmp_path / "scorecard_state.json"
    monkeypatch.setattr(sc_mod, "_SCORECARD_STATE_FILE", state_file)
    yield state_file


# ============================================================================
# ЧАСТЬ 1: save_feedback / get_feedback_stats
# ============================================================================

class TestSaveFeedback:
    """save_feedback записывает строку в таблицу autopilot_feedback."""

    def test_save_feedback_creates_row(self, tmp_db):
        """После save_feedback в таблице появляется запись."""
        from services.autopilot_feedback import save_feedback, ensure_table

        save_feedback(ad_id="12345678901", action="p", verdict="up")

        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM autopilot_feedback").fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0]["ad_id"] == "12345678901"
        assert rows[0]["action"] == "p"
        assert rows[0]["verdict"] == "up"

    def test_save_multiple_verdicts(self, tmp_db):
        """Можно сохранять несколько оценок разных решений."""
        from services.autopilot_feedback import save_feedback

        save_feedback(ad_id="11111111111", action="p", verdict="up")
        save_feedback(ad_id="22222222222", action="s", verdict="down")
        save_feedback(ad_id="33333333333", action="p", verdict="down")

        conn = sqlite3.connect(tmp_db)
        count = conn.execute("SELECT COUNT(*) FROM autopilot_feedback").fetchone()[0]
        conn.close()

        assert count == 3


class TestGetFeedbackStats:
    """get_feedback_stats считает % согласия."""

    def test_empty_returns_zero(self, tmp_db):
        """Без оценок — нули, agreement_pct=None."""
        from services.autopilot_feedback import get_feedback_stats

        stats = get_feedback_stats(since_days=7)
        assert stats["up"] == 0
        assert stats["down"] == 0
        assert stats["total"] == 0
        assert stats["agreement_pct"] is None

    def test_all_up_agreement_100(self, tmp_db):
        """Все 👍 → согласие 100%."""
        from services.autopilot_feedback import save_feedback, get_feedback_stats

        save_feedback("11111111111", "p", "up")
        save_feedback("22222222222", "p", "up")

        stats = get_feedback_stats(since_days=7)
        assert stats["up"] == 2
        assert stats["down"] == 0
        assert stats["agreement_pct"] == 100.0

    def test_mixed_agreement_pct(self, tmp_db):
        """3 👍 и 1 👎 → 75%."""
        from services.autopilot_feedback import save_feedback, get_feedback_stats

        for _ in range(3):
            save_feedback("11111111111", "p", "up")
        save_feedback("22222222222", "p", "down")

        stats = get_feedback_stats(since_days=7)
        assert stats["up"] == 3
        assert stats["down"] == 1
        assert stats["total"] == 4
        assert stats["agreement_pct"] == 75.0

    def test_since_days_filters_old(self, tmp_db):
        """Старые записи (за пределами since_days) не учитываются."""
        import agent.database as db_mod

        # Вставляем «старую» запись напрямую (8 дней назад)
        conn = sqlite3.connect(tmp_db)
        conn.execute("CREATE TABLE IF NOT EXISTS autopilot_feedback "
                     "(id INTEGER PRIMARY KEY, ad_id TEXT, action TEXT, verdict TEXT, created_at TEXT)")
        old_date = (datetime.now(_TZ_LOCAL) - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO autopilot_feedback (ad_id, action, verdict, created_at) VALUES (?,?,?,?)",
            ("11111111111", "p", "up", old_date),
        )
        conn.commit()
        conn.close()

        from services.autopilot_feedback import get_feedback_stats
        stats = get_feedback_stats(since_days=7)
        # Старая запись должна быть за пределами окна
        assert stats["total"] == 0


# ============================================================================
# ЧАСТЬ 2: _handle_callback на apfb:
# ============================================================================

OWNER_ID = "111"


def _make_cb(from_id="111", chat_id="111", data="ack", cb_id="cid1"):
    return {
        "id": cb_id,
        "from": {"id": int(from_id)},
        "message": {"chat": {"id": int(chat_id)}},
        "data": data,
    }


class TestHandleCallbackApfb:
    """_handle_callback с apfb: — кнопка протухла, фидбэк по ней не пишется.

    Проект approval-first: `apfb:` попал в _LEGACY_CALLBACK_PREFIXES, и
    `_execute_apfb` оставлен только для вежливого ответа старой кнопке. Причина
    инварианта — доверие к данным: нажатие на кнопку из старого сообщения не
    несёт актуального контекста (объявление могло быть заменено/удалено, а само
    решение теперь живёт в proposal), поэтому запись в autopilot_feedback по
    такому нажатию исказила бы обучающую статистику.

    Записи фидбэка проверяются напрямую в TestSaveFeedback выше — Telegram-путь
    к ним больше не ведёт.
    """

    _STALE = "Кнопка устарела — создай новое предложение"

    def _handle(self, data: str, from_id: str = OWNER_ID, chat_id: str = OWNER_ID):
        """Прогоняет callback и возвращает (mock save_feedback, mock ответа)."""
        with patch("config.TELEGRAM_BOT_TOKEN", "tok"), \
             patch("config.TELEGRAM_CHAT_ID", OWNER_ID), \
             patch("services.autopilot_feedback.save_feedback") as mock_save, \
             patch("services.telegram_bot._answer_callback") as mock_ans:
            from services.telegram_bot import _handle_callback
            _handle_callback(_make_cb(from_id=from_id, chat_id=chat_id, data=data))
        return mock_save, mock_ans

    def test_apfb_up_answers_stale_and_saves_nothing(self, tmp_db):
        """apfb:up:<ad_id>:p → ответ «кнопка устарела», save_feedback НЕ вызван."""
        mock_save, mock_ans = self._handle("apfb:up:12345678901:p")

        mock_save.assert_not_called()
        mock_ans.assert_called_once()
        assert mock_ans.call_args[0][1] == self._STALE

    def test_apfb_down_answers_stale_and_saves_nothing(self, tmp_db):
        """apfb:down:<ad_id>:s — вердикт «down» тоже больше не сохраняется."""
        mock_save, mock_ans = self._handle("apfb:down:96356664125:s")

        mock_save.assert_not_called()
        mock_ans.assert_called_once()
        assert mock_ans.call_args[0][1] == self._STALE

    def test_apfb_foreign_chat_id_ignored(self):
        """apfb от чужого chat_id → полный игнор, даже без ответа кнопке."""
        mock_save, mock_ans = self._handle(
            "apfb:up:12345678901:p", from_id="999", chat_id="999"
        )

        mock_save.assert_not_called()
        mock_ans.assert_not_called()

    def test_apfb_invalid_format_saves_nothing(self):
        """Битый ad_id (4 цифры) → ничего не пишется, payload не попадает в ответ.

        Раньше формат валидировался до записи. Записи больше нет вообще, поэтому
        важно другое: ответ — фиксированный текст, чужие данные из callback_data
        в него не подмешиваются.
        """
        mock_save, mock_ans = self._handle("apfb:up:1234:p")

        mock_save.assert_not_called()
        assert mock_ans.call_args[0][1] == self._STALE
        assert "1234" not in mock_ans.call_args[0][1]

    def test_apfb_bad_verdict_saves_nothing(self):
        """Недопустимый вердикт → ничего не пишется, ответ без эха payload."""
        mock_save, mock_ans = self._handle("apfb:maybe:12345678901:p")

        mock_save.assert_not_called()
        assert mock_ans.call_args[0][1] == self._STALE
        assert "maybe" not in mock_ans.call_args[0][1]


# ============================================================================
# ЧАСТЬ 3: build_scorecard / format_scorecard
# ============================================================================

class TestBuildScorecard:
    """build_scorecard собирает данные из моков."""

    def test_agreement_correct(self, tmp_db, tmp_scorecard_state):
        """build_scorecard правильно считает согласие из autopilot_feedback."""
        from services.autopilot_feedback import save_feedback

        save_feedback("11111111111", "p", "up")
        save_feedback("22222222222", "p", "up")
        save_feedback("33333333333", "p", "down")

        # Мокаем creative_kb чтобы не нужна была реальная KB
        with patch("services.scorecard._get_scale_ad_ids", return_value=[]), \
             patch("services.scorecard._get_decision_counts", return_value={"paused": 2, "scaled": 0}):
            from services.scorecard import build_scorecard
            data = build_scorecard(days=7)

        fb = data["feedback"]
        assert fb["up"] == 2
        assert fb["down"] == 1
        assert fb["total"] == 3
        assert fb["agreement_pct"] == pytest.approx(66.7, abs=0.2)

    def test_scale_accuracy_with_winners(self, tmp_db, tmp_scorecard_state):
        """build_scorecard учитывает текущие qual_pct/romi победителей."""
        # Мокаем получение scaled ad_ids и creative_kb
        scaled_ids = ["11111111111", "22222222222"]

        # Первый — победитель (qual>=15, romi>0), второй — нет
        def mock_kb_conn():
            db = sqlite3.connect(":memory:")
            db.row_factory = sqlite3.Row
            db.execute(
                "CREATE TABLE creative_kb (ad_id TEXT, qual_pct REAL, romi REAL)"
            )
            db.execute("INSERT INTO creative_kb VALUES ('11111111111', 20.0, 1.5)")
            db.execute("INSERT INTO creative_kb VALUES ('22222222222', 10.0, -0.5)")
            db.commit()
            return db

        with patch("services.scorecard._get_scale_ad_ids", return_value=scaled_ids), \
             patch("services.scorecard._get_decision_counts", return_value={"paused": 1, "scaled": 2}), \
             patch("services.autopilot_feedback.get_feedback_stats", return_value={
                 "up": 0, "down": 0, "total": 0, "agreement_pct": None
             }), \
             patch("services.creative_intelligence._get_connection", side_effect=mock_kb_conn):
            from services.scorecard import build_scorecard
            data = build_scorecard(days=7)

        sa = data["scale_accuracy"]
        assert sa["total"] == 2
        assert sa["winners"] == 1  # только первый победитель
        assert sa["accuracy_pct"] == 50.0

    def test_no_scaled_returns_none_accuracy(self, tmp_db, tmp_scorecard_state):
        """Если отскейленных нет — accuracy_pct=None."""
        with patch("services.scorecard._get_scale_ad_ids", return_value=[]), \
             patch("services.scorecard._get_decision_counts", return_value={"paused": 3, "scaled": 0}), \
             patch("services.autopilot_feedback.get_feedback_stats", return_value={
                 "up": 0, "down": 0, "total": 0, "agreement_pct": None
             }):
            from services.scorecard import build_scorecard
            data = build_scorecard(days=7)

        assert data["scale_accuracy"]["accuracy_pct"] is None


class TestFormatScorecard:
    """format_scorecard формирует HTML-текст для Telegram."""

    def test_includes_agreement(self):
        """В тексте есть % согласия."""
        from services.scorecard import format_scorecard
        data = {
            "period_days": 7,
            "feedback": {"up": 3, "down": 1, "total": 4, "agreement_pct": 75.0},
            "decisions": {"paused": 2, "scaled": 1},
            "scale_accuracy": {"total": 1, "winners": 1, "accuracy_pct": 100.0},
        }
        text = format_scorecard(data)
        assert "75.0%" in text
        assert "👍" in text

    def test_low_feedback_warning(self):
        """При мало оценок — предупреждение."""
        from services.scorecard import format_scorecard
        data = {
            "period_days": 7,
            "feedback": {"up": 0, "down": 0, "total": 0, "agreement_pct": None},
            "decisions": {"paused": 0, "scaled": 0},
            "scale_accuracy": {"total": 0, "winners": 0, "accuracy_pct": None},
        }
        text = format_scorecard(data)
        # Должно содержать призыв к оценке
        assert "Мало" in text or "жми кнопки" in text

    def test_no_scaled_shows_no_raises(self):
        """Если поднятий не было — текст без краша."""
        from services.scorecard import format_scorecard
        data = {
            "period_days": 7,
            "feedback": {"up": 5, "down": 0, "total": 5, "agreement_pct": 100.0},
            "decisions": {"paused": 3, "scaled": 0},
            "scale_accuracy": {"total": 0, "winners": 0, "accuracy_pct": None},
        }
        text = format_scorecard(data)
        assert "📊" in text
        assert "нет" in text.lower() or "0" in text


# ============================================================================
# ЧАСТЬ 4: Крон-гейт should_send_scorecard_this_week
# ============================================================================

class TestScorecardCronGate:
    """Гейт крона: воскресенье 19:xx по локальному времени, раз в неделю."""

    def _make_sunday_19(self):
        """Возвращает datetime в воскресенье 19:00 по локальному времени."""
        # Ищем ближайшее воскресенье от 2026-06-28
        dt = datetime(2026, 6, 28, 19, 0, 0, tzinfo=timezone(timedelta(hours=5)))
        assert dt.weekday() == 6  # воскресенье
        return dt

    def test_sunday_19_first_time_returns_true(self, tmp_scorecard_state):
        """Воскресенье 19:xx, неделя не отправлялась → True."""
        from services.scorecard import should_send_scorecard_this_week
        now = self._make_sunday_19()
        assert should_send_scorecard_this_week(now) is True

    def test_sunday_19_second_time_returns_false(self, tmp_scorecard_state):
        """Воскресенье 19:xx, уже отправляли эту неделю → False."""
        from services.scorecard import should_send_scorecard_this_week, mark_scorecard_sent
        now = self._make_sunday_19()
        mark_scorecard_sent(now)
        assert should_send_scorecard_this_week(now) is False

    def test_monday_returns_false(self, tmp_scorecard_state):
        """Понедельник → False (не воскресенье)."""
        from services.scorecard import should_send_scorecard_this_week
        now = datetime(2026, 6, 22, 19, 0, 0, tzinfo=timezone(timedelta(hours=5)))
        assert now.weekday() == 0  # понедельник
        assert should_send_scorecard_this_week(now) is False

    def test_sunday_18_returns_false(self, tmp_scorecard_state):
        """Воскресенье, но час != 19 → False."""
        from services.scorecard import should_send_scorecard_this_week
        now = datetime(2026, 6, 28, 18, 30, 0, tzinfo=timezone(timedelta(hours=5)))
        assert now.weekday() == 6
        assert should_send_scorecard_this_week(now) is False
