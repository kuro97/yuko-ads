"""
Тесты для services/evening_report.py — формат v2 (по фидбеку владельца).

Формат v2: честные окна («Сегодня» — день
кабинета FB с 12:00 по локальному времени, «Вчера» — полный закрытый день), вердикт-строка
с дельтами сразу после заголовка, паузы/удержания ПОЛНЫМИ блоками (не топ-3).

Мокаем внешние зависимости: decisions_repo, _get_fb_spend_for_day,
get_google_week_spend, get_leads_window/classify_lead, _section_unit_economics,
load_hold_state, state-файлы запусков/ТЗ.
НЕ импортируем web.app (google.genai не нужен).
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

# Локальный часовой пояс
_TZ_LOCAL = timezone(timedelta(hours=5))

# Время «сейчас» для тестов — заведомо ПОСЛЕ 12:00, чтобы окно «сегодня»
# (день кабинета) было открыто и не съезжало на вчера (см. _today_window_bounds).
_NOW = datetime(2026, 6, 11, 21, 0, 0, tzinfo=_TZ_LOCAL)


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_decision(
    action: str, ad_name: str, ad_id: str, confirmed_by: str,
    reason: str = "тест", spend: float | None = 100.0,
) -> dict:
    """Создаёт запись решения в формате get_decisions_history."""
    return {
        "id": 1,
        "ad_id": ad_id,
        "ad_name": ad_name,
        "action": action,
        "reason": reason,
        "confirmed_by": confirmed_by,
        "timestamp": "2026-06-11 10:00:00",
        "spend": spend,
        "leads": 5,
        "cpl": 20.0,
        "ctr": None,
        "cpm": None,
        "romi": None,
        "qual_pct": 15.0,
    }


def _make_history_result(decisions: list[dict]) -> dict:
    """Оборачивает список решений в формат get_decisions_history."""
    return {"decisions": decisions, "total": len(decisions), "limit": 500, "offset": 0}


def _make_lead(status: str = "квал") -> dict:
    """Минимальный лид для classify_lead (мокается напрямую, содержимое не важно)."""
    return {"id": 1, "status_id": 1}


def _reload_module():
    """Перезагружает services.evening_report, чтобы патчи sys.modules сработали."""
    import importlib
    import services.evening_report as er_module
    importlib.reload(er_module)
    return er_module


def _fb_spend_by_day(today_value: float, yesterday_value: float, now: datetime = _NOW):
    """side_effect для _get_fb_spend_for_day: разные суммы для сегодня/вчера по ISO-дате."""
    today_str = now.date().isoformat()
    yesterday_str = (now - timedelta(days=1)).date().isoformat()

    def _impl(day_iso: str) -> float:
        if day_iso == today_str:
            return today_value
        if day_iso == yesterday_str:
            return yesterday_value
        raise AssertionError(f"неожиданный day_iso={day_iso}")

    return _impl


def _leads_by_window(today_leads: list[dict], yesterday_leads: list[dict], now: datetime = _NOW):
    """side_effect для get_leads_window: различает окно «сегодня» (с 12:00) от «вчера» (сутки)."""
    from services.evening_report import _today_window_bounds, _yesterday_window_bounds

    today_from, today_to = _today_window_bounds(now)
    y_from, y_to, _ = _yesterday_window_bounds(now)

    def _impl(from_ts: int, to_ts: int) -> list[dict]:
        if from_ts == today_from:
            return today_leads
        if from_ts == y_from:
            return yesterday_leads
        raise AssertionError(f"неожиданное окно from_ts={from_ts} to_ts={to_ts}")

    return _impl


def _classify_all(status: str):
    """side_effect для classify_lead, возвращающий фиксированный статус для любого лида."""
    def _impl(lead):
        return status
    return _impl


def _default_patches(now: datetime = _NOW):
    """Базовый набор патчей для «счастливого пути»: расход/лиды/юнитка есть, но 0."""
    return [
        patch("services.morning_digest._get_fb_spend_for_day", side_effect=_fb_spend_by_day(0.0, 0.0, now)),
        patch("services.budget_scaler.get_google_week_spend", return_value=0.0),
        # Google за день теперь через get_daily_google_spend (различает «0» и «нет
        # данных»). 0.0 = настоящий ноль,
        # секция «Деньги» рендерит «(FB $A + Google $0)» как раньше.
        patch("services.google_spend.get_daily_google_spend", return_value=0.0),
        patch("integrations.amo.get_leads_window", side_effect=_leads_by_window([], [], now)),
        patch("integrations.amo.classify_lead", side_effect=_classify_all("новый")),
        patch("services.morning_digest._section_unit_economics", return_value=["Юнитка: факт 5% / план 8%"]),
        patch("services.autopilot_hold.load_hold_state", return_value={"holds": {}}),
        # По умолчанию журнал сомнений пуст — изолируем тесты от реального
        # data/doubt_log.json на диске (не должен «протекать» в тесты).
        patch("services.doubt_log.get_doubt_entries_for_date", return_value=[]),
    ]


def _patch_target(p) -> tuple:
    """Уникальный идентификатор цели patch-объекта (модуль/объект + имя атрибута).

    Нужен, чтобы находить дубли (два patch() на один и тот же атрибут ломают
    restore: unittest.mock.patch хранит "оригинал" на момент start(), и если
    два патча одного атрибута останавливать не в обратном порядке — второй
    stop() затирает уже восстановленное значение мок-объектом первого патча).
    """
    return (p.getter(), p.attribute)


def _build_with_decisions(decisions: list[dict], now: datetime | None = None, extra_patches: list | None = None):
    """Строит отчёт с заданными decisions и дефолтными безопасными моками остального.

    ВАЖНО: сначала перезагружаем модуль (patch.dict decisions_repo требует reload,
    чтобы сработать), и только ПОТОМ стартуем патчи на атрибуты evening_report
    (_LAUNCH_STATE_PATH/_BRIEF_STATE_PATH) — иначе importlib.reload пересоздаёт
    атрибуты модуля заново и затирает патч.

    extra_patches может переопределять цели из _default_patches() (например,
    другое return_value для того же атрибута) — в этом случае дефолтный патч
    на тот же target исключается, чтобы не патчить один атрибут дважды.
    Патчи останавливаются в обратном (LIFO) порядке относительно старта —
    единственный порядок, при котором unittest.mock.patch корректно
    восстанавливает исходные значения даже при частичном пересечении целей.
    """
    build_now = now or _NOW
    mock_repo = MagicMock()
    mock_repo.get_decisions_history.return_value = _make_history_result(decisions)

    extra = extra_patches or []
    extra_targets = {_patch_target(p) for p in extra}
    patches = [p for p in _default_patches(build_now) if _patch_target(p) not in extra_targets] + extra
    with patch.dict("sys.modules", {"agent.repositories.decisions_repo": mock_repo}):
        er_module = _reload_module()
        started: list = []
        try:
            for p in patches:
                p.start()
                started.append(p)
            return er_module.build_evening_report(build_now)
        finally:
            for p in reversed(started):
                p.stop()


# ---------------------------------------------------------------------------
# Тест: build_evening_report — структура нового блочного формата v2
# ---------------------------------------------------------------------------

class TestBuildEveningReportStructure:
    """Формат v2: вердикт первой строкой, секции с пустыми строками, жирные
    заголовки, нулевые пункты скрыты."""

    def test_header_has_date_and_emoji(self):
        """Заголовок содержит «Итог дня DD.MM» с эмодзи 🌇."""
        result = _build_with_decisions([])
        text = result["text"]
        assert "🌇 <b>Итог дня" in text

    def test_verdict_line_present_after_header(self):
        """Вторая непустая строка (сразу после заголовка+пустой строки) — вердикт дня
        с числами (формат v2, пункт 2 фидбека владельца)."""
        result = _build_with_decisions([])
        lines = result["text"].split("\n")
        assert lines[0].startswith("🌇 <b>Итог дня")
        assert lines[1] == ""
        verdict_line = lines[2]
        assert "Бот" in verdict_line
        assert verdict_line.strip() != ""

    def test_sections_present(self):
        """Присутствуют все обязательные секции: Деньги, Что сделал бот, Завтра."""
        result = _build_with_decisions([])
        text = result["text"]
        assert "<b>Деньги</b>" in text
        assert "<b>Что сделал бот</b>" in text
        assert "<b>Завтра</b>" in text

    def test_zero_actions_shows_no_actions_message(self):
        """Если решений и остальных действий не было — «Сегодня действий не было»."""
        result = _build_with_decisions([])
        assert "Сегодня действий не было." in result["text"]

    def test_tomorrow_section_has_guardian_slots_and_crons(self):
        """Секция «Завтра» содержит слоты Стража, автозапуск 10:00, масштабирование 13:00."""
        result = _build_with_decisions([])
        text = result["text"]
        assert "Стража" in text
        assert "10:00" in text
        assert "13:00" in text

    def test_footer_notes_fb_spend_still_loading(self):
        """Внизу отчёта — пометка про дозагрузку расхода FB (пункт 5 фидбека владельца)."""
        result = _build_with_decisions([])
        text = result["text"]
        assert "дозагружается" in text
        assert "утренн" in text  # "утреннем дайджесте"


# ---------------------------------------------------------------------------
# Тест: секция «Деньги» v2 — честные окна, никакого смешанного CPL
# ---------------------------------------------------------------------------

class TestMoneySectionV2:
    """Формат v2: две отдельные строки
    «Сегодня» (день кабинета с 12:00) и «Вчера» (полный день), свой CPL
    внутри каждого окна, никакого CPL из смешанных окон."""

    def test_today_and_yesterday_lines_present(self):
        """Секция «Деньги» содержит явные подписи «Сегодня» и «Вчера»."""
        result = _build_with_decisions([])
        text = result["text"]
        assert "Сегодня (день кабинета с 12:00)" in text
        assert "полный день" in text

    def test_fb_spend_from_single_day_function_not_week_cache(self):
        """build_evening_report вызывает _get_fb_spend_for_day (2 раза: сегодня+вчера),
        а НЕ budget_scaler.get_fb_week_spend."""
        decisions = []
        mock_repo = MagicMock()
        mock_repo.get_decisions_history.return_value = _make_history_result(decisions)

        fb_day_mock = MagicMock(side_effect=_fb_spend_by_day(333.0, 111.0))
        google_mock = MagicMock(return_value=0.0)
        week_spend_mock = MagicMock(return_value=999999.0)  # если бы вызвался — было бы видно в тексте

        patches = [
            patch("services.morning_digest._get_fb_spend_for_day", fb_day_mock),
            patch("services.budget_scaler.get_google_week_spend", google_mock),
            patch("services.google_spend.get_daily_google_spend", return_value=0.0),
            patch("services.budget_scaler.get_fb_week_spend", week_spend_mock),
            patch("integrations.amo.get_leads_window", side_effect=_leads_by_window([], [])),
            patch("integrations.amo.classify_lead", side_effect=_classify_all("новый")),
            patch("services.morning_digest._section_unit_economics", return_value=["Юнитка: н/д"]),
            patch("services.autopilot_hold.load_hold_state", return_value={"holds": {}}),
            patch("services.doubt_log.get_doubt_entries_for_date", return_value=[]),
        ]
        with patch.dict("sys.modules", {"agent.repositories.decisions_repo": mock_repo}):
            started: list = []
            try:
                for p in patches:
                    p.start()
                    started.append(p)
                er_module = _reload_module()
                result = er_module.build_evening_report(_NOW)
            finally:
                for p in reversed(started):
                    p.stop()

        text = result["text"]
        assert fb_day_mock.call_count == 2
        # Недельная функция НЕ должна вызываться из evening_report вообще
        week_spend_mock.assert_not_called()
        assert "333" in text
        assert "111" in text
        assert "999999" not in text

    def test_today_leads_use_noon_window_not_midnight(self):
        """Лиды «сегодня» считаются с 12:00 (день кабинета), НЕ с полуночи —
        регрессия старого бага смешивания окон (владелец: фейковый CPL $3.2)."""
        from services.evening_report import _today_window_bounds

        today_from, today_to = _today_window_bounds(_NOW)
        today_noon = _NOW.replace(hour=12, minute=0, second=0, microsecond=0)
        assert today_from == int(today_noon.timestamp())
        assert today_to == int(_NOW.timestamp())

    def test_today_leads_before_noon_use_previous_window(self):
        """Если now раньше 12:00 — окно «сегодня» берёт от 12:00 ВЧЕРА (окно ещё
        не открылось по времени кабинета)."""
        from services.evening_report import _today_window_bounds

        early_now = datetime(2026, 6, 11, 8, 0, 0, tzinfo=_TZ_LOCAL)
        today_from, today_to = _today_window_bounds(early_now)
        expected_start = datetime(2026, 6, 10, 12, 0, 0, tzinfo=_TZ_LOCAL)
        assert today_from == int(expected_start.timestamp())

    def test_no_cross_window_cpl(self):
        """CPL «сегодня» считается ТОЛЬКО из расхода+лидов окна «сегодня», CPL «вчера» —
        ТОЛЬКО из расхода+лидов окна «вчера». Раньше расход дня кабинета (частичный)
        делился на лиды за локальные сутки (другое окно) → заниженный фейковый CPL."""
        today_leads = [_make_lead() for _ in range(2)]  # мало лидов в открытом окне
        yesterday_leads = [_make_lead() for _ in range(10)]  # полные сутки — много лидов

        result = _build_with_decisions([], extra_patches=[
            patch("services.morning_digest._get_fb_spend_for_day", side_effect=_fb_spend_by_day(100.0, 500.0)),
            patch("integrations.amo.get_leads_window", side_effect=_leads_by_window(today_leads, yesterday_leads)),
            patch("integrations.amo.classify_lead", side_effect=_classify_all("новый")),
        ])
        text = result["text"]
        # Сегодня: $100 / 2 лида = $50 CPL
        assert "$50" in text
        # Вчера: $500 / 10 лидов = $50 CPL тоже, но проверяем что цифры лидов раздельные
        assert "лиды с 12:00 по AMO — 2" in text
        assert "лиды 10" in text

    def test_unit_economics_fact_vs_plan_shown(self):
        """Юнитка факт/план отображается в секции «Деньги» (переиспользуем morning_digest)."""
        result = _build_with_decisions([], extra_patches=[
            patch("services.morning_digest._section_unit_economics",
                  return_value=["Юнитка: факт 7% / план 8% (ДРР за окно до сб 27.06)"]),
        ])
        text = result["text"]
        assert "факт 7% / план 8%" in text

    def test_google_missing_shows_no_data_not_zero(self):
        """Снимка Google за день ещё нет → «Google: данных ещё нет · последний
        известный день DD.MM: $X», а НЕ «Google $0»."""
        result = _build_with_decisions([], extra_patches=[
            patch("services.morning_digest._get_fb_spend_for_day", side_effect=_fb_spend_by_day(333.0, 111.0)),
            patch("services.google_spend.get_daily_google_spend", return_value=None),
            patch("services.google_spend.get_last_known_google_spend", return_value=("2026-06-09", 480.0)),
        ])
        text = result["text"]
        assert "Google: данных ещё нет" in text
        assert "последний известный день 09.06" in text
        assert "$480" in text
        # FB-часть по-прежнему видна (не «нет данных» целиком)
        assert "FB $333" in text or "FB $111" in text


# ---------------------------------------------------------------------------
# Тест: вердикт дня — дельты квал%/цена квала сегодня vs вчера
# ---------------------------------------------------------------------------

class TestVerdictSection:
    """Вердикт дня — первая строка после заголовка, человеческим языком с числами
    (пункт 2 фидбека владельца)."""

    def test_verdict_mentions_pause_count(self):
        """Вердикт упоминает число пауз («Бот выключил N...»)."""
        decisions = [
            _make_decision("PAUSED", "CityA | Тест 1", "ad_1", "autopilot"),
            _make_decision("PAUSED", "CityB | Тест 2", "ad_2", "autopilot"),
        ]
        result = _build_with_decisions(decisions)
        text = result["text"]
        assert "Бот выключил 2" in text

    def test_verdict_zero_pauses_says_nothing_paused(self):
        """Без пауз — вердикт честно говорит «ничего не выключил», не «0 паузы»."""
        result = _build_with_decisions([])
        text = result["text"]
        assert "Бот сегодня ничего не выключил" in text

    def test_verdict_shows_qual_delta_in_percentage_points(self):
        """Вердикт показывает дельту квал% сегодня к вчера в процентных пунктах."""
        today_leads = [_make_lead() for _ in range(10)]
        yesterday_leads = [_make_lead() for _ in range(10)]

        def _classify_today_60pct(lead):
            return "квал"

        result = _build_with_decisions([], extra_patches=[
            patch("integrations.amo.get_leads_window", side_effect=_leads_by_window(today_leads, yesterday_leads)),
            patch("integrations.amo.classify_lead", side_effect=_classify_today_60pct),
        ])
        text = result["text"]
        # Оба окна квалифицированы 100% (мок классифицирует все как «квал») — дельта 0
        assert "п.п. к вчера" in text

    def test_verdict_shows_cost_per_qual_comparison(self):
        """Вердикт показывает цену квала сегодня и вчера, если есть расход и квалы."""
        today_leads = [_make_lead() for _ in range(5)]
        yesterday_leads = [_make_lead() for _ in range(5)]

        result = _build_with_decisions([], extra_patches=[
            patch("services.morning_digest._get_fb_spend_for_day", side_effect=_fb_spend_by_day(100.0, 200.0)),
            patch("integrations.amo.get_leads_window", side_effect=_leads_by_window(today_leads, yesterday_leads)),
            patch("integrations.amo.classify_lead", side_effect=_classify_all("квал")),
        ])
        text = result["text"]
        assert "цена квала" in text
        # Сегодня $100/5квал=$20, вчера $200/5квал=$40
        assert "$20" in text
        assert "$40" in text


# ---------------------------------------------------------------------------
# Тест: секция «Что сделал бот» v2 — паузы ПОЛНЫМИ блоками, удержания тем же форматом
# ---------------------------------------------------------------------------

class TestBotActionsSectionV2:
    """Паузы — ВСЕ решения дня полными блоками (формат _format_pause_block:
    город, имя, 💸 расход, 👥 квал+оплаты, 📉 причина). Удержания — тем же
    форматом (_format_held_section). Подъёмы бюджета — кратко, как было."""

    def test_all_pauses_shown_not_just_top3(self):
        """ВСЕ паузы дня показываются полными блоками, не только топ-3 (regression
        фидбека владельца: раньше топ-3, теперь полный список)."""
        decisions = [
            _make_decision("PAUSED", f"CityA | Реклама {c}", f"ad_{c}", "autopilot", spend=float(500 - c * 10))
            for c in range(5)
        ]
        result = _build_with_decisions(decisions)
        text = result["text"]
        assert "⏸ <b>Паузы: 5</b>" in text
        for c in range(5):
            assert f"Реклама {c}" in text

    def test_pause_block_has_city_spend_qual_reason(self):
        """Блок паузы содержит город (из «Город | Имя»), 💸 расход, 👥 квал, 📉 причину."""
        decisions = [
            _make_decision(
                "PAUSED", "CityC | Пробный период", "ad_sh1", "autopilot",
                reason="CPL_TOO_HIGH: cpl 45.0 > порог 30.0", spend=250.0,
            ),
        ]
        result = _build_with_decisions(decisions)
        text = result["text"]
        assert "CityC" in text
        assert "Пробный период" in text
        assert "$250" in text
        assert "квал" in text
        assert "💸" in text
        assert "👥" in text
        assert "📉" in text

    def test_pause_block_uses_humanized_reason_not_raw_technical(self):
        """Причина паузы в блоке — человеческая фраза (через _humanize_pause_reason),
        не сырой технический код правила. Формат reason как в decision_policy.py:
        части разделены "; ", каждая может начинаться с служебного префикса "PAUSE: "."""
        decisions = [
            _make_decision(
                "PAUSED", "CityA | Тест", "ad_a1", "autopilot",
                reason="PAUSE: подтверждённый слив (тир 1) — высокий CPL",
            ),
        ]
        result = _build_with_decisions(decisions)
        text = result["text"]
        # _humanize_pause_reason убирает служебный префикс "PAUSE: "
        assert "PAUSE:" not in text
        assert "подтверждённый слив" in text

    def test_held_shown_with_full_block_format(self):
        """Удержания сегодняшние показываются ПОЛНЫМ блоком (_format_held_section:
        имя, дата окончания, ROMI, расход, квал) — тем же форматом что паузы."""
        today_iso = _NOW.date().isoformat()
        hold_state = {
            "holds": {
                "ad_h1": {
                    "ad_name": "CityA | Держим",
                    "held_at": f"{today_iso}T10:00:00+05:00",
                    "hold_until": "2099-01-01T00:00:00+05:00",
                    "romi": 150.0,
                    "spend": 300.0,
                    "qual_pct": 18.0,
                },
            }
        }
        result = _build_with_decisions([], extra_patches=[
            patch("services.autopilot_hold.load_hold_state", return_value=hold_state),
        ])
        text = result["text"]
        assert "Держу 1" in text
        assert "CityA | Держим" in text
        assert "ROMI 150%" in text
        assert "$300" in text
        assert "18%" in text

    def test_held_from_yesterday_not_counted(self):
        """Удержания, поставленные не сегодня, НЕ считаются в «Держу N» вечернего отчёта."""
        hold_state = {
            "holds": {
                "ad_h1": {"ad_name": "X", "held_at": "2020-01-01T10:00:00+05:00", "hold_until": "2099-01-01T00:00:00+05:00"},
            }
        }
        result = _build_with_decisions([], extra_patches=[
            patch("services.autopilot_hold.load_hold_state", return_value=hold_state),
        ])
        text = result["text"]
        assert "Держу" not in text

    def test_budget_raised_shown_briefly_as_before(self):
        """Подъём бюджета показывает адсет и +% из reason кратко (пункт 4 фидбека:
        «Подъёмы... как было»), скобка процента не теряется."""
        decisions = [
            _make_decision(
                "BUDGET_RAISED", "CityB | Победитель", "ad_r1", "budget_pilot",
                reason="Адсет CityB PRODA: $100→$115 (+15%, сегодня 15%/15%), 5 оплат за 7дн",
            ),
        ]
        result = _build_with_decisions(decisions)
        text = result["text"]
        assert "⬆️ Подняли бюджет: 1" in text
        assert "Адсет CityB PRODA: $100" in text
        # Закрывающая скобка процента роста НЕ должна теряться при обрезке (regression:
        # reason.split(",")[0] резал строку на первой запятой ВНУТРИ "(+15%, сегодня 15%/15%)",
        # оставляя "(+15%" без закрывающей ")" — теперь скобка закрывается полностью).
        assert "(+15%, сегодня 15%/15%)" in text
        assert "оплат за 7дн" not in text

    def test_zero_pauses_not_shown_when_other_actions_exist(self):
        """Если пауз 0, но есть подъём бюджета — «0 пауз» НЕ пишем, пункт паузы просто отсутствует."""
        decisions = [
            _make_decision("BUDGET_RAISED", "CityA | Победитель", "ad_b1", "budget_pilot",
                            reason="Адсет CityA: $50→$60 (+20%, сегодня 20%/15%), 3 оплат за 7дн"),
        ]
        result = _build_with_decisions(decisions)
        text = result["text"]
        assert "⏸ <b>Паузы" not in text
        assert "0 пауз" not in text
        assert "⬆️ Подняли бюджет: 1" in text

    def test_launches_today_counted(self, tmp_path):
        """Запуски сегодня считаются из auto_launch_state.json (launched_ever)."""
        today_iso = _NOW.date().isoformat()
        launch_file = tmp_path / "auto_launch_state.json"
        launch_file.write_text(json.dumps({
            "launched_ever": {"card1": f"{today_iso}T09:00:00+05:00", "card2": "2020-01-01T00:00:00+05:00"}
        }), encoding="utf-8")

        result = _build_with_decisions([], extra_patches=[
            patch("services.evening_report._LAUNCH_STATE_PATH", launch_file),
        ])
        text = result["text"]
        assert "🚀 Запусков: 1" in text

    def test_briefs_today_counted(self, tmp_path):
        """Карточки ТЗ считаются из brief_gen_state.json при last_run_date == сегодня."""
        today_iso = _NOW.date().isoformat()
        brief_file = tmp_path / "brief_gen_state.json"
        brief_file.write_text(json.dumps({
            "last_run_date": today_iso,
            "generated_signatures": ["sig1", "sig2", "sig3"],
        }), encoding="utf-8")

        result = _build_with_decisions([], extra_patches=[
            patch("services.evening_report._BRIEF_STATE_PATH", brief_file),
        ])
        text = result["text"]
        assert "🃏 Карточек ТЗ: 3" in text

    def test_briefs_not_today_not_counted(self, tmp_path):
        """Если last_run_date не сегодня — карточки ТЗ не показываются."""
        brief_file = tmp_path / "brief_gen_state.json"
        brief_file.write_text(json.dumps({
            "last_run_date": "2020-01-01",
            "generated_signatures": ["sig1"],
        }), encoding="utf-8")

        result = _build_with_decisions([], extra_patches=[
            patch("services.evening_report._BRIEF_STATE_PATH", brief_file),
        ])
        text = result["text"]
        assert "Карточек ТЗ" not in text

    # -----------------------------------------------------------------------
    # Регрессия: генератор ТЗ теперь пишет last_scheduled_run_date/
    # last_manual_run_date вместо единого last_run_date (см.
    # services/brief_generator.py::_latest_brief_run_date) — секция должна
    # видеть прогон по ЛЮБОЙ из новых меток, а не только по legacy-ключу.
    # Значения — полные ISO-таймстампы (как реально пишет
    # datetime.now().isoformat() в generate_and_push_briefs), не просто дата.
    # -----------------------------------------------------------------------

    def test_briefs_today_counted_via_scheduled_key_only(self, tmp_path):
        """Только last_scheduled_run_date == сегодня (плановый крон) — секция видит прогон."""
        today_iso = _NOW.date().isoformat()
        brief_file = tmp_path / "brief_gen_state.json"
        brief_file.write_text(json.dumps({
            "last_scheduled_run_date": f"{today_iso}T08:00:05.123456",
            "last_manual_run_date": None,
            "generated_signatures": ["sig1", "sig2"],
        }), encoding="utf-8")

        result = _build_with_decisions([], extra_patches=[
            patch("services.evening_report._BRIEF_STATE_PATH", brief_file),
        ])
        text = result["text"]
        assert "🃏 Карточек ТЗ: 2" in text

    def test_briefs_today_counted_via_manual_key_only(self, tmp_path):
        """Только last_manual_run_date == сегодня (/brief, HTTP-эндпоинт) — секция видит прогон
        (обратная совместимость: раньше секция читала единый last_run_date, писавшийся
        И плановым, И ручным прогоном — теперь смотрит на обе новые метки)."""
        today_iso = _NOW.date().isoformat()
        brief_file = tmp_path / "brief_gen_state.json"
        brief_file.write_text(json.dumps({
            "last_scheduled_run_date": None,
            "last_manual_run_date": f"{today_iso}T14:12:00.000001",
            "generated_signatures": ["sig1"],
        }), encoding="utf-8")

        result = _build_with_decisions([], extra_patches=[
            patch("services.evening_report._BRIEF_STATE_PATH", brief_file),
        ])
        text = result["text"]
        assert "🃏 Карточек ТЗ: 1" in text

    def test_briefs_uses_freshest_of_scheduled_and_manual(self, tmp_path):
        """Обе метки заданы, свежее — сегодняшний ручной прогон (scheduled — вчера):
        секция берёт более позднюю дату, видит сегодняшний прогон."""
        today_iso = _NOW.date().isoformat()
        yesterday_iso = (_NOW - timedelta(days=1)).date().isoformat()
        brief_file = tmp_path / "brief_gen_state.json"
        brief_file.write_text(json.dumps({
            "last_scheduled_run_date": f"{yesterday_iso}T08:00:00",
            "last_manual_run_date": f"{today_iso}T09:30:00",
            "generated_signatures": ["sig1", "sig2", "sig3"],
        }), encoding="utf-8")

        result = _build_with_decisions([], extra_patches=[
            patch("services.evening_report._BRIEF_STATE_PATH", brief_file),
        ])
        text = result["text"]
        assert "🃏 Карточек ТЗ: 3" in text

    def test_briefs_freshest_of_both_not_today_not_counted(self, tmp_path):
        """Обе метки заданы, но свежайшая всё равно не сегодня — карточки ТЗ не показываются."""
        two_days_ago_iso = (_NOW - timedelta(days=2)).date().isoformat()
        three_days_ago_iso = (_NOW - timedelta(days=3)).date().isoformat()
        brief_file = tmp_path / "brief_gen_state.json"
        brief_file.write_text(json.dumps({
            "last_scheduled_run_date": f"{two_days_ago_iso}T08:00:00",
            "last_manual_run_date": f"{three_days_ago_iso}T09:00:00",
            "generated_signatures": ["sig1"],
        }), encoding="utf-8")

        result = _build_with_decisions([], extra_patches=[
            patch("services.evening_report._BRIEF_STATE_PATH", brief_file),
        ])
        text = result["text"]
        assert "Карточек ТЗ" not in text


# ---------------------------------------------------------------------------
# Тест: кнопки «Вернуть»
# ---------------------------------------------------------------------------

class TestButtons:
    """Кнопки «Вернуть» — только для сегодняшних пауз от autopilot."""

    def test_buttons_contain_undo_for_paused_by_autopilot(self):
        decisions = [
            _make_decision("PAUSED", "CityC | Тест 1", "ad_p1", "autopilot"),
            _make_decision("PAUSED", "CityA | Тест 2", "ad_p2", "autopilot"),
        ]
        result = _build_with_decisions(decisions)
        buttons = result["buttons"]
        assert buttons[0][0][1] == "ack"
        callback_data = [row[0][1] for row in buttons[1:]]
        assert "undo:ad_p1" in callback_data
        assert "undo:ad_p2" in callback_data

    def test_no_undo_buttons_for_budget_raised(self):
        """BUDGET_RAISED решения НЕ генерируют кнопки «Вернуть»."""
        decisions = [
            _make_decision("BUDGET_RAISED", "CityB | X", "ad_b1", "budget_pilot"),
        ]
        result = _build_with_decisions(decisions)
        buttons = result["buttons"]
        assert len(buttons) == 1
        assert buttons[0][0][1] == "ack"

    def test_no_undo_buttons_for_owner_paused(self):
        """Паузы от owner_telegram НЕ генерируют кнопки «Вернуть» (только autopilot)."""
        decisions = [
            _make_decision("PAUSED", "CityA | X", "ad_o1", "owner_telegram"),
        ]
        result = _build_with_decisions(decisions)
        buttons = result["buttons"]
        assert len(buttons) == 1


# ---------------------------------------------------------------------------
# Тест: секция «Сомнения» — читает журнал data/doubt_log.json за сегодня
# ---------------------------------------------------------------------------

class TestDoubtsSection:
    """Секция «Сомнения» показывается, только если за сегодня есть записи в
    журнале services.doubt_log (пустой день → секция скрыта целиком)."""

    def test_doubts_section_absent_by_default(self):
        """Нет записей в журнале за сегодня — секция «Сомнения» отсутствует."""
        result = _build_with_decisions([])
        text = result["text"]
        assert "Сомнения" not in text

    def test_doubts_section_shown_when_entries_exist(self):
        """Есть запись в журнале за сегодня — секция появляется с причинами
        маркированным списком и строкой решения."""
        entries = [{
            "date": _NOW.date().isoformat(),
            "triggers": ["ДРР почти равен цели", "CDP лёг — считаю по листу"],
            "decision": "поднимаю бюджет (гейты прошли)",
        }]
        result = _build_with_decisions([], extra_patches=[
            patch("services.doubt_log.get_doubt_entries_for_date", return_value=entries),
        ])
        text = result["text"]
        assert "<b>Сомнения</b>" in text
        assert "ДРР почти равен цели" in text
        assert "CDP лёг — считаю по листу" in text
        assert "поднимаю бюджет (гейты прошли)" in text

    def test_doubts_section_escapes_html(self):
        """Причины и решение в секции «Сомнения» экранируются (пользовательский
        текст decision/triggers не должен ломать HTML Telegram)."""
        entries = [{
            "date": _NOW.date().isoformat(),
            "triggers": ['<script>alert("x")</script>'],
            "decision": "<b>тест</b>",
        }]
        result = _build_with_decisions([], extra_patches=[
            patch("services.doubt_log.get_doubt_entries_for_date", return_value=entries),
        ])
        text = result["text"]
        assert "<script>" not in text
        assert "&lt;script&gt;" in text


# ---------------------------------------------------------------------------
# Тест: ошибки секций не роняют отчёт
# ---------------------------------------------------------------------------

class TestErrorFallback:
    """При ошибке любой секции — отчёт всё равно строится с заглушками."""

    def test_fb_error_report_still_has_structure(self):
        """Даже при ошибке FB-расхода отчёт содержит заголовок и секцию «Завтра»."""
        result = _build_with_decisions([], extra_patches=[
            patch("services.morning_digest._get_fb_spend_for_day",
                  side_effect=RuntimeError("FB rate limit")),
        ])
        text = result["text"]
        assert "Итог дня" in text
        assert "Завтра" in text
        assert "10:00" in text

    def test_amo_error_shows_no_data_placeholder(self):
        """При ошибке AMO — «нет данных», отчёт не падает."""
        result = _build_with_decisions([], extra_patches=[
            patch("integrations.amo.get_leads_window", side_effect=ConnectionError("timeout")),
        ])
        text = result["text"]
        assert "нет данных" in text

    def test_decisions_repo_error_shows_no_actions(self):
        """При ошибке decisions_repo — «Сегодня действий не было», отчёт не падает."""
        mock_repo = MagicMock()
        mock_repo.get_decisions_history.side_effect = RuntimeError("db down")

        patches = _default_patches()
        with patch.dict("sys.modules", {"agent.repositories.decisions_repo": mock_repo}):
            started: list = []
            try:
                for p in patches:
                    p.start()
                    started.append(p)
                er_module = _reload_module()
                result = er_module.build_evening_report(_NOW)
            finally:
                for p in reversed(started):
                    p.stop()

        assert "Сегодня действий не было." in result["text"]


# ---------------------------------------------------------------------------
# Тест: усечение по лимиту Telegram
# ---------------------------------------------------------------------------

class TestTruncation:
    """Отчёт не превышает лимит Telegram (4096), усекается по блокам/строкам."""

    def test_many_pauses_do_not_exceed_telegram_limit(self):
        """Даже при большом количестве решений итоговый текст <= 4096 символов."""
        decisions = [
            _make_decision("PAUSED", f"Очень длинное имя объявления номер {i} " * 3, f"ad_{i}", "autopilot")
            for i in range(200)
        ]
        result = _build_with_decisions(decisions)
        assert len(result["text"]) <= 4096

    def test_truncated_report_still_has_header(self):
        """При усечении заголовок всё равно присутствует (режем с конца)."""
        decisions = [
            _make_decision("PAUSED", f"Имя {i} " * 5, f"ad_{i}", "autopilot")
            for i in range(300)
        ]
        result = _build_with_decisions(decisions)
        assert "🌇 <b>Итог дня" in result["text"]

    def test_truncation_marker_honest_and_ends_with_n_more(self):
        """При усечении в конце — честная пометка «…и ещё N» (переиспользуем паттерн
        _truncate_to_limit: «…отчёт обрезан», единое честное усечение всего сообщения)."""
        decisions = [
            _make_decision("PAUSED", f"Реклама с длинным именем {i} " * 4, f"ad_{i}", "autopilot")
            for i in range(300)
        ]
        result = _build_with_decisions(decisions)
        assert "…отчёт обрезан" in result["text"]


# ---------------------------------------------------------------------------
# Тест: should_send_report — граничные случаи (логика не менялась)
# ---------------------------------------------------------------------------

class TestShouldSendReport:
    """should_send_report — чистая функция проверки условий отправки."""

    def test_before_21_returns_false(self):
        """В 20:59 отчёт НЕ шлётся."""
        now = datetime(2026, 6, 11, 20, 59, 0, tzinfo=_TZ_LOCAL)
        from services.evening_report import should_send_report
        assert should_send_report(now, None) is False

    def test_at_21_05_returns_true(self):
        """В 21:05 отчёт шлётся (если сегодня ещё не слали)."""
        now = datetime(2026, 6, 11, 21, 5, 0, tzinfo=_TZ_LOCAL)
        from services.evening_report import should_send_report
        assert should_send_report(now, None) is True

    def test_already_sent_today_returns_false(self):
        """Если уже слали сегодня — не шлём повторно."""
        now = datetime(2026, 6, 11, 21, 30, 0, tzinfo=_TZ_LOCAL)
        today = date(2026, 6, 11)
        from services.evening_report import should_send_report
        assert should_send_report(now, today) is False

    def test_sent_yesterday_returns_true(self):
        """Если слали вчера — сегодня шлём снова."""
        now = datetime(2026, 6, 11, 21, 0, 0, tzinfo=_TZ_LOCAL)
        yesterday = date(2026, 6, 10)
        from services.evening_report import should_send_report
        assert should_send_report(now, yesterday) is True

    def test_midnight_hour_returns_false(self):
        """В 00:00 отчёт НЕ шлётся."""
        now = datetime(2026, 6, 11, 0, 0, 0, tzinfo=_TZ_LOCAL)
        from services.evening_report import should_send_report
        assert should_send_report(now, None) is False


# ---------------------------------------------------------------------------
# Тест: HTML-инъекция в именах объявлений экранируется
# ---------------------------------------------------------------------------

class TestHtmlEscaping:
    """Пользовательские данные в тексте отчёта должны быть HTML-экранированы."""

    def test_html_injection_in_ad_name_is_escaped(self):
        """Теги в имени объявления экранируются: < > &."""
        dangerous_name = 'CityA | <script>alert("xss")</script> & "test"'
        decisions = [
            _make_decision("PAUSED", dangerous_name, "ad_xss", "autopilot"),
        ]
        result = _build_with_decisions(decisions)
        text = result["text"]
        assert "<script>" not in text
        assert "&lt;script&gt;" in text or "script" in text
        assert "&amp;" in text

    def test_html_injection_in_budget_raised_reason_is_escaped(self):
        """Теги в reason подъёма бюджета тоже экранируются."""
        dangerous_reason = '<b>не паузить!</b>, adset X'
        decisions = [
            _make_decision("BUDGET_RAISED", "Нормальное имя", "ad_r1", "budget_pilot", dangerous_reason),
        ]
        result = _build_with_decisions(decisions)
        text = result["text"]
        assert "<b>не паузить!</b>" not in text


# ---------------------------------------------------------------------------
# Тест: расход FB отсутствует (FB ещё не посчитал день) — «данных ещё нет», не $0
# ---------------------------------------------------------------------------

class TestFbSpendMissingIsNotZero:
    """Пустой data[] у FB insights = «данных ещё нет», НЕ расход $0.

    Случай: вечерний отчёт в 21:00 (сутки кабинета открылись 9 часов назад)
    заявлял «расход сегодня $0» на пустом ответе Graph, Approval Checker бракует
    такое утверждение кодом NULL_COERCED_TO_ZERO — blocking, и весь отчёт
    не уходил владельцу.
    """

    def test_fb_insights_empty_rows_give_none_not_zero(self):
        """_get_fb_spend_for_day: data[] пуст → None (а не 0.0)."""
        from unittest.mock import MagicMock as _MM

        from services.morning_digest import _get_fb_spend_for_day

        response = _MM(status_code=200)
        response.json.return_value = {"data": []}
        with (
            patch("agent.fb_common._throttled_get", return_value=response),
            patch("services.fb_token_provider.get_fb_token", return_value="tok"),
            patch("services.fb_token_provider.get_fb_account_id", return_value="123"),
        ):
            assert _get_fb_spend_for_day("2026-07-28") is None

    def test_money_section_says_no_data_instead_of_zero_spend(self):
        """Секция «Деньги» рендерит «FB: данных ещё нет», а не «$0»."""
        result = _build_with_decisions([], extra_patches=[
            patch("services.morning_digest._get_fb_spend_for_day", return_value=None),
        ])
        today_line = next(
            line for line in result["text"].split("\n") if "Сегодня (день кабинета" in line
        )
        assert "FB: данных ещё нет" in today_line
        # Расход FB не превращается в число: ни «FB $0», ни общий тотал.
        assert "FB $" not in today_line
        assert "расход $" not in today_line

    def test_missing_fb_spend_claim_has_no_value_and_is_not_required(self):
        """Claim расхода не утверждает число без значения и не блокирует отчёт."""
        result = _build_with_decisions([], extra_patches=[
            patch("services.morning_digest._get_fb_spend_for_day", return_value=None),
        ])
        claims = {claim.field_id: claim for claim in result["check_request"].claims}
        assert claims["today.fb_spend"].value is None
        assert claims["today.fb_spend"].required is False

    def test_missing_fb_spend_does_not_block_report(self):
        """Чекер по пустому расходу даёт EVIDENCE_MISSING без blocking → не BLOCKED."""
        from decimal import Decimal

        from services.approval_checker_models import (
            EvidenceState,
            FactCategory,
            Metric,
            SourceSystem,
        )
        from services.approval_rules import compare_claim

        result = _build_with_decisions([], extra_patches=[
            patch("services.morning_digest._get_fb_spend_for_day", return_value=None),
        ])
        claims = {claim.field_id: claim for claim in result["check_request"].claims}

        missing = compare_claim(claims["today.fb_spend"], None)
        assert [issue.code for issue in missing.issues] == ["EVIDENCE_MISSING"]
        assert all(not issue.blocking for issue in missing.issues)

        # Контроль: тот же claim со значением 0 остался бы blocking-претензией.
        zero_claim = type(claims["today.fb_spend"])(
            claim_id="claim:today.fb_spend",
            field_id="today.fb_spend",
            category=FactCategory.BUSINESS_METRIC,
            subject=claims["today.fb_spend"].subject,
            metric=Metric.SPEND,
            value=Decimal("0"),
            source=SourceSystem.FACEBOOK,
            window=claims["today.fb_spend"].window,
            currency="USD",
        )
        zeroed = compare_claim(zero_claim, None)
        assert [issue.code for issue in zeroed.issues] == ["NULL_COERCED_TO_ZERO"]
        assert all(issue.blocking for issue in zeroed.issues)
        assert EvidenceState  # импорт-маркер: сравнение шло без записи источника


# ---------------------------------------------------------------------------
# Тест: окна расхода FB = сутки кабинета (их Graph умеет доказать)
# ---------------------------------------------------------------------------

class TestFbClaimWindowsMatchAccountDay:
    """Расход FB подписывается сутками кабинета, а не окном AMO-лидов.

    _get_fb_spend_for_day меряет insights time_range since=until=дата по времени
    аккаунта; approval_source_facebook._load_insights доказывает ТОЛЬКО окна,
    обе границы которых попадают в полночь кабинета. Окно «12:00 → сейчас» и
    локальные сутки такому условию не удовлетворяли → EVIDENCE_MISSING.
    """

    def test_fb_windows_are_whole_account_days(self):
        result = _build_with_decisions([])
        fields = {f.field_id: f for f in result["check_request"].payload.fields}

        today_window = fields["today.fb_spend"].window
        yesterday_window = fields["yesterday.fb_spend"].window
        for window in (today_window, yesterday_window):
            assert window.start.hour == 12
            assert window.end.hour == 12
            assert window.end - window.start == timedelta(days=1)
        assert today_window.start.date() == _NOW.date()
        assert yesterday_window.end == today_window.start

    def test_fb_window_bounds_are_claimed_as_fb_window_not_amo_window(self):
        result = _build_with_decisions([])
        fields = {f.field_id: f for f in result["check_request"].payload.fields}
        assert fields["today.window_start"].value == fields["today.fb_spend"].window.start.isoformat()
        assert fields["today.window_end"].value == fields["today.fb_spend"].window.end.isoformat()
        # А лиды остаются на своём окне AMO (с 12:00 до «сейчас»).
        assert fields["today.amo_leads"].window != fields["today.fb_spend"].window
        assert fields["today.amo_leads"].window.end.hour == _NOW.hour


# ---------------------------------------------------------------------------
# Тест: лиды заявляются источником AMO и контракт это разрешает
# ---------------------------------------------------------------------------

class TestLeadsClaimUsesAmoCanon:
    """Лиды отчёта — AMO-канон (воронка «Новые продажи»), не FB-каунты."""

    def test_leads_claim_source_is_amo_and_contract_allows_it(self):
        from services.approval_checker_models import Metric, SourceSystem
        from services.approval_rules import _source_contract_issue

        result = _build_with_decisions([])
        claims = {claim.field_id: claim for claim in result["check_request"].claims}

        for field_id in ("today.amo_leads", "yesterday.amo_leads"):
            claim = claims[field_id]
            assert claim.source is SourceSystem.AMO
            assert claim.metric is Metric.LEADS
            assert _source_contract_issue(claim) is None

    def test_contract_still_allows_facebook_lead_counts(self):
        """Канонизация Meta-лидов не сломана: FB как источник LEADS остаётся."""
        from services.approval_rules import _METRIC_SOURCES
        from services.approval_checker_models import Metric, SourceSystem

        assert _METRIC_SOURCES[Metric.LEADS] == frozenset(
            {SourceSystem.FACEBOOK, SourceSystem.AMO}
        )

    def test_leads_come_from_amo_window_call_not_fb_insights(self):
        """Значение лидов приходит из integrations.amo.get_leads_window."""
        amo_leads = [_make_lead() for _ in range(7)]
        with patch("integrations.amo.get_leads_window") as leads_mock:
            leads_mock.side_effect = _leads_by_window(amo_leads, [])
            result = _build_with_decisions([], extra_patches=[
                patch("integrations.amo.get_leads_window", side_effect=_leads_by_window(amo_leads, [])),
            ])
        claims = {claim.field_id: claim for claim in result["check_request"].claims}
        assert claims["today.amo_leads"].value == 7


def test_fetch_contour_pauses_reads_confirmed_attempts(tmp_path):
    """Регрессия: вечерний отчёт показывал «действий не было», потому
    что паузы контура одобрений не попадали в decisions. Подтверждённые PAUSE_AD
    попытки дня должны приходить в формате decisions_repo (SYSTEM → autopilot,
    кнопки «Вернуть»; OWNER → owner_telegram)."""
    import sqlite3 as _sqlite
    from datetime import timezone as _tz
    from services import evening_report as er

    db = tmp_path / "er.db"
    conn = _sqlite.connect(db)
    conn.executescript("""
        CREATE TABLE owner_action_attempts (attempt_id TEXT PRIMARY KEY, decision_id TEXT,
            operation_kind TEXT, resource_id TEXT, state TEXT, completed_at TEXT);
        CREATE TABLE owner_action_decisions (decision_id TEXT PRIMARY KEY, decision_source TEXT);
        CREATE TABLE creative_kb (ad_id TEXT PRIMARY KEY, ad_name TEXT, spend REAL, leads INTEGER, cpl REAL, qual_pct REAL);
    """)
    now = datetime.now(er._TZ_LOCAL).replace(hour=20, minute=0, second=0, microsecond=0)
    at = (now - timedelta(hours=3)).astimezone(_tz.utc).isoformat()
    yesterday = (now - timedelta(days=1)).astimezone(_tz.utc).isoformat()
    conn.execute("INSERT INTO owner_action_decisions VALUES ('s1','SYSTEM'), ('o1','OWNER')")
    conn.execute("INSERT INTO owner_action_attempts VALUES ('a1','s1','PAUSE_AD','111','CONFIRMED',?)", (at,))
    conn.execute("INSERT INTO owner_action_attempts VALUES ('a2','o1','PAUSE_AD','222','CONFIRMED',?)", (at,))
    conn.execute("INSERT INTO owner_action_attempts VALUES ('a3','s1','PAUSE_AD','333','CONFIRMED',?)", (yesterday,))
    conn.execute("INSERT INTO creative_kb VALUES ('111','CityA | Раз',120.0,7,17.1,0.0), ('222','CityB | Два',80.0,3,26.7,10.0)")
    conn.commit(); conn.close()

    with patch("services.creative_intelligence.DB_PATH", str(db)), \
         patch("services.autonomous_pause.load_state", return_value={"journal": [
             {"ad_id": "111", "name": "CityA | Раз", "business_reason": "20 лидов, оплат 0", "spend": 150.0, "leads": 20, "cpl": 7.5, "qual_pct": 0.0}
         ]}):
        rows = er._fetch_contour_pauses(now)

    by_id = {r["ad_id"]: r for r in rows}
    assert set(by_id) == {"111", "222"}          # вчерашняя 333 — вне суток
    assert by_id["111"]["confirmed_by"] == "autopilot"
    assert by_id["111"]["spend"] == 150.0        # журнал автономии побеждает KB
    assert by_id["111"]["reason"] == "20 лидов, оплат 0"
    assert by_id["222"]["confirmed_by"] == "owner_telegram"
    assert by_id["222"]["ad_name"] == "CityB | Два"  # из KB
    assert all(r["action"] == "PAUSED" for r in rows)
