"""
Тесты человеческого формата Telegram-отчёта авто-запуска (ARCH-auto-launch-report.md).

Проверяем:
- _build_active_report: заголовок, метка продукта, города/объявления, склонения,
  честная строка бюджета vs fmt_money, итог (очередь/деньги/оценка), кнопки Стоп.
- _build_dry_run_report: тот же формат блоков без кнопок, с пометкой «(план)».
- _plural_ru: склонения.
- Stop-map хелперы: _record_launch_stop / get_launch_stop_entry / mark_launch_stopped
  / ретеншн.

Прод-код только читаем, сеть не дёргается (моки).
"""

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from services.auto_launch import (
    _build_active_report,
    _build_dry_run_report,
    _launch_block,
    _plural_ru,
    _record_launch_stop,
    get_launch_stop_entry,
    mark_launch_stopped,
    STOP_MAP_RETENTION_DAYS,
)

_TZ_LOCAL = timezone(timedelta(hours=5))


@pytest.fixture
def clean_state(tmp_path, monkeypatch):
    """Чистый state-файл для каждого теста (как в tests/test_auto_launch.py)."""
    state_file = tmp_path / "auto_launch_state.json"
    monkeypatch.setattr("services.auto_launch._AUTO_LAUNCH_STATE_FILE", state_file)
    return state_file


def _rec(**overrides) -> dict:
    """Базовая запись запуска для тестов отчёта."""
    base = {
        "card_id": "card1",
        "card_name": "Петров / Тема А",
        "campaign_type": "leadgen",
        "cities": None,
        "labels": ["PRODA"],
        "reason": "запуск готовой карточки",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _plural_ru
# ---------------------------------------------------------------------------

class TestPluralRu:
    def test_один(self):
        assert _plural_ru(1, ("город", "города", "городов")) == "город"

    def test_несколько(self):
        assert _plural_ru(3, ("город", "города", "городов")) == "города"

    def test_много(self):
        assert _plural_ru(5, ("город", "города", "городов")) == "городов"

    def test_одиннадцать_исключение(self):
        # 11 заканчивается на 1, но попадает в "many" (11-14 — исключение)
        assert _plural_ru(11, ("город", "города", "городов")) == "городов"


# ---------------------------------------------------------------------------
# _build_active_report
# ---------------------------------------------------------------------------

class TestBuildActiveReportHeader:
    def test_заголовок_и_метка_продукта(self):
        rec = _rec(ad_ids_by_city={
            "CityA": ["1"], "CityB": ["2"], "CityC": ["3"],
            "CityD": ["4"], "CityE": ["5"],
        })
        text, _ = _build_active_report([rec], None)

        assert "🚀 <b>Запустил 1 новую рекламу</b> (из «Готово»)" in text
        assert "[PRODA]" in text

    def test_склонение_заголовка_несколько(self):
        rec1 = _rec(card_id="c1")
        rec2 = _rec(card_id="c2")
        text, _ = _build_active_report([rec1, rec2], None)

        assert "🚀 <b>Запустил 2 новые рекламы</b> (из «Готово»)" in text

    def test_склонение_заголовка_много(self):
        recs = [_rec(card_id=f"c{i}") for i in range(5)]
        text, _ = _build_active_report(recs, None)

        assert "🚀 <b>Запустил 5 новых реклам</b> (из «Готово»)" in text


class TestBuildActiveReportCities:
    def test_города_и_объявления_из_ad_ids_by_city(self):
        rec = _rec(ad_ids_by_city={
            "CityA": ["1"], "CityB": ["2"], "CityC": ["3"],
            "CityD": ["4"], "CityE": ["5"],
        })
        text, _ = _build_active_report([rec], None)

        assert "5 городов · 5 объявлений" in text
        assert "Города: CityA, CityB, CityC, CityD, CityE" in text

    def test_города_не_перечислены_при_больше_пяти(self):
        rec = _rec(ad_ids_by_city={f"Город{i}": [str(i)] for i in range(6)})
        text, _ = _build_active_report([rec], None)

        assert "6 городов · 6 объявлений" in text
        assert "Города:" not in text

    def test_склонения_города_объявления(self):
        rec1 = _rec(card_id="c1", ad_ids_by_city={"CityA": ["1"]})
        text, _ = _build_active_report([rec1], None)
        assert "1 город · 1 объявление" in text

        rec3 = _rec(card_id="c3", ad_ids_by_city={
            "CityA": ["1"], "CityB": ["2"], "CityC": ["3"],
        })
        text, _ = _build_active_report([rec3], None)
        assert "3 города · 3 объявления" in text

        rec5 = _rec(card_id="c5", ad_ids_by_city={
            f"Город{i}": [str(i)] for i in range(5)
        })
        text, _ = _build_active_report([rec5], None)
        assert "5 городов · 5 объявлений" in text


class TestBuildActiveReportBudget:
    def test_бюджет_честная_строка_без_daily_budget(self):
        rec = _rec(ad_ids_by_city={"CityA": ["1"]})
        text, _ = _build_active_report([rec], None)

        assert "Бюджет: по настройкам кампании (адсет не меняли)" in text

    def test_деньги_через_fmt_money(self):
        rec = _rec(ad_ids_by_city={"CityA": ["1"]}, daily_budget_usd=1500)
        text, _ = _build_active_report([rec], None)

        assert "$1.500/день" in text
        assert "💰 Добавлено в день: $1.500" in text

    def test_итоговая_сумма_не_добавляется_если_не_у_всех_известен_бюджет(self):
        rec1 = _rec(card_id="c1", ad_ids_by_city={"CityA": ["1"]}, daily_budget_usd=1500)
        rec2 = _rec(card_id="c2", ad_ids_by_city={"CityB": ["2"]})  # бюджет неизвестен
        text, _ = _build_active_report([rec1, rec2], None)

        assert "Добавлено в день" not in text


class TestBuildActiveReportFooter:
    def test_итог_очередь_новой_формулировкой(self):
        rec = _rec(ad_ids_by_city={"CityA": ["1"]})
        text, _ = _build_active_report([rec], 4)

        assert "📦 В очереди «Готово»: 4 карточки" in text

    def test_итог_без_remaining_in_queue_строка_не_добавляется(self):
        rec = _rec(ad_ids_by_city={"CityA": ["1"]})
        text, _ = _build_active_report([rec], None)

        assert "В очереди" not in text

    def test_итог_строка_ранней_оценки(self):
        rec = _rec(ad_ids_by_city={"CityA": ["1"]})
        text, _ = _build_active_report([rec], None)

        assert "⏱ Первая оценка — через 48 часов" in text

    def test_нет_строки_причина(self):
        rec = _rec(ad_ids_by_city={"CityA": ["1"]})
        text, _ = _build_active_report([rec], None)

        assert "Причина:" not in text


class TestBuildActiveReportButtons:
    def test_кнопка_стоп_на_каждый_запуск(self):
        rec1 = _rec(card_id="card_aaa111", ad_ids_by_city={"CityA": ["1"]}, ad_ids=["1"])
        rec2 = _rec(card_id="card_bbb222", ad_ids_by_city={"CityB": ["2"]}, ad_ids=["2"])
        _, buttons = _build_active_report([rec1, rec2], None)

        assert len(buttons) == 2
        assert buttons[0][0][1] == "stop_launch:card_aaa111"
        assert buttons[1][0][1] == "stop_launch:card_bbb222"

    def test_callback_data_не_превышает_64_байта(self):
        card_id = "a" * 24  # похоже на Trello card_id (24 hex)
        rec = _rec(card_id=card_id, ad_ids_by_city={"CityA": ["1"]}, ad_ids=["1"])
        _, buttons = _build_active_report([rec], None)

        callback_data = buttons[0][0][1]
        assert len(callback_data.encode("utf-8")) <= 64

    def test_имя_кнопки_обрезано_по_границе_слова(self):
        """Раньше было name[:22] — резало посреди слова («...для пров»).
        Владелец против обрубков — теперь truncate_at_word_boundary: режем
        по последнему пробелу до лимита и добавляем "…"."""
        rec = _rec(
            card_id="card1",
            card_name="Очень длинное название карточки для проверки обрезки",
            ad_ids_by_city={"CityA": ["1"]},
            ad_ids=["1"],
        )
        _, buttons = _build_active_report([rec], None)

        label = buttons[0][0][0]
        assert label == "⏸ Остановить «Очень длинное…»"
        assert label.startswith("⏸ Остановить «")
        assert label.endswith("»")
        # обрубка слова "название" посреди слова быть не должно
        assert "назв»" not in label and "назван…" not in label

    def test_имя_кнопки_длинное_имя_режется_по_слову(self):
        """Регрессия: «Петров / Тестовая подт» — обрубок посреди слова «подтема».
        Теперь режем на «Тестовая…» целиком."""
        rec = _rec(
            card_id="card1",
            card_name="Петров / Тестовая подтема",
            ad_ids_by_city={"CityA": ["1"]},
            ad_ids=["1"],
        )
        _, buttons = _build_active_report([rec], None)

        label = buttons[0][0][0]
        assert label == "⏸ Остановить «Петров / Тестовая…»"
        assert "подт»" not in label  # обрубок посреди слова "подтема" запрещён

    def test_имя_кнопки_короткое_не_обрезается(self):
        """Имя короче лимита — без изменений и без многоточия."""
        rec = _rec(
            card_id="card1",
            card_name="Бонус",
            ad_ids_by_city={"CityA": ["1"]},
            ad_ids=["1"],
        )
        _, buttons = _build_active_report([rec], None)

        label = buttons[0][0][0]
        assert label == "⏸ Остановить «Бонус»"
        assert "…" not in label

    def test_нет_кнопки_без_ad_id(self):
        rec = _rec(card_id="card1", ad_ids_by_city={}, ad_ids=[])
        _, buttons = _build_active_report([rec], None)

        assert buttons == []


class TestBuildActiveReportNoneSafety:
    def test_none_safety_cities_и_ad_ids_by_city(self):
        rec = _rec(cities=None, ad_ids_by_city=None)
        text, buttons = _build_active_report([rec], None)

        assert "Города: все города" in text
        assert buttons == []


# ---------------------------------------------------------------------------
# _build_dry_run_report
# ---------------------------------------------------------------------------

class TestBuildDryRunReport:
    def test_план_и_все_города(self):
        rec = _rec(cities=None)
        text = _build_dry_run_report([rec])

        assert "(план)" in text
        assert "все города" in text

    def test_пусто(self):
        text = _build_dry_run_report([])

        assert "Нет готовых карточек для запуска" in text

    def test_блок_без_бюджета_честная_строка(self):
        rec = _rec(cities=None)
        text = _build_dry_run_report([rec])

        assert "Бюджет: по настройкам кампании (адсет не меняли)" in text


class TestLaunchBlock:
    def test_без_названия(self):
        rec = _rec(card_name=None, ad_ids_by_city=None, cities=None)
        text = _launch_block(rec)

        assert "без названия" in text

    def test_метка_продукта_prodb(self):
        rec = _rec(card_name="Сидорова / Заявка на PRODB", labels=["PRODB"], ad_ids_by_city=None, cities=None)
        text = _launch_block(rec)

        assert "[PRODB]" in text


# ---------------------------------------------------------------------------
# Stop-map: _record_launch_stop / get_launch_stop_entry / mark_launch_stopped
# ---------------------------------------------------------------------------

class TestStopMapRecordGet:
    def test_record_и_get_stop_entry(self, clean_state):
        from services.auto_launch import _load_auto_launch_state, _save_auto_launch_state

        state = _load_auto_launch_state()
        _record_launch_stop(state, "card1", "Тестовая карточка", ["111", "222"])
        _save_auto_launch_state(state)

        entry = get_launch_stop_entry("card1")
        assert entry is not None
        assert entry["name"] == "Тестовая карточка"
        assert entry["ad_ids"] == ["111", "222"]
        assert entry["stopped"] is False
        assert entry["at"]

    def test_get_launch_stop_entry_нет_записи(self, clean_state):
        assert get_launch_stop_entry("nope") is None

    def test_mark_launch_stopped_идемпотентно(self, clean_state):
        from services.auto_launch import _load_auto_launch_state, _save_auto_launch_state

        state = _load_auto_launch_state()
        _record_launch_stop(state, "card1", "Карточка", ["111"])
        _save_auto_launch_state(state)

        assert mark_launch_stopped("card1") is True
        entry = get_launch_stop_entry("card1")
        assert entry["stopped"] is True

        # Повторный вызов — тоже True (запись есть, просто снова выставляем True)
        assert mark_launch_stopped("card1") is True

    def test_mark_launch_stopped_несуществующий_id(self, clean_state):
        assert mark_launch_stopped("nope") is False


class TestStopMapRetention:
    def test_ретеншн_чистит_старое(self, clean_state):
        from services.auto_launch import _load_auto_launch_state, _save_auto_launch_state

        old_at = (datetime.now(_TZ_LOCAL) - timedelta(days=STOP_MAP_RETENTION_DAYS + 10)).isoformat()
        state = _load_auto_launch_state()
        state["stop_map"] = {
            "old_card": {"name": "Старая", "ad_ids": ["1"], "stopped": False, "at": old_at},
        }
        _save_auto_launch_state(state)

        state = _load_auto_launch_state()
        _record_launch_stop(state, "new_card", "Новая", ["2"])
        _save_auto_launch_state(state)

        assert get_launch_stop_entry("old_card") is None
        assert get_launch_stop_entry("new_card") is not None
