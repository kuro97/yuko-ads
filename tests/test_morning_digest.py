"""
Тесты для services/morning_digest.py.

Проверяем:
1. build_morning_digest с мокнутыми источниками содержит все секции
2. Сбой каждой секции по отдельности не роняет весь дайджест (заглушка «нет данных»)
3. Текст <=20 непустых строк
4. should_send_digest: гейты 08ч по локальному времени / уже слали сегодня
5. Юнитка: revenue=0 -> «н/д», без деления на 0

Моки — все источники (budget_scaler, plan_reader, exchange_rate, integrations.amo,
decisions_repo, anomaly_alerts) через patch/monkeypatch как контекст-менеджеры.
НЕ ходим в настоящий FB/AMO/Telegram. auto_launch_state.json / brief_gen_state.json
в реальном data/ не существуют -> секция «сделал» читает launches=briefs=0 (read-only,
безопасно, не создаём файлы в проекте).
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_TZ = timezone(timedelta(hours=5))


def _make_lead(status: str) -> dict:
    """Фейковый лид AMO — classify_lead мокается отдельно, содержимое не важно."""
    return {"id": 1, "status": status}


def _mock_decisions_repo_module(decisions: list[dict]):
    """Создаёт MagicMock-модуль agent.repositories.decisions_repo с заданной историей."""
    mock_repo = MagicMock()
    mock_repo.get_decisions_history.return_value = {
        "decisions": decisions, "total": len(decisions), "limit": 500, "offset": 0,
    }
    return mock_repo


def _happy_path_mocks():
    """Контекст-менеджер набора патчей для полностью успешного построения дайджеста.

    Возвращает список patch-объектов (используется через contextlib.ExitStack в тестах).
    """
    return [
        patch("services.morning_digest._get_fb_spend_for_day", return_value=200.0),
        # get_fb_week_spend — отдельный источник для секции «Юнитка» (7-дневное
        # ДРР-окно compute_drr_window), НЕ используется в секции «Вчера»
        # (та берёт _get_fb_spend_for_day напрямую из FB за один день).
        patch("services.budget_scaler.get_fb_week_spend", return_value=1400.0),
        patch("services.budget_scaler.get_google_week_spend", return_value=50.0),
        # Секция «Вчера» теперь берёт Google за конкретный день через
        # get_daily_google_spend (различает «0» и «нет данных»).
        # Возвращаем 50 → расход $250 (FB $200 + Google $50).
        patch("services.google_spend.get_daily_google_spend", return_value=50.0),
        patch("integrations.amo.get_leads_window", return_value=[
            _make_lead("квал"), _make_lead("квал"), _make_lead("оплата"), _make_lead("обычный"),
        ]),
        patch("integrations.amo.classify_lead", side_effect=lambda lead: lead["status"]),
        patch("services.budget_scaler.get_scale_config", return_value={"plan_sheet_id": "sheet123"}),
        patch("services.plan_reader.read_general_plan", return_value={"unit_target": 0.30}),
        patch("services.budget_scaler.compute_drr_window", return_value=(
            datetime(2026, 6, 27, tzinfo=_TZ), datetime(2026, 7, 3, tzinfo=_TZ),
            datetime(2026, 6, 27, tzinfo=_TZ).date(),
        )),
        patch("services.budget_scaler.get_amo_week_revenue", return_value=200_000.0),
        patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0),
        patch("services.anomaly_alerts.detect_cpl_spike_by_city", return_value=[]),
        patch("services.anomaly_alerts.detect_fb_error_burst", return_value=[]),
    ]


@pytest.fixture
def happy_decisions_module():
    """Патчит sys.modules['agent.repositories.decisions_repo'] на мок с несколькими решениями."""
    decisions = [
        {"action": "PAUSED", "confirmed_by": "autopilot"},
        {"action": "PAUSED", "confirmed_by": "autopilot"},
        {"action": "BUDGET_RAISED", "confirmed_by": "budget_pilot"},
        {"action": "DELETED_STALE", "confirmed_by": "cleaner"},
        {"action": "PAUSED", "confirmed_by": "user"},  # не автопилот — не считается
    ]
    mock_repo = _mock_decisions_repo_module(decisions)
    with patch.dict("sys.modules", {"agent.repositories.decisions_repo": mock_repo}):
        yield mock_repo


# ---------------------------------------------------------------------------
# build_morning_digest — happy path, все секции
# ---------------------------------------------------------------------------

class TestBuildMorningDigestHappyPath:
    """Все источники замоканы успешно -> текст содержит все ожидаемые секции."""

    def test_contains_all_sections(self, happy_decisions_module):
        """Текст содержит заголовки всех секций из §2 спеки."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                stack.enter_context(p)
            result = build_morning_digest(now)

        text = result["text"]
        assert "Вчера" in text
        assert "Юнитка" in text
        assert "сделал" in text
        assert "Сегодня по плану" in text

    def test_line_count_within_limit(self, happy_decisions_module):
        """Непустых строк <= 20."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                stack.enter_context(p)
            result = build_morning_digest(now)

        non_empty_lines = [ln for ln in result["text"].split("\n") if ln.strip()]
        assert len(non_empty_lines) <= 20, f"Слишком много строк: {len(non_empty_lines)}"

    def test_never_raises_with_valid_mocks(self, happy_decisions_module):
        """build_morning_digest не бросает исключений при валидных моках."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                stack.enter_context(p)
            result = build_morning_digest(now)

        assert isinstance(result, dict)
        assert isinstance(result["text"], str)
        assert result["text"]


# ---------------------------------------------------------------------------
# Сбой каждой секции по отдельности не роняет дайджест
# ---------------------------------------------------------------------------

class TestBuildMorningDigestSectionFailures:
    """Каждая секция может упасть независимо -> заглушка «нет данных», дайджест жив."""

    def test_all_sections_fail_digest_still_returns(self):
        """Все внешние зависимости бросают исключения -> текст есть, «нет данных» в секциях."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)

        with patch("services.morning_digest._get_fb_spend_for_day", side_effect=RuntimeError("fb down")), \
             patch("services.budget_scaler.get_fb_week_spend", side_effect=RuntimeError("fb down")), \
             patch("services.budget_scaler.get_google_week_spend", side_effect=RuntimeError("google down")), \
             patch("integrations.amo.get_leads_window", side_effect=RuntimeError("amo down")), \
             patch("services.budget_scaler.get_scale_config", side_effect=RuntimeError("cfg down")), \
             patch("services.anomaly_alerts.detect_cpl_spike_by_city", side_effect=RuntimeError("db down")), \
             patch("services.anomaly_alerts.detect_fb_error_burst", side_effect=RuntimeError("counter down")), \
             patch.dict("sys.modules", {
                 "agent.repositories.decisions_repo": MagicMock(
                     get_decisions_history=MagicMock(side_effect=RuntimeError("db down"))
                 )
             }):
            result = build_morning_digest(now)

        text = result["text"]
        assert "нет данных" in text
        assert "Вчера" in text
        assert "Юнитка" in text
        # Дайджест не бросает и всегда возвращает валидный dict
        assert isinstance(result, dict)

    def test_yesterday_leads_failure_isolated_from_spend(self, happy_decisions_module):
        """Падает только AMO-часть секции «Вчера» (лиды/квалы/оплаты) -> расход FB/Google
        и остальные секции остаются рабочими (каждый под-блок секции «Вчера» в своём try)."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                if p.attribute == "get_leads_window":
                    continue
                stack.enter_context(p)
            # Ломаем только AMO-запрос лидов, расход FB/Google остаётся рабочим
            stack.enter_context(
                patch("integrations.amo.get_leads_window", side_effect=RuntimeError("amo down"))
            )
            result = build_morning_digest(now)

        text = result["text"]
        assert "лиды/квалы/оплаты: нет данных" in text
        assert "расход $250" in text  # FB $200 + Google $50 посчитались нормально
        assert "Юнитка" in text  # юнитка не затронута (использует свои вызовы FB/Google/AMO)
        assert "факт" in text  # юнитка посчиталась нормально

    def test_unit_economics_section_failure_isolated(self, happy_decisions_module):
        """Падает только секция «Юнитка» (план) -> остальные секции остаются рабочими."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                if "read_general_plan" in str(p) or "compute_drr_window" in str(p):
                    continue
                stack.enter_context(p)
            stack.enter_context(
                patch("services.plan_reader.read_general_plan", side_effect=RuntimeError("sheets down"))
            )
            stack.enter_context(
                patch("services.budget_scaler.compute_drr_window", side_effect=RuntimeError("boom"))
            )
            result = build_morning_digest(now)

        text = result["text"]
        assert "Юнитка: нет данных" in text
        assert "лиды" in text  # секция «Вчера» не затронута


# ---------------------------------------------------------------------------
# Секция аномалий: только CPL + FB-error, каждый детектор изолирован
# ---------------------------------------------------------------------------

class TestSectionAnomalies:
    """Утренний дайджест не производит spend-сигналы и не дублирует Telegram."""

    def test_calls_only_cpl_and_fb_error_detectors(self):
        """Оба оставшихся health-детектора читаются и попадают в дайджест."""
        from services.morning_digest import _section_anomalies

        now = datetime(2026, 7, 17, 8, 5, tzinfo=_TZ)
        with patch(
            "services.anomaly_alerts.detect_cpl_spike_by_city",
            return_value=["CPL CityA 4.0× медианы"],
        ) as cpl_detector, patch(
            "services.anomaly_alerts.detect_fb_error_burst",
            return_value=["Серия FB-ошибок: 7 за час"],
        ) as fb_detector, patch("services.notifications.send_telegram") as telegram:
            lines = _section_anomalies(now)

        cpl_detector.assert_called_once_with(now)
        fb_detector.assert_called_once_with(now)
        telegram.assert_not_called()
        assert lines == [
            "⚠️ Аномалии:",
            "CPL CityA 4.0× медианы",
            "Серия FB-ошибок: 7 за час",
        ]

    def test_cpl_failure_does_not_hide_fb_error(self):
        """Ошибка CPL не маскирует валидную серию FB-ошибок."""
        from services.morning_digest import _section_anomalies

        now = datetime(2026, 7, 17, 8, 5, tzinfo=_TZ)
        with patch(
            "services.anomaly_alerts.detect_cpl_spike_by_city",
            side_effect=RuntimeError("db down"),
        ), patch(
            "services.anomaly_alerts.detect_fb_error_burst",
            return_value=["Серия FB-ошибок: 7 за час"],
        ):
            lines = _section_anomalies(now)

        assert lines == ["⚠️ Аномалии:", "Серия FB-ошибок: 7 за час"]

    def test_fb_error_failure_does_not_hide_cpl(self):
        """Ошибка счётчика FB не маскирует валидную CPL-аномалию."""
        from services.morning_digest import _section_anomalies

        now = datetime(2026, 7, 17, 8, 5, tzinfo=_TZ)
        with patch(
            "services.anomaly_alerts.detect_cpl_spike_by_city",
            return_value=["CPL CityA 4.0× медианы"],
        ), patch(
            "services.anomaly_alerts.detect_fb_error_burst",
            side_effect=RuntimeError("counter down"),
        ):
            lines = _section_anomalies(now)

        assert lines == ["⚠️ Аномалии:", "CPL CityA 4.0× медианы"]

    def test_empty_detectors_omit_section(self):
        """Если обе health-проверки тихие, секция не занимает место."""
        from services.morning_digest import _section_anomalies

        with patch(
            "services.anomaly_alerts.detect_cpl_spike_by_city", return_value=[]
        ), patch("services.anomaly_alerts.detect_fb_error_burst", return_value=[]):
            assert _section_anomalies(datetime(2026, 7, 17, 8, 5, tzinfo=_TZ)) == []

    def test_source_does_not_consume_removed_spend_detector(self):
        """Статическая регрессия: morning digest не требует удалённый API."""
        source_path = Path(__file__).parent.parent / "services" / "morning_digest.py"
        source = source_path.read_text(encoding="utf-8")

        assert "detect_spend_spike" not in source


# ---------------------------------------------------------------------------
# Регресс: расход «Вчера» — строго за один день, не за многодневный кеш
# (пример бага: $7000 за неделю вместо $1000 за день, см.
# docstring _get_fb_spend_for_day в services/morning_digest.py)
# ---------------------------------------------------------------------------

class TestYesterdaySpendIsSingleDayNotWeeklyCache:
    """Секция «Вчера» берёт расход FB строго за вчера, даже если где-то в
    системе есть многодневный (7-дневный) кеш/снимок с бОльшей суммой."""

    def test_fb_insights_request_scoped_to_single_day(self, happy_decisions_module):
        """_get_fb_spend_for_day запрашивает FB с time_range since==until==вчера
        (не диапазон в несколько дней) — воспроизводит защиту от бага окна."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 3, 8, 5, tzinfo=_TZ)  # «сегодня» 03.07 -> «вчера» 02.07

        # Имитируем прямой FB-ответ: metrics ТОЛЬКО за запрошенный day_iso.
        # Если бы код (по старому багу) читал многодневный analytics_cache.json
        # с суммой за 7 дней, здесь бы вернулось иное число.
        captured_params = {}

        class _FakeResp:
            status_code = 200

            def json(self_inner):
                return {"data": [{"spend": "1000.00"}]}

        def _fake_throttled_get(url, **kwargs):
            captured_params.update(kwargs.get("params", {}))
            return _FakeResp()

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                if p.attribute == "_get_fb_spend_for_day":
                    continue  # не мокаем — проверяем реальную реализацию
                stack.enter_context(p)
            stack.enter_context(
                patch("agent.fb_common._throttled_get", side_effect=_fake_throttled_get)
            )
            stack.enter_context(
                patch("services.fb_token_provider.get_fb_token", return_value="fake-token")
            )
            stack.enter_context(
                patch("services.fb_token_provider.get_fb_account_id", return_value="123")
            )
            result = build_morning_digest(now)

        # time_range запрошен ровно за один день (since == until == вчера)
        import json as _json
        time_range = _json.loads(captured_params["time_range"])
        assert time_range["since"] == "2026-07-02"
        assert time_range["until"] == "2026-07-02"

        text = result["text"]
        assert "расход $1050" in text  # FB $1000 (день) + Google $50 — НЕ ×7

    def test_multiday_metrics_do_not_inflate_yesterday_spend(self, happy_decisions_module):
        """Если источник расхода вернул бы сумму за неделю ($7000), это НЕ
        должно попасть в секцию «Вчера» — она использует day-scoped функцию,
        отдельную от недельной budget_scaler.get_fb_week_spend."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 3, 8, 5, tzinfo=_TZ)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                if p.attribute in ("_get_fb_spend_for_day", "get_fb_week_spend"):
                    continue
                stack.enter_context(p)
            # day-scoped источник «Вчера» — корректные $1000 за один день
            stack.enter_context(
                patch("services.morning_digest._get_fb_spend_for_day", return_value=1000.0)
            )
            # недельный источник («Юнитка») — по ошибке содержит завышенный
            # 7-дневный агрегат (имитация многодневного кеша) — не должен
            # просочиться в секцию «Вчера»
            stack.enter_context(
                patch("services.budget_scaler.get_fb_week_spend", return_value=7000.0)
            )
            result = build_morning_digest(now)

        text = result["text"]
        yesterday_line = next(ln for ln in text.split("\n") if ln.startswith("Вчера:"))
        assert "$1050" in yesterday_line  # FB $1000 + Google $50, НЕ $7000+
        assert "7000" not in yesterday_line


# ---------------------------------------------------------------------------
# Google «0» vs «нет данных» в секции «Вчера»
# (исправление: раньше отсутствие вкладки дня
# показывалось как «Google $0», а не «данных ещё нет»)
# ---------------------------------------------------------------------------

class TestYesterdayGoogleMissingVsZero:
    """Секция «Вчера»: если снимка Google за вчера ещё нет (подрядчик грузит с
    задержкой) — показываем «данных ещё нет» + последний известный день, а НЕ $0."""

    def test_google_missing_shows_no_data_and_last_known(self, happy_decisions_module):
        """Вкладки за вчера нет → «Google: данных ещё нет · последний известный день DD.MM: $X»."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 15, 8, 5, tzinfo=_TZ)  # «вчера» = 14.07

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                # Подменяем Google-источник секции «Вчера» на «нет данных»
                if p.attribute == "get_daily_google_spend":
                    continue
                stack.enter_context(p)
            stack.enter_context(
                patch("services.google_spend.get_daily_google_spend", return_value=None)
            )
            stack.enter_context(
                patch("services.google_spend.get_last_known_google_spend",
                      return_value=("2026-07-11", 320.0))
            )
            result = build_morning_digest(now)

        text = result["text"]
        yesterday_line = next(ln for ln in text.split("\n") if ln.startswith("Вчера:"))
        assert "Google: данных ещё нет" in yesterday_line
        assert "последний известный день 11.07" in yesterday_line
        assert "$320" in yesterday_line
        # FB-часть всё равно показана
        assert "FB $200" in yesterday_line

    def test_google_present_shows_number_as_before(self, happy_decisions_module):
        """Вкладка за вчера есть → расход считается как раньше ($250 = FB $200 + Google $50)."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                stack.enter_context(p)
            result = build_morning_digest(now)

        text = result["text"]
        assert "расход $250 (FB $200 + Google $50)" in text
        assert "данных ещё нет" not in next(ln for ln in text.split("\n") if ln.startswith("Вчера:"))


# ---------------------------------------------------------------------------
# Юнитка: revenue=0 -> «н/д», без деления на 0
# ---------------------------------------------------------------------------

class TestUnitEconomicsZeroRevenue:
    """revenue=0 -> факт «н/д» вместо деления на ноль."""

    def test_zero_revenue_shows_nd_not_division_error(self, happy_decisions_module):
        """get_amo_week_revenue вернул 0 -> текст содержит 'н/д', исключения нет."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                if "get_amo_week_revenue" in str(p):
                    continue
                stack.enter_context(p)
            stack.enter_context(
                patch("services.budget_scaler.get_amo_week_revenue", return_value=0.0)
            )
            result = build_morning_digest(now)

        text = result["text"]
        assert "н/д" in text
        assert "Юнитка" in text


# ---------------------------------------------------------------------------
# should_send_digest: гейты 08ч по локальному времени / уже слали
# ---------------------------------------------------------------------------

class TestShouldSendDigest:
    """should_send_digest: час==8 И today != last_digest_date."""

    def test_08_hour_first_time_true(self):
        """8 утра, ещё не слали (last=None) -> True."""
        from services.morning_digest import should_send_digest

        now = datetime(2026, 7, 2, 8, 3, tzinfo=_TZ)
        assert should_send_digest(now, None) is True

    def test_08_hour_already_sent_today_false(self):
        """8 утра, но уже слали сегодня -> False."""
        from services.morning_digest import should_send_digest

        now = datetime(2026, 7, 2, 8, 3, tzinfo=_TZ)
        last = date(2026, 7, 2)
        assert should_send_digest(now, last) is False

    def test_not_08_hour_false(self):
        """Не 8 час -> False, даже если ещё не слали."""
        from services.morning_digest import should_send_digest

        now = datetime(2026, 7, 2, 10, 0, tzinfo=_TZ)
        assert should_send_digest(now, None) is False

    def test_08_hour_sent_yesterday_true(self):
        """8 утра, последняя отправка была вчера -> True (новый день)."""
        from services.morning_digest import should_send_digest

        now = datetime(2026, 7, 2, 8, 3, tzinfo=_TZ)
        last = date(2026, 7, 1)
        assert should_send_digest(now, last) is True

    def test_naive_datetime_treated_as_local(self):
        """now без tzinfo трактуется как уже локальное (не конвертируется повторно)."""
        from services.morning_digest import should_send_digest

        now = datetime(2026, 7, 2, 8, 3)  # naive
        assert should_send_digest(now, None) is True


# ---------------------------------------------------------------------------
# Секция «Запуски вчера» — читает launch_verify_state за вчера
# ---------------------------------------------------------------------------

class TestSectionLaunchVerify:
    """_section_launch_verify: строка «Запуски вчера: крутятся X/Y» из state
    Контроля запуска (services/launch_verify.py), только если state датирован вчера."""

    def test_state_датирован_вчера_показывает_строку(self):
        from services.morning_digest import _section_launch_verify

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        state = {"date": "2026-07-01", "launched": 5, "running": 4, "problems_count": 1}

        with patch("services.launch_verify.load_verify_state", return_value=state):
            lines = _section_launch_verify(now)

        assert lines == ["Запуски вчера: крутятся 4/5"]

    def test_нет_данных_строка_пропускается(self):
        """load_verify_state вернул {} (файла нет) -> пустой список строк."""
        from services.morning_digest import _section_launch_verify

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)

        with patch("services.launch_verify.load_verify_state", return_value={}):
            lines = _section_launch_verify(now)

        assert lines == []

    def test_state_датирован_не_вчера_строка_пропускается(self):
        """State есть, но это не «вчера» (например, позавчерашний, бот стоял) -> пропуск."""
        from services.morning_digest import _section_launch_verify

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        state = {"date": "2026-06-29", "launched": 3, "running": 3, "problems_count": 0}

        with patch("services.launch_verify.load_verify_state", return_value=state):
            lines = _section_launch_verify(now)

        assert lines == []

    def test_build_digest_включает_строку_запусков_вчера(self, happy_decisions_module):
        """build_morning_digest подключает секцию — строка попадает в итоговый текст."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        verify_state = {"date": "2026-07-01", "launched": 3, "running": 3, "problems_count": 0}

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                stack.enter_context(p)
            stack.enter_context(
                patch("services.launch_verify.load_verify_state", return_value=verify_state)
            )
            result = build_morning_digest(now)

        assert "Запуски вчера: крутятся 3/3" in result["text"]

    def test_секция_падает_дайджест_не_ломается(self, happy_decisions_module):
        """load_verify_state бросает исключение -> дайджест жив, строка просто отсутствует."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                stack.enter_context(p)
            stack.enter_context(
                patch("services.launch_verify.load_verify_state", side_effect=RuntimeError("boom"))
            )
            result = build_morning_digest(now)

        assert isinstance(result, dict)
        assert "Запуски вчера" not in result["text"]


# ---------------------------------------------------------------------------
# Регрессия: генератор ТЗ (services/brief_generator.py) теперь
# пишет last_scheduled_run_date/last_manual_run_date вместо единого
# last_run_date (см. _latest_brief_run_date) — секция «сделал за сутки»
# (_section_actions_24h) должна видеть прогон по ЛЮБОЙ из новых меток, а
# не только по устаревшему legacy-ключу. Тестируем _section_actions_24h
# напрямую (не через build_morning_digest) — читает brief_gen_state.json
# через модульную константу _BRIEF_STATE_PATH (аналог evening_report,
# добавлена этим фиксом специально для тестируемости пути к файлу).
# ---------------------------------------------------------------------------

class TestSectionActionsBriefsCount:
    """_section_actions_24h: счётчик «🃏 N карточки ТЗ» в строке «сделал за сутки»."""

    def test_briefs_counted_via_scheduled_key_only(self, tmp_path):
        """Только last_scheduled_run_date сегодня (плановый крон) — секция видит прогон."""
        import json as _json
        from services.morning_digest import _section_actions_24h

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        brief_file = tmp_path / "brief_gen_state.json"
        brief_file.write_text(_json.dumps({
            "last_scheduled_run_date": "2026-07-02T08:00:05.123456",
            "last_manual_run_date": None,
            "generated_signatures": ["sig1", "sig2"],
        }), encoding="utf-8")

        mock_repo = _mock_decisions_repo_module([])
        with patch.dict("sys.modules", {"agent.repositories.decisions_repo": mock_repo}), \
             patch("services.morning_digest._BRIEF_STATE_PATH", brief_file):
            lines = _section_actions_24h(now)

        assert "🃏 2 карточки ТЗ" in lines[-1]

    def test_briefs_counted_via_manual_key_only(self, tmp_path):
        """Только last_manual_run_date вчера (/brief, HTTP-эндпоинт) — в окне 24ч, считается
        (обратная совместимость: раньше секция читала единый last_run_date, писавшийся
        И плановым, И ручным прогоном — теперь смотрит на обе новые метки)."""
        import json as _json
        from services.morning_digest import _section_actions_24h

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        brief_file = tmp_path / "brief_gen_state.json"
        brief_file.write_text(_json.dumps({
            "last_scheduled_run_date": None,
            "last_manual_run_date": "2026-07-01T20:00:00",
            "generated_signatures": ["sig1", "sig2", "sig3", "sig4"],
        }), encoding="utf-8")

        mock_repo = _mock_decisions_repo_module([])
        with patch.dict("sys.modules", {"agent.repositories.decisions_repo": mock_repo}), \
             patch("services.morning_digest._BRIEF_STATE_PATH", brief_file):
            lines = _section_actions_24h(now)

        # generated_signatures хранит ВСЕ сигнатуры файла (не только этого прогона) —
        # секция берёт последние 3 (см. _section_actions_24h).
        assert "🃏 3 карточки ТЗ" in lines[-1]

    def test_briefs_uses_freshest_of_both_keys(self, tmp_path):
        """Обе метки заданы — берётся более свежая (вчерашний ручной прогон), а не
        недельной давности плановый."""
        import json as _json
        from services.morning_digest import _section_actions_24h

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        brief_file = tmp_path / "brief_gen_state.json"
        brief_file.write_text(_json.dumps({
            "last_scheduled_run_date": "2026-06-25T08:00:00",
            "last_manual_run_date": "2026-07-01T22:00:00",
            "generated_signatures": ["sig1"],
        }), encoding="utf-8")

        mock_repo = _mock_decisions_repo_module([])
        with patch.dict("sys.modules", {"agent.repositories.decisions_repo": mock_repo}), \
             patch("services.morning_digest._BRIEF_STATE_PATH", brief_file):
            lines = _section_actions_24h(now)

        assert "🃏 1 карточки ТЗ" in lines[-1]

    def test_legacy_last_run_date_still_works(self, tmp_path):
        """Обратная совместимость: старый прод-state с единым last_run_date
        (до фикса) по-прежнему считается — fallback в _latest_brief_run_date."""
        import json as _json
        from services.morning_digest import _section_actions_24h

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        brief_file = tmp_path / "brief_gen_state.json"
        brief_file.write_text(_json.dumps({
            "last_run_date": "2026-07-01T09:00:00",
            "generated_signatures": ["sig1", "sig2"],
        }), encoding="utf-8")

        mock_repo = _mock_decisions_repo_module([])
        with patch.dict("sys.modules", {"agent.repositories.decisions_repo": mock_repo}), \
             patch("services.morning_digest._BRIEF_STATE_PATH", brief_file):
            lines = _section_actions_24h(now)

        assert "🃏 2 карточки ТЗ" in lines[-1]

    def test_freshest_of_both_older_than_24h_not_counted(self, tmp_path):
        """Обе метки старше суток — счётчик карточек ТЗ = 0."""
        import json as _json
        from services.morning_digest import _section_actions_24h

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        brief_file = tmp_path / "brief_gen_state.json"
        brief_file.write_text(_json.dumps({
            "last_scheduled_run_date": "2026-06-20T08:00:00",
            "last_manual_run_date": "2026-06-21T08:00:00",
            "generated_signatures": ["sig1"],
        }), encoding="utf-8")

        mock_repo = _mock_decisions_repo_module([])
        with patch.dict("sys.modules", {"agent.repositories.decisions_repo": mock_repo}), \
             patch("services.morning_digest._BRIEF_STATE_PATH", brief_file):
            lines = _section_actions_24h(now)

        assert "🃏 0 карточки ТЗ" in lines[-1]


# ---------------------------------------------------------------------------
# Волна B: причина отказа по каждой карточке. Утренний дайджест показывает,
# какие карточки последний прогон авто-запуска не пропустил (last_run_blocked
# / last_run_at в auto_launch_state.json — пишет services/auto_launch.py
# ::_record_last_run_blocked на каждом завершении прогона).
# ---------------------------------------------------------------------------

_BLOCKED_SAMPLE = [
    {
        "card_id": "card-bonus",
        "card_name": "Бонус / Бесплатная консультация",
        "reason_codes": ["TOPIC_VETO"],
        "reason": "Тема «бонус» — вето по журналу решений",
    },
    {
        "card_id": "card-cap",
        "card_name": "Сидорова / Предложение PRODB",
        "reason_codes": ["CAPACITY_BLOCKED"],
        "reason": "В кабинете нет свободных слотов адсетов",
    },
]


def _write_auto_launch_state(path: Path, *, run_at, blocked) -> Path:
    import json as _json

    path.write_text(_json.dumps({
        "schema_version": 2,
        "launched_today": [],
        "launched_ever": {},
        "launch_attempts": {},
        "last_launch_date": None,
        "last_run_at": run_at,
        "last_run_mode": "dry_run",
        "last_run_blocked": blocked,
    }, ensure_ascii=False), encoding="utf-8")
    return path


class TestSectionAutoLaunchBlocked:
    """_section_auto_launch_blocked: «🚫 Автозапуск не пропустил N карточек (прогон
    ДД.ММ ЧЧ:ММ):» + до 5 строк «• имя — код: причина», остаток «… и ещё K»."""

    def test_список_попадает_в_секцию(self, tmp_path):
        from services.morning_digest import _section_auto_launch_blocked

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        state_file = _write_auto_launch_state(
            tmp_path / "auto_launch_state.json",
            run_at="2026-07-01T10:00:00+05:00", blocked=_BLOCKED_SAMPLE,
        )

        with patch("services.morning_digest._AUTO_LAUNCH_STATE_PATH", state_file):
            lines = _section_auto_launch_blocked(now)

        assert lines == [
            "🚫 Автозапуск не пропустил 2 карточки (прогон 01.07 10:00):",
            "• Бонус / Бесплатная консультация — TOPIC_VETO: Тема «бонус» — вето по журналу решений",
            "• Сидорова / Предложение PRODB — CAPACITY_BLOCKED: В кабинете нет свободных слотов адсетов",
        ]

    def test_пустой_список_секция_опускается(self, tmp_path):
        from services.morning_digest import _section_auto_launch_blocked

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        state_file = _write_auto_launch_state(
            tmp_path / "auto_launch_state.json",
            run_at="2026-07-01T10:00:00+05:00", blocked=[],
        )

        with patch("services.morning_digest._AUTO_LAUNCH_STATE_PATH", state_file):
            assert _section_auto_launch_blocked(now) == []

    def test_нет_файла_секция_опускается(self, tmp_path):
        from services.morning_digest import _section_auto_launch_blocked

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        with patch("services.morning_digest._AUTO_LAUNCH_STATE_PATH", tmp_path / "нет.json"):
            assert _section_auto_launch_blocked(now) == []

    def test_прогон_старше_суток_не_показываем(self, tmp_path):
        """Бот стоял три дня — старая стена отказов не должна висеть каждое утро."""
        from services.morning_digest import _section_auto_launch_blocked

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        state_file = _write_auto_launch_state(
            tmp_path / "auto_launch_state.json",
            run_at="2026-06-28T10:00:00+05:00", blocked=_BLOCKED_SAMPLE,
        )

        with patch("services.morning_digest._AUTO_LAUNCH_STATE_PATH", state_file):
            assert _section_auto_launch_blocked(now) == []

    def test_naive_last_run_at_считается_локальным(self, tmp_path):
        from services.morning_digest import _section_auto_launch_blocked

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        state_file = _write_auto_launch_state(
            tmp_path / "auto_launch_state.json",
            run_at="2026-07-01T10:00:00", blocked=_BLOCKED_SAMPLE[:1],
        )

        with patch("services.morning_digest._AUTO_LAUNCH_STATE_PATH", state_file):
            lines = _section_auto_launch_blocked(now)

        assert lines[0] == "🚫 Автозапуск не пропустил 1 карточку (прогон 01.07 10:00):"

    def test_больше_пяти_сворачиваются(self, tmp_path):
        from services.morning_digest import _section_auto_launch_blocked

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        blocked = [
            {"card_id": f"c{i}", "card_name": f"Карточка {i}",
             "reason_codes": ["TOPIC_VETO"], "reason": "вето"}
            for i in range(7)
        ]
        state_file = _write_auto_launch_state(
            tmp_path / "auto_launch_state.json",
            run_at="2026-07-01T10:00:00+05:00", blocked=blocked,
        )

        with patch("services.morning_digest._AUTO_LAUNCH_STATE_PATH", state_file):
            lines = _section_auto_launch_blocked(now)

        assert lines[0].startswith("🚫 Автозапуск не пропустил 7 карточек")
        assert len(lines) == 1 + 5 + 1
        assert lines[-1] == "… и ещё 2"

    def test_html_экранирование(self, tmp_path):
        from services.morning_digest import _section_auto_launch_blocked

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        blocked = [{"card_id": "c1", "card_name": "Тест <b>x</b>",
                    "reason_codes": ["CODE"], "reason": "a < b & c"}]
        state_file = _write_auto_launch_state(
            tmp_path / "auto_launch_state.json",
            run_at="2026-07-01T10:00:00+05:00", blocked=blocked,
        )

        with patch("services.morning_digest._AUTO_LAUNCH_STATE_PATH", state_file):
            lines = _section_auto_launch_blocked(now)

        assert lines[1] == "• Тест &lt;b&gt;x&lt;/b&gt; — CODE: a &lt; b &amp; c"

    def test_build_digest_включает_список(self, happy_decisions_module, tmp_path):
        """build_morning_digest подключает секцию — имя и код попадают в текст,
        лимит 20 непустых строк не нарушен."""
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        state_file = _write_auto_launch_state(
            tmp_path / "auto_launch_state.json",
            run_at="2026-07-01T10:00:00+05:00", blocked=_BLOCKED_SAMPLE,
        )

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                stack.enter_context(p)
            stack.enter_context(
                patch("services.morning_digest._AUTO_LAUNCH_STATE_PATH", state_file)
            )
            result = build_morning_digest(now)

        text = result["text"]
        assert "Автозапуск не пропустил 2 карточки" in text
        assert "• Бонус / Бесплатная консультация — TOPIC_VETO:" in text
        assert "• Сидорова / Предложение PRODB — CAPACITY_BLOCKED:" in text
        non_empty = [ln for ln in text.split("\n") if ln.strip()]
        assert len(non_empty) <= 20, f"Слишком много строк: {len(non_empty)}"

    def test_секция_падает_дайджест_не_ломается(self, happy_decisions_module):
        from services.morning_digest import build_morning_digest

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _happy_path_mocks():
                stack.enter_context(p)
            stack.enter_context(patch(
                "services.morning_digest._section_auto_launch_blocked",
                side_effect=RuntimeError("boom"),
            ))
            result = build_morning_digest(now)

        assert isinstance(result, dict)
        assert "Автозапуск не пропустил" not in result["text"]


# ---------------------------------------------------------------------------
# Регрессия «карточки ТЗ за 24ч недоступны — can't compare offset-naive and
# offset-aware datetimes»: web/app.py пишет last_scheduled_run_date aware-датой
# (now.isoformat() с tz), brief_generator — naive (datetime.now().isoformat());
# _latest_brief_run_date сравнивал их напрямую через max(), падал, и счётчик
# карточек ТЗ молча становился 0. Теперь обе стороны приводятся к aware UTC.
# ---------------------------------------------------------------------------

class TestBriefsMixedNaiveAwareTimestamps:

    def test_смешанные_метки_считаются_без_warning(self, tmp_path, caplog):
        import json as _json
        import logging as _logging
        from services.morning_digest import _section_actions_24h

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        brief_file = tmp_path / "brief_gen_state.json"
        brief_file.write_text(_json.dumps({
            "last_scheduled_run_date": "2026-07-02T08:00:05+05:00",
            "last_manual_run_date": "2026-07-01T20:00:00",
            "generated_signatures": ["sig1", "sig2"],
        }), encoding="utf-8")

        mock_repo = _mock_decisions_repo_module([])
        caplog.set_level(_logging.WARNING, logger="services.morning_digest")
        with patch.dict("sys.modules", {"agent.repositories.decisions_repo": mock_repo}), \
             patch("services.morning_digest._BRIEF_STATE_PATH", brief_file):
            lines = _section_actions_24h(now)

        assert "карточки ТЗ за 24ч недоступны" not in caplog.text
        assert "🃏 2 карточки ТЗ" in lines[-1]

    def test_aware_метка_недельной_давности_не_побеждает_naive_вчерашнюю(self, tmp_path, caplog):
        """Побеждает реально более свежая метка, а не та, у которой есть tz."""
        import json as _json
        import logging as _logging
        from services.morning_digest import _section_actions_24h

        now = datetime(2026, 7, 2, 8, 5, tzinfo=_TZ)
        brief_file = tmp_path / "brief_gen_state.json"
        brief_file.write_text(_json.dumps({
            "last_scheduled_run_date": "2026-06-20T08:00:05+05:00",
            "last_manual_run_date": "2026-07-01T20:00:00",
            "generated_signatures": ["sig1"],
        }), encoding="utf-8")

        mock_repo = _mock_decisions_repo_module([])
        caplog.set_level(_logging.WARNING, logger="services.morning_digest")
        with patch.dict("sys.modules", {"agent.repositories.decisions_repo": mock_repo}), \
             patch("services.morning_digest._BRIEF_STATE_PATH", brief_file):
            lines = _section_actions_24h(now)

        assert "карточки ТЗ за 24ч недоступны" not in caplog.text
        assert "🃏 1 карточки ТЗ" in lines[-1]
