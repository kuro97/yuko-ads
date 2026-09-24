"""
Городские метки Trello сужают целевые города автозапуска.

Исправленный баг: карточка «Карточка / CityA 3» (метки FB, PRODA,
СТАТИКА, CityA) создавалась ботом в адсете CityF: decide_launches и
_recommendation_from_card отдавали cities=None, _target_cities разворачивал
None во «все города» карты, а метки до выбора городов не доходили.

Проверяем:
- _cities_from_labels: одна метка, несколько, «Все города», без городских
  меток, регистр/пробелы, выключенный через settings город;
- _target_cities: приоритет явного списка и онлайн-контура над метками,
  отказ CITY_LABEL_UNROUTED вместо «все города»;
- decide_launches / _recommendation_from_card: cities из меток;
- ретрай: launched_ever.target_cities только из меток — «висящих» городов
  вне меток не бывает;
- run_auto_launch dry_run: метки доходят до запроса чекера, карточка с
  выключенным городом уходит в отказ, а не во все города.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

import agent.scheduler as scheduler
from services.auto_launch import (
    _cities_from_labels,
    _finalize_launch_attempt,
    _load_auto_launch_state,
    _prepare_launch_attempt,
    _recommendation_from_card,
    _record_city_failure,
    _record_city_success,
    _target_cities,
    decide_launches,
    run_auto_launch,
)
from services.launch_checker import (
    CheckerMode,
    LaunchCheckBlocked,
    check_candidate,
)
from services.launch_routing import all_cities

ALL = ["CityA", "CityB", "CityC", "CityD", "CityE", "CityF"]


@pytest.fixture(autouse=True)
def _no_settings(monkeypatch):
    """Карта маршрутизации чисто дефолтная: все города карты."""
    monkeypatch.setattr(scheduler, "load_settings", lambda: {})


def _disable_city(monkeypatch, city: str) -> None:
    monkeypatch.setattr(
        scheduler,
        "load_settings",
        lambda: {"launch_routing": {"cities": {city: None}}},
    )


@pytest.fixture
def clean_state(tmp_path, monkeypatch):
    state_file = tmp_path / "auto_launch_state.json"
    monkeypatch.setattr("services.auto_launch._AUTO_LAUNCH_STATE_FILE", state_file)
    monkeypatch.setattr(
        "services.auto_launch._AUTO_LAUNCH_RUN_LOCK_FILE",
        tmp_path / "auto-launch-active.lock",
    )
    monkeypatch.setattr("services.auto_launch._mark_card_done", lambda _card_id: None)
    return state_file


def _card(card_id: str, labels: list[str], name: str | None = None) -> dict:
    return {
        "id": card_id,
        "name": name or f"Карточка {card_id}",
        "labels": labels,
        "desc": "",
        "dueComplete": False,
        "pos": 1.0,
    }


LABELED_CARD = _card(
    "cta3",
    ["FB", "PRODA", "СТАТИКА", "CityA"],
    name="Карточка / CityA 3",
)


# ---------------------------------------------------------------------------
# _cities_from_labels
# ---------------------------------------------------------------------------


class TestCitiesFromLabels:
    def test_карта_по_умолчанию_все_города_карты(self):
        assert all_cities() == ALL

    def test_одна_городская_метка(self):
        assert _cities_from_labels(["FB", "PRODA", "СТАТИКА", "CityA"]) == ["CityA"]

    def test_несколько_меток_в_порядке_карты(self):
        assert _cities_from_labels(["CityF", "PRODA", "CityA"]) == ["CityA", "CityF"]

    def test_метка_все_города_даёт_none_даже_рядом_с_городами(self):
        assert _cities_from_labels(["Все города", "CityA"]) is None
        assert _cities_from_labels(["ВСЕ ГОРОДА"]) is None

    def test_без_городских_меток_none(self):
        assert _cities_from_labels(["FB", "PRODA", "СТАТИКА"]) is None
        assert _cities_from_labels([]) is None

    def test_не_список_none(self):
        assert _cities_from_labels(None) is None
        assert _cities_from_labels("CityA") is None

    def test_регистр_и_пробелы_не_мешают(self):
        assert _cities_from_labels([" citya ", "CITYB"]) == ["CityA", "CityB"]

    def test_только_точное_имя_города(self):
        """«CityA 3» из названия карточки — не городская метка."""
        assert _cities_from_labels(["CityA 3", "CityA Б"]) is None

    def test_дубли_меток_схлопываются(self):
        assert _cities_from_labels(["CityA", "citya"]) == ["CityA"]

    def test_выключенный_город_даёт_пустой_список(self, monkeypatch):
        _disable_city(monkeypatch, "CityF")
        assert _cities_from_labels(["CityF", "PRODA"]) == []

    def test_выключенный_рядом_с_живым_остаётся_только_живой(self, monkeypatch):
        _disable_city(monkeypatch, "CityF")
        assert _cities_from_labels(["CityF", "CityA"]) == ["CityA"]


# ---------------------------------------------------------------------------
# _target_cities
# ---------------------------------------------------------------------------


class TestTargetCities:
    def test_метка_сужает_до_одного_города(self):
        rec = {"campaign_type": "leadgen", "cities": None, "labels": LABELED_CARD["labels"]}
        assert _target_cities(rec) == ["CityA"]

    def test_несколько_меток(self):
        rec = {"campaign_type": "leadgen", "labels": ["CityB", "CityE", "PRODA"]}
        assert _target_cities(rec) == ["CityB", "CityE"]

    def test_все_города_и_без_меток_прежнее_поведение(self):
        assert _target_cities({"campaign_type": "leadgen", "labels": ["Все города", "CityA"]}) == ALL
        assert _target_cities({"campaign_type": "leadgen", "labels": ["PRODA"]}) == ALL
        assert _target_cities({"campaign_type": "leadgen"}) == ALL

    def test_явный_список_сильнее_меток(self):
        rec = {"campaign_type": "leadgen", "cities": ["CityB"], "labels": ["CityA"]}
        assert _target_cities(rec) == ["CityB"]

    def test_онлайн_контур_сильнее_меток(self):
        rec = {"campaign_type": "mql_online", "labels": ["CityA"]}
        assert _target_cities(rec) == ["Онлайн"]

    def test_выключенный_город_отказ_а_не_все_города(self, monkeypatch):
        _disable_city(monkeypatch, "CityF")
        with pytest.raises(LaunchCheckBlocked) as info:
            _target_cities({"campaign_type": "leadgen", "labels": ["CityF"]})
        assert info.value.code == "CITY_LABEL_UNROUTED"


# ---------------------------------------------------------------------------
# decide_launches / _recommendation_from_card
# ---------------------------------------------------------------------------


class TestDecideLaunchesByLabels:
    def test_одна_метка(self, clean_state):
        recs = decide_launches({}, [LABELED_CARD])
        assert [r["cities"] for r in recs] == [["CityA"]]
        assert recs[0]["campaign_type"] == "leadgen"

    def test_несколько_меток(self, clean_state):
        recs = decide_launches({}, [_card("c", ["CityC", "PRODA", "CityA"])])
        assert recs[0]["cities"] == ["CityA", "CityC"]

    def test_все_города(self, clean_state):
        recs = decide_launches({}, [_card("c", ["Все города", "PRODA"])])
        assert recs[0]["cities"] is None

    def test_без_городских_меток(self, clean_state):
        recs = decide_launches({}, [_card("c", ["FB", "PRODA", "СТАТИКА"])])
        assert recs[0]["cities"] is None

    def test_выключенный_город_карточка_пропущена(self, clean_state, monkeypatch, caplog):
        _disable_city(monkeypatch, "CityF")
        cards = [_card("ctf", ["CityF", "PRODA"]), _card("cta", ["CityA"])]
        with caplog.at_level("WARNING"):
            recs = decide_launches({}, cards)
        assert [r["card_id"] for r in recs] == ["cta"]
        assert "CITY_LABEL_UNROUTED" in caplog.text or "не входят в карту" in caplog.text

    def test_recommendation_from_card_берёт_метки(self):
        assert _recommendation_from_card(LABELED_CARD)["cities"] == ["CityA"]
        assert _recommendation_from_card(_card("c", ["PRODA"]))["cities"] is None
        assert _recommendation_from_card(_card("c", ["Все города", "CityA"]))["cities"] is None

    def test_recommendation_from_card_выключенный_город_пустой_список(self, monkeypatch):
        _disable_city(monkeypatch, "CityF")
        assert _recommendation_from_card(_card("c", ["CityF"]))["cities"] == []


# ---------------------------------------------------------------------------
# Ретрай: контракт городов только из меток
# ---------------------------------------------------------------------------


class TestRetryKeepsLabelContract:
    def _rec(self, labels):
        return {
            "card_id": "retry",
            "card_name": "Карточка ретрая",
            "campaign_type": "leadgen",
            "cities": None,
            "labels": labels,
        }

    def test_launched_ever_хранит_только_города_меток(self, clean_state):
        rec = self._rec(["CityA", "CityB", "PRODA"])
        key = _prepare_launch_attempt(rec, _target_cities(rec))
        _record_city_success(key, "CityA", ["ad-citya"])
        _record_city_failure(key, "CityB", "adset full")
        assert _finalize_launch_attempt(key) == "PARTIAL"

        state = _load_auto_launch_state()
        entry = state["launched_ever"]["retry"]
        assert entry["target_cities"] == ["CityA", "CityB"]
        assert entry["complete"] is False
        assert state["launch_attempts"][key]["pending_cities"] == ["CityB"]

        # Ретрай предлагает только висящий город из меток — не CityF и прочих.
        retry = decide_launches({}, [_card("retry", ["CityA", "CityB", "PRODA"])])[0]
        assert retry["cities"] == ["CityB"]

    def test_после_успеха_всех_городов_меток_карточка_закрыта(self, clean_state):
        rec = self._rec(["CityA", "PRODA"])
        key = _prepare_launch_attempt(rec, _target_cities(rec))
        _record_city_success(key, "CityA", ["ad-citya"])
        assert _finalize_launch_attempt(key) == "SUCCEEDED"

        entry = _load_auto_launch_state()["launched_ever"]["retry"]
        assert entry["complete"] is True
        assert entry["target_cities"] == ["CityA"]
        # Полностью запущенная карточка больше не предлагается.
        assert decide_launches({}, [_card("retry", ["CityA", "PRODA"])]) == []


# ---------------------------------------------------------------------------
# run_auto_launch dry_run: метки доходят до чекера, отказ виден в blocked
# ---------------------------------------------------------------------------


class TestRunAutoLaunchLabels:
    @pytest.fixture
    def checker_requests(self, monkeypatch):
        requests: dict[str, object] = {}

        class RecordingChecker:
            def __init__(self, mode):
                self.mode = CheckerMode(mode)

            def prepare_and_reserve(self, card, request, state):
                requests[card["id"]] = request
                checked = check_candidate(card, request, state)
                if not checked.allowed:
                    raise LaunchCheckBlocked(
                        checked.reason_codes[0], checked.reasons, checked.check_id
                    )
                return SimpleNamespace(
                    check_id=checked.check_id,
                    card_id=card["id"],
                    request=request,
                    media={"type": "image", "paths": []},
                    media_sha256="a" * 64,
                    authorization=None,
                )

        monkeypatch.setattr(
            "services.auto_launch._get_launch_checker",
            lambda mode, **_kwargs: RecordingChecker(mode),
        )
        return requests

    def _run(self, cards):
        cfg = {
            "enabled": True,
            "kill_switch": False,
            "launch_enabled": True,
            "max_launches_per_day": 5,
        }
        with patch("services.auto_launch._get_autopilot_config", return_value=cfg), \
             patch("services.auto_launch._analyze_coverage", return_value={}), \
             patch("services.auto_launch._get_done_list_id", return_value="list1"), \
             patch("services.auto_launch._get_unlaunched_cards", return_value=cards), \
             patch("services.auto_launch._send_telegram"), \
             patch("agent.launcher.launch_single") as mock_launch:
            result = run_auto_launch(mode="dry_run", max_launches=3)
        mock_launch.assert_not_called()
        return result

    def test_метки_доходят_до_запроса_чекера(self, clean_state, checker_requests):
        result = self._run([LABELED_CARD, _card("all", ["Все города"]), _card("none", ["PRODA"])])
        cities = {r["card_id"]: r["cities"] for r in result["recommendations"]}
        assert cities == {"cta3": ["CityA"], "all": None, "none": None}
        assert checker_requests["cta3"].cities == ("CityA",)
        assert checker_requests["all"].cities is None
        assert checker_requests["none"].cities is None

    def test_выключенный_город_отказ_вместо_всех_городов(
        self, clean_state, checker_requests, monkeypatch
    ):
        _disable_city(monkeypatch, "CityF")
        result = self._run([_card("ctf", ["CityF", "PRODA"]), LABELED_CARD])
        assert [r["card_id"] for r in result["recommendations"]] == ["cta3"]
        assert "ctf" not in checker_requests
        assert [b["card_id"] for b in result["blocked"]] == ["ctf"]
        assert result["blocked"][0]["reason_codes"] == ["CITY_LABEL_UNROUTED"]


class TestCityFromCardName:
    """Решение владельца: без метки город в последнем сегменте названия равен метке."""

    def test_город_с_номером_в_конце_названия(self):
        from services.auto_launch import _city_from_card_name

        assert _city_from_card_name("Карточка Б / Тема / CityB 2") == ["CityB"]
        assert _city_from_card_name("Карточка / CityA") == ["CityA"]

    def test_нет_города_или_не_последний_сегмент(self):
        from services.auto_launch import _city_from_card_name

        assert _city_from_card_name("Тема 7 / черновик, заметки") is None
        assert _city_from_card_name("CityF карточка l2") is None  # без « / »
        assert _city_from_card_name("CityB / Карточка") is None  # город не в последнем сегменте
        assert _city_from_card_name("Карточка / CityA 3 вариант") is None  # не «город + номер»

    def test_рекомендация_берёт_город_из_названия_если_меток_нет(self):
        rec = _recommendation_from_card(_card("h", ["FB", "PRODA"], name="Карточка Б / Тема / CityB 2"))
        assert rec["cities"] == ["CityB"]
        # метка сильнее названия
        rec = _recommendation_from_card(_card("h2", ["CityC"], name="Карточка / CityB 2"))
        assert rec["cities"] == ["CityC"]

    def test_решение_по_карточке_тоже_видит_город_из_названия(self, clean_state):
        recs = decide_launches({}, [_card("h3", ["PRODA"], name="Карточка / CityE")])
        assert recs[0]["cities"] == ["CityE"]


class TestSkippedTargetsNarrowCities:
    def test_гейт_сужает_города_и_пишет_причину(self):
        from types import SimpleNamespace

        from services.auto_launch import _apply_skipped_targets

        rec = {"card_name": "Креатор А / Тема", "cities": None}
        plan = SimpleNamespace(
            targets=(SimpleNamespace(city="CityA"), SimpleNamespace(city="CityC")),
            skipped_targets=(("CityE", "DUPLICATE_LIVE", "CityE: найден частичный card identity"),),
        )
        _apply_skipped_targets(rec, plan)
        assert rec["cities"] == ["CityA", "CityC"]
        assert rec["skipped_cities"]["CityE"].startswith("уже запущен")
        assert _target_cities(rec) == ["CityA", "CityC"]

    def test_без_пропусков_ничего_не_меняет(self):
        from types import SimpleNamespace

        from services.auto_launch import _apply_skipped_targets

        rec = {"card_name": "x", "cities": None}
        _apply_skipped_targets(rec, SimpleNamespace(targets=(), skipped_targets=()))
        assert rec == {"card_name": "x", "cities": None}


class TestPausedAdsetSkip:
    """Правило владельца: выключенный адсет города не блокирует карточку — город пропускается."""

    def _rec(self):
        return {"campaign_type": "leadgen", "card_name": "Тест / карточка", "labels": ["CityB", "CityF", "CityE"]}

    def test_город_с_паузным_адсетом_пропускается_и_записывается(self, monkeypatch):
        from services import auto_launch

        monkeypatch.setattr(
            auto_launch, "_city_adset_statuses",
            lambda rec, cities: {"CityB": "ACTIVE", "CityF": "PAUSED", "CityE": "ACTIVE"},
        )
        rec = self._rec()
        assert _target_cities(rec) == ["CityB", "CityE"]
        assert rec["skipped_cities"] == {"CityF": "адсет выключен (PAUSED)"}

    def test_неизвестный_статус_и_город_без_адсета_остаются(self, monkeypatch):
        from services import auto_launch

        monkeypatch.setattr(auto_launch, "_city_adset_statuses", lambda rec, cities: {"CityB": "UNKNOWN"})
        assert _target_cities(self._rec()) == ["CityB", "CityE", "CityF"]  # порядок карты

    def test_fb_недоступен_прежнее_поведение(self, monkeypatch):
        from services import auto_launch

        def _boom(rec, cities):
            raise RuntimeError("FB down")

        monkeypatch.setattr(auto_launch, "_city_adset_statuses", _boom)
        rec = self._rec()
        assert _target_cities(rec) == ["CityB", "CityE", "CityF"]
        assert "skipped_cities" not in rec

    def test_все_адсеты_выключены_отказ(self, monkeypatch):
        from services import auto_launch

        monkeypatch.setattr(
            auto_launch, "_city_adset_statuses",
            lambda rec, cities: {city: "CAMPAIGN_PAUSED" for city in cities},
        )
        with pytest.raises(LaunchCheckBlocked) as info:
            _target_cities(self._rec())
        assert info.value.code == "ALL_ADSETS_PAUSED"
