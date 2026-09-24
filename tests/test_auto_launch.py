"""
Тесты модуля services/auto_launch.py.

Авто-запуск стал approval-first producer: в active-режиме он больше НЕ создаёт
объявления, а зовёт `agent.launcher.launch_single`, который создаёт владельцу
LAUNCH-proposal. Поэтому «запущенные» карточки прогона теперь лежат в
`result["proposals"]`, а `result["launched"]` заполняется только после реального
подтверждённого CREATE (уже за границей одобрения). Дневной кап и лизинг прогона
считают именно предложения — иначе один и тот же креатив ушёл бы владельцу
несколько раз.

Проверяем:
- decide_launches: приоритет пустых групп, исключение «бонус»
- run_auto_launch dry_run: ничего не предлагает (0 вызовов launch_single)
- run_auto_launch active при launch_enabled=False: не предлагает
- cap max_launches соблюдён
- уже запущенная карточка не предлагается дважды
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
from services.action_gateway_core import ActionResult

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта
import sys as _sys
if "google.genai" not in _sys.modules:
    _sys.modules["google.genai"] = MagicMock()
if "google" not in _sys.modules:
    _sys.modules["google"] = MagicMock()

from services.auto_launch import (
    decide_launches,
    run_auto_launch,
    _detect_campaign_type,
    _load_auto_launch_state,
    _save_auto_launch_state,
)
from services.approval_checker_models import ActionOrigin
from services.launch_checker import (
    CheckerMode,
    LaunchCheckBlocked,
    ProviderLaunchAuthorization,
    check_candidate,
)


# ---------------------------------------------------------------------------
# Вспомогательные фикстуры
# ---------------------------------------------------------------------------


def _proposal_launcher(recorder: list, *, side_effect=None):
    """Подмена agent.launcher.launch_single: возвращает созданный proposal.

    Реальный launch_single в approval-first только регистрирует предложение
    владельцу и заполняет status["proposal_state"], поэтому фейк делает то же.
    `side_effect(card_id)` позволяет тесту сымитировать блокировку или сбой.
    """

    def _launch(**kwargs):
        card_id = kwargs["card_id"]
        recorder.append(card_id)
        status = kwargs["status"]
        if side_effect is not None:
            outcome = side_effect(card_id)
            if outcome is not None:
                return outcome
        status["proposal_state"] = "PENDING_OWNER"
        status["outcome"] = "pending_owner"
        status["running"] = False
        return {"proposal_id": f"proposal-{card_id}"}

    return _launch


@pytest.fixture
def sample_coverage():
    """Покрытие с пустыми и тонкими группами."""
    return {
        "empty": [
            {"city": "CityD", "adset_type": "L2", "count": 0},
            {"city": "CityE", "adset_type": "L1", "count": 0},
        ],
        "thin": [
            {"city": "CityB", "adset_type": "L2", "count": 1},
        ],
        "ok_count": 3,
        "by_group": {},
        "generated_at": "2026-06-25T10:00:00+05:00",
    }


@pytest.fixture
def sample_coverage_empty():
    """Покрытие без пробелов."""
    return {
        "empty": [],
        "thin": [],
        "ok_count": 12,
        "by_group": {},
        "generated_at": "2026-06-25T10:00:00+05:00",
    }


@pytest.fixture
def sample_cards():
    """Незапущенные карточки Trello."""
    return [
        {
            "id": "card1",
            "name": "Петров / Тема А",
            "labels": ["PRODA"],
            "desc": "",
            "dueComplete": False,
            "pos": 1.0,
        },
        {
            "id": "card2",
            "name": "Сидорова / Заявка на PRODB",
            "labels": ["PRODB"],
            "desc": "",
            "dueComplete": False,
            "pos": 2.0,
        },
        {
            "id": "card3",
            "name": "Бонус / Бесплатный доступ",
            "labels": [],
            "desc": "",
            "dueComplete": False,
            "pos": 3.0,
        },
        {
            "id": "card4",
            "name": "Иванов / Тема В",
            "labels": [],
            "desc": "",
            "dueComplete": False,
            "pos": 4.0,
        },
    ]


@pytest.fixture
def clean_state(tmp_path, monkeypatch):
    """Чистый state-файл для каждого теста."""
    state_file = tmp_path / "auto_launch_state.json"
    monkeypatch.setattr(
        "services.auto_launch._AUTO_LAUNCH_STATE_FILE",
        state_file,
    )
    monkeypatch.setattr(
        "services.auto_launch._AUTO_LAUNCH_RUN_LOCK_FILE",
        tmp_path / "auto-launch-active.lock",
    )
    monkeypatch.setattr("services.auto_launch._mark_card_done", lambda _card_id: None)

    class OfflineChecker:
        def __init__(self, mode):
            self.mode = CheckerMode(mode)

        def prepare_and_reserve(self, card, request, state):
            checked = check_candidate(card, request, state)
            if not checked.allowed:
                raise LaunchCheckBlocked(
                    checked.reason_codes[0],
                    checked.reasons,
                    checked.check_id,
                )
            authorization = None
            if self.mode is CheckerMode.ENFORCE:
                authorization = ProviderLaunchAuthorization(
                    f"auth-{card['id']}",
                    "offline-test-secret",
                )
            return SimpleNamespace(
                check_id=checked.check_id,
                card_id=card["id"],
                request=request,
                media={"type": "image", "paths": []},
                media_sha256="a" * 64,
                authorization=authorization,
            )

    monkeypatch.setattr(
        "services.auto_launch._get_launch_checker",
        lambda mode, **_kwargs: OfflineChecker(mode),
    )
    return state_file


# ---------------------------------------------------------------------------
# Тесты decide_launches
# ---------------------------------------------------------------------------

class TestDecidesLaunches:
    def test_запускает_все_готовые_карточки(self, sample_coverage, sample_cards, clean_state):
        """Launch-all: возвращаются ВСЕ незапущенные карточки (кроме вето 'бонус'),
        независимо от пробелов покрытия."""
        recs = decide_launches(sample_coverage, sample_cards)

        # 4 карточки, из них card3 — «бонус» (вето) → 3 рекомендации
        assert len(recs) == 3
        for rec in recs:
            assert rec["cities"] is None

    def test_исключает_бонус(self, sample_coverage, sample_cards, clean_state):
        """Карточки с темой «бонус» не должны попадать в рекомендации."""
        recs = decide_launches(sample_coverage, sample_cards)

        card_names = [r["card_name"] for r in recs]
        assert not any("Бонус" in name or "бонус" in name.lower() for name in card_names)

    def test_пустой_список_при_нет_карточек(self, sample_coverage, clean_state):
        """Нет незапущенных карточек → пустые рекомендации."""
        recs = decide_launches(sample_coverage, [])
        assert recs == []

    def test_возвращает_карточки_даже_без_пробелов(self, sample_coverage_empty, sample_cards, clean_state):
        """Launch-all: покрытие больше НЕ гейтит запуск — даже при полном покрытии
        (empty=[], thin=[]) возвращаются все готовые карточки (кроме 'бонус')."""
        recs = decide_launches(sample_coverage_empty, sample_cards)
        assert len(recs) == 3
        card_ids = {r["card_id"] for r in recs}
        assert card_ids == {"card1", "card2", "card4"}

    def test_только_бонусные_карточки_пустые_рекомендации(self, sample_coverage, clean_state):
        """Если все карточки — «бонус», рекомендаций нет."""
        veto_cards = [
            {"id": "c1", "name": "Бонус / Акция", "labels": [], "desc": "", "dueComplete": False},
            {"id": "c2", "name": "бонус 2024 акция", "labels": [], "desc": "", "dueComplete": False},
        ]
        recs = decide_launches(sample_coverage, veto_cards)
        assert recs == []

    def test_одна_карточка_не_предлагается_дважды(self, sample_coverage, clean_state):
        """Одна карточка не должна повторяться в рекомендациях."""
        # Даём только одну карточку — пробелов несколько
        one_card = [
            {"id": "card1", "name": "Петров / Тема А", "labels": [], "desc": "", "dueComplete": False},
        ]
        recs = decide_launches(sample_coverage, one_card)

        card_ids = [r["card_id"] for r in recs]
        # card1 должна встречаться не более 1 раза
        assert card_ids.count("card1") <= 1

    def test_уже_запущенная_карточка_исключается(self, sample_coverage, sample_cards, clean_state):
        """Карточка, запущенная ранее через авто-запуск, не предлагается снова."""
        # Сохраняем card1 как уже запущенную
        state = {
            "launched_today": ["card1"],
            "launched_ever": {"card1": "2026-06-24T10:00:00+05:00"},
            "last_launch_date": "2026-06-24",
        }
        _save_auto_launch_state(state)

        recs = decide_launches(sample_coverage, sample_cards)
        card_ids = [r["card_id"] for r in recs]
        assert "card1" not in card_ids

    def test_причина_запуск_готовой_карточки(self, sample_coverage, sample_cards, clean_state):
        """Причина рекомендации отражает launch-all, а не закрытие пробела."""
        recs = decide_launches(sample_coverage, sample_cards)
        assert len(recs) > 0
        for rec in recs:
            assert rec["reason"] == "запуск готовой карточки"


# ---------------------------------------------------------------------------
# Тесты campaign_type по меткам
# ---------------------------------------------------------------------------

class TestDetectCampaignType:
    def test_proda_метка(self):
        assert _detect_campaign_type(["PRODA"]) == "leadgen"

    def test_prodb_метка(self):
        assert _detect_campaign_type(["PRODB"]) == "leadgen_prodb"

    def test_нет_метки_дефолт(self):
        assert _detect_campaign_type([]) == "leadgen"

    def test_нижний_регистр(self):
        assert _detect_campaign_type(["proda"]) == "leadgen"

    def test_prodb_нижний(self):
        assert _detect_campaign_type(["prodb"]) == "leadgen_prodb"


# ---------------------------------------------------------------------------
# Тесты run_auto_launch dry_run
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_не_вызывает_launch_single(self, sample_coverage, sample_cards, clean_state):
        """В dry_run launch_single НЕ должен вызываться (0 вызовов)."""
        with patch("services.auto_launch._get_autopilot_config", return_value={
            "enabled": True,
            "kill_switch": False,
            "launch_enabled": True,
            "max_launches_per_day": 5,
        }), patch("services.auto_launch._analyze_coverage", return_value=sample_coverage), \
           patch("services.auto_launch._get_done_list_id", return_value="list1"), \
           patch("services.auto_launch._get_unlaunched_cards", return_value=sample_cards), \
           patch("services.auto_launch._send_telegram"), \
           patch("agent.launcher.launch_single") as mock_launch:

            result = run_auto_launch(mode="dry_run", max_launches=3)

        # launch_single не вызывался
        mock_launch.assert_not_called()
        # Рекомендации есть
        assert len(result["recommendations"]) > 0
        # Launched пусто
        assert result["launched"] == []


class TestFreshTopCheckerQueue:
    def _config(self, checker_mode="enforce"):
        return {
            "enabled": True,
            "kill_switch": False,
            "launch_enabled": True,
            "max_launches_per_day": 5,
            "launch_checker": {"mode": checker_mode},
        }

    def test_raw_cards_sorted_by_pos_id_and_legacy_veto_are_visible_blocks(
        self,
        clean_state,
    ):
        _save_auto_launch_state(
            {
                "schema_version": 2,
                "launched_today": [],
                "launched_ever": {"legacy": "2026-07-01T10:00:00+05:00"},
                "launch_attempts": {},
                "last_launch_date": None,
            }
        )
        cards = [
            {"id": "fresh-b", "name": "B", "labels": ["PRODA"], "pos": 30.0},
            {"id": "veto", "name": "Про бонус", "labels": ["PRODA"], "pos": 10.0},
            {"id": "fresh-a", "name": "A", "labels": ["PRODA"], "pos": 30.0},
            {"id": "legacy", "name": "Legacy", "labels": ["PRODA"], "pos": 20.0},
            {"id": "fresh-c", "name": "C", "labels": ["PRODA"], "pos": 40.0},
        ]
        proposed: list[str] = []
        origins: list[object] = []

        def _capture_origin(**kwargs):
            origins.append(kwargs.get("origin"))
            return None

        launch = _proposal_launcher(proposed)

        def launch_and_capture(**kwargs):
            _capture_origin(**kwargs)
            return launch(**kwargs)

        with patch(
            "services.auto_launch._get_autopilot_config",
            return_value=self._config(),
        ), patch("services.auto_launch._analyze_coverage", return_value={}), patch(
            "services.auto_launch._get_done_list_id", return_value="list1"
        ), patch(
            "services.auto_launch._get_unlaunched_cards", return_value=cards
        ), patch("services.auto_launch._send_telegram"), patch(
            "agent.launcher.launch_single", side_effect=launch_and_capture
        ):
            result = run_auto_launch(
                mode="active",
                max_launches=2,
                source="AUTO_LAUNCH_NOW",
            )

        # Порядок очереди: pos, затем id; veto и legacy видны как блоки
        assert proposed == ["fresh-a", "fresh-b"]
        assert [item["card_id"] for item in result["proposals"]] == ["fresh-a", "fresh-b"]
        # Origin закрытого списка: без него launcher fail-closed
        assert {origin for origin in origins} == {ActionOrigin.AUTO_LAUNCH}
        assert {item["reason_codes"][0] for item in result["blocked"]} >= {"TOPIC_VETO"}
        assert result["raw_count"] == 5
        assert result["eligible_count"] == 3
        assert result["blocked_count"] == 2
        assert {
            item["reason_codes"][0] for item in result["blocked"]
        } == {"TOPIC_VETO", "UNKNOWN_LEGACY"}

    def test_typed_block_does_not_consume_slot_and_is_not_false_success(
        self,
        clean_state,
    ):
        cards = [
            {"id": f"card-{index}", "name": f"Card {index}", "labels": [], "pos": index}
            for index in range(1, 5)
        ]
        calls: list[str] = []

        def blocked_first(card_id):
            if card_id == "card-1":
                raise LaunchCheckBlocked("CAPACITY_BLOCKED", ("CityA: 50/50",), "check-1")
            return None

        with patch(
            "services.auto_launch._get_autopilot_config",
            return_value=self._config(),
        ), patch("services.auto_launch._analyze_coverage", return_value={}), patch(
            "services.auto_launch._get_done_list_id", return_value="list1"
        ), patch(
            "services.auto_launch._get_unlaunched_cards", return_value=cards
        ), patch("services.auto_launch._send_telegram"), patch(
            "agent.launcher.launch_single",
            side_effect=_proposal_launcher(calls, side_effect=blocked_first),
        ):
            result = run_auto_launch(mode="active", max_launches=2)

        # Typed-блок не съедает слот: очередь доходит до следующих кандидатов
        assert calls == ["card-1", "card-2", "card-3"]
        assert [item["card_id"] for item in result["proposals"]] == ["card-2", "card-3"]
        assert result["blocked"][-1]["reason_codes"] == ["CAPACITY_BLOCKED"]
        assert result["raw_count"] == result["eligible_count"] + result["blocked_count"]
        # Блокировка не превращается в ложный успех
        assert all(item["card_id"] != "card-1" for item in result["proposals"])
        assert result["launched"] == []

    def test_durable_partial_consumes_daily_slot_before_next_candidate(
        self,
        clean_state,
    ):
        cards = [
            {"id": "partial", "name": "Partial", "labels": [], "pos": 1},
            {"id": "next", "name": "Next", "labels": [], "pos": 2},
        ]
        from services.auto_launch import _get_today_str

        calls: list[str] = []
        today = _get_today_str()

        def partial_then_fail(card_id):
            # Имитируем сбой ПОСЛЕ того, как слот уже стал durable
            state = _load_auto_launch_state()
            state["last_launch_date"] = today
            state["launched_today"] = [card_id]
            _save_auto_launch_state(state)
            raise RuntimeError("mid-card exception after durable slot")

        config = {**self._config(), "max_launches_per_day": 1}
        with patch(
            "services.auto_launch._get_autopilot_config",
            return_value=config,
        ), patch("services.auto_launch._analyze_coverage", return_value={}), patch(
            "services.auto_launch._get_done_list_id", return_value="list1"
        ), patch(
            "services.auto_launch._get_unlaunched_cards", return_value=cards
        ), patch("services.auto_launch._send_telegram"), patch(
            "agent.launcher.launch_single",
            side_effect=_proposal_launcher(calls, side_effect=partial_then_fail),
        ):
            result = run_auto_launch(mode="active", max_launches=5)

        # Занятый слот не отдаётся следующему кандидату даже после сбоя
        assert calls == ["partial"]
        assert result["proposals"] == []
        assert result["launched"] == []
        assert "1/1" in result["skipped_reason"]

    def test_active_observe_reports_would_allow_but_never_executes(self, clean_state):
        cards = [{"id": "fresh", "name": "Fresh", "labels": [], "pos": 1.0}]
        with patch(
            "services.auto_launch._get_autopilot_config",
            return_value=self._config("observe"),
        ), patch("services.auto_launch._analyze_coverage", return_value={}), patch(
            "services.auto_launch._get_done_list_id", return_value="list1"
        ), patch(
            "services.auto_launch._get_unlaunched_cards", return_value=cards
        ), patch("services.auto_launch._send_telegram"), patch(
            "services.auto_launch._execute_launch"
        ) as execute:
            result = run_auto_launch(mode="active", max_launches=1)

        execute.assert_not_called()
        assert result["eligible_count"] == 1
        assert result["blocked_count"] == 0
        assert "observe" in result["skipped_reason"]

    def test_dry_run_не_вызывает_launch_single_даже_при_enabled(self, sample_coverage, sample_cards, clean_state):
        """dry_run не запускает даже если enabled=True и launch_enabled=True."""
        with patch("services.auto_launch._get_autopilot_config", return_value={
            "enabled": True,
            "kill_switch": False,
            "launch_enabled": True,
            "max_launches_per_day": 5,
        }), patch("services.auto_launch._analyze_coverage", return_value=sample_coverage), \
           patch("services.auto_launch._get_done_list_id", return_value="list1"), \
           patch("services.auto_launch._get_unlaunched_cards", return_value=sample_cards), \
           patch("services.auto_launch._send_telegram"), \
           patch("services.auto_launch._execute_launch") as mock_exec:

            result = run_auto_launch(mode="dry_run", max_launches=3)

        mock_exec.assert_not_called()
        assert result["launched"] == []


# ---------------------------------------------------------------------------
# Тесты run_auto_launch active с предохранителями
# ---------------------------------------------------------------------------

class TestActiveLaunch:
    def test_active_при_launch_enabled_false_не_запускает(self, sample_coverage, sample_cards, clean_state):
        """active режим при launch_enabled=False → skipped_reason, нет запусков."""
        with patch("services.auto_launch._get_autopilot_config", return_value={
            "enabled": True,
            "kill_switch": False,
            "launch_enabled": False,  # ВЫКЛЮЧЕНО
            "max_launches_per_day": 1,
            "launch_checker": {"mode": "enforce"},
        }), patch("services.auto_launch._analyze_coverage", return_value=sample_coverage), \
           patch("services.auto_launch._get_done_list_id", return_value="list1"), \
           patch("services.auto_launch._get_unlaunched_cards", return_value=sample_cards), \
           patch("services.auto_launch._send_telegram"), \
           patch("services.auto_launch._execute_launch") as mock_exec:

            result = run_auto_launch(mode="active", max_launches=1)

        mock_exec.assert_not_called()
        assert result["launched"] == []
        assert result["skipped_reason"] is not None
        assert "launch_enabled" in result["skipped_reason"]

    def test_active_при_enabled_false_не_запускает(self, sample_coverage, sample_cards, clean_state):
        """active режим при enabled=False → skipped."""
        with patch("services.auto_launch._get_autopilot_config", return_value={
            "enabled": False,  # ВЫКЛЮЧЕНО
            "kill_switch": False,
            "launch_enabled": True,
            "max_launches_per_day": 1,
            "launch_checker": {"mode": "enforce"},
        }), patch("services.auto_launch._analyze_coverage", return_value=sample_coverage), \
           patch("services.auto_launch._get_done_list_id", return_value="list1"), \
           patch("services.auto_launch._get_unlaunched_cards", return_value=sample_cards), \
           patch("services.auto_launch._send_telegram"), \
           patch("services.auto_launch._execute_launch") as mock_exec:

            result = run_auto_launch(mode="active", max_launches=1)

        mock_exec.assert_not_called()
        assert result["launched"] == []
        assert "enabled" in result["skipped_reason"]

    def test_active_при_kill_switch_не_запускает(self, sample_coverage, sample_cards, clean_state):
        """kill_switch=True блокирует запуск."""
        with patch("services.auto_launch._get_autopilot_config", return_value={
            "enabled": True,
            "kill_switch": True,  # АВАРИЙНАЯ ОСТАНОВКА
            "launch_enabled": True,
            "max_launches_per_day": 1,
            "launch_checker": {"mode": "enforce"},
        }), patch("services.auto_launch._analyze_coverage", return_value=sample_coverage), \
           patch("services.auto_launch._get_done_list_id", return_value="list1"), \
           patch("services.auto_launch._get_unlaunched_cards", return_value=sample_cards), \
           patch("services.auto_launch._send_telegram"), \
           patch("services.auto_launch._execute_launch") as mock_exec:

            result = run_auto_launch(mode="active", max_launches=1)

        mock_exec.assert_not_called()
        assert "kill_switch" in result["skipped_reason"]

    def test_cap_max_launches_соблюдён(self, sample_coverage, sample_cards, clean_state):
        """Запускается не больше max_launches карточек."""
        with patch("services.auto_launch._get_autopilot_config", return_value={
            "enabled": True,
            "kill_switch": False,
            "launch_enabled": True,
            "max_launches_per_day": 10,  # большой лимит в конфиге
            "launch_checker": {"mode": "enforce"},
        }), patch("services.auto_launch._analyze_coverage", return_value=sample_coverage), \
           patch("services.auto_launch._get_done_list_id", return_value="list1"), \
           patch("services.auto_launch._get_unlaunched_cards", return_value=sample_cards), \
           patch("services.auto_launch._send_telegram"), \
           patch("services.auto_launch._execute_launch") as mock_exec:

            # Просим max_launches=1
            run_auto_launch(mode="active", max_launches=1)

        # Не более 1 запуска
        assert mock_exec.call_count <= 1

    def test_дневной_лимит_из_конфига_соблюдён(self, sample_coverage, sample_cards, clean_state):
        """Конфиговый max_launches_per_day=1 ограничивает запуски."""
        with patch("services.auto_launch._get_autopilot_config", return_value={
            "enabled": True,
            "kill_switch": False,
            "launch_enabled": True,
            "max_launches_per_day": 1,  # лимит 1
            "launch_checker": {"mode": "enforce"},
        }), patch("services.auto_launch._analyze_coverage", return_value=sample_coverage), \
           patch("services.auto_launch._get_done_list_id", return_value="list1"), \
           patch("services.auto_launch._get_unlaunched_cards", return_value=sample_cards), \
           patch("services.auto_launch._send_telegram"), \
           patch("services.auto_launch._execute_launch") as mock_exec:

            run_auto_launch(mode="active", max_launches=5)  # просим 5, но конфиг=1

        # Не более 1
        assert mock_exec.call_count <= 1

    def test_дневной_лимит_исчерпан_сегодня(self, sample_coverage, sample_cards, clean_state):
        """Если сегодня уже использован лимит — пропускаем."""
        from services.auto_launch import _get_today_str

        today = _get_today_str()
        state = {
            "launched_today": ["card_prev"],
            "launched_ever": {"card_prev": "2026-06-25T08:00:00+05:00"},
            "last_launch_date": today,
        }
        _save_auto_launch_state(state)

        with patch("services.auto_launch._get_autopilot_config", return_value={
            "enabled": True,
            "kill_switch": False,
            "launch_enabled": True,
            "max_launches_per_day": 1,  # лимит 1, уже 1 был
            "launch_checker": {"mode": "enforce"},
        }), patch("services.auto_launch._analyze_coverage", return_value=sample_coverage), \
           patch("services.auto_launch._get_done_list_id", return_value="list1"), \
           patch("services.auto_launch._get_unlaunched_cards", return_value=sample_cards), \
           patch("services.auto_launch._send_telegram"), \
           patch("services.auto_launch._execute_launch") as mock_exec:

            result = run_auto_launch(mode="active", max_launches=1)

        mock_exec.assert_not_called()
        assert result["skipped_reason"] is not None
        assert "лимит" in result["skipped_reason"]

    def test_active_запускает_при_всех_флагах_ок(self, sample_coverage, sample_cards, clean_state):
        """При всех флагах OK — создаётся LAUNCH-предложение владельцу."""
        proposed: list[str] = []
        with patch("services.auto_launch._get_autopilot_config", return_value={
            "enabled": True,
            "kill_switch": False,
            "launch_enabled": True,
            "max_launches_per_day": 3,
            "launch_checker": {"mode": "enforce"},
        }), patch("services.auto_launch._analyze_coverage", return_value=sample_coverage), \
           patch("services.auto_launch._get_done_list_id", return_value="list1"), \
           patch("services.auto_launch._get_unlaunched_cards", return_value=sample_cards), \
           patch("services.auto_launch._send_telegram"), \
           patch("agent.launcher.launch_single", side_effect=_proposal_launcher(proposed)):

            result = run_auto_launch(mode="active", max_launches=1)

        # Владельцу отправлено предложение; объявления не созданы
        assert len(proposed) >= 1
        assert len(result["proposals"]) >= 1
        assert result["launched"] == []


# ---------------------------------------------------------------------------
# Тест: нет пробелов → нет рекомендаций
# ---------------------------------------------------------------------------

class TestNoCoverage:
    def test_нет_пробелов_всё_равно_запускает_готовые(
        self, sample_coverage_empty, sample_cards, clean_state
    ):
        """Launch-all: даже при полном покрытии готовые карточки предлагаются
        (покрытие больше не гейтит запуск). Темп ограничен дневным капом."""
        proposed: list[str] = []
        with patch("services.auto_launch._get_autopilot_config", return_value={
            "enabled": True,
            "kill_switch": False,
            "launch_enabled": True,
            "max_launches_per_day": 3,
            "launch_checker": {"mode": "enforce"},
        }), patch("services.auto_launch._analyze_coverage", return_value=sample_coverage_empty), \
           patch("services.auto_launch._get_done_list_id", return_value="list1"), \
           patch("services.auto_launch._get_unlaunched_cards", return_value=sample_cards), \
           patch("services.auto_launch._send_telegram"), \
           patch("agent.launcher.launch_single", side_effect=_proposal_launcher(proposed)):

            result = run_auto_launch(mode="active", max_launches=3)

        # 3 не-бонус карточки, кап=3 → все 3 предложены владельцу (покрытие не мешает)
        assert len(proposed) == 3
        assert len(result["recommendations"]) == 3
        assert len(result["proposals"]) == 3
        assert result["launched"] == []


# ---------------------------------------------------------------------------
# Тесты ФИКС 1: матчинг campaign_type к пробелу
# ---------------------------------------------------------------------------

class TestCampaignTypeMatching:
    def test_prodb_карточка_теперь_запускается(self, clean_state):
        """Launch-all: PRODB-карточка больше НЕ фильтруется по типу пробела — запускается."""
        coverage = {
            "empty": [{"city": "CityA", "adset_type": "L2", "count": 0}],
            "thin": [],
            "ok_count": 0,
            "by_group": {},
            "generated_at": "2026-06-25T10:00:00+05:00",
        }
        prodb_only_cards = [
            {
                "id": "prodb_card",
                "name": "Заявка на PRODB",
                "labels": ["PRODB"],
                "desc": "",
                "dueComplete": False,
            }
        ]
        recs = decide_launches(coverage, prodb_only_cards)
        assert len(recs) == 1
        assert recs[0]["card_id"] == "prodb_card"
        assert recs[0]["cities"] is None

    def test_карточка_запускается_независимо_от_покрытия(self, clean_state):
        """Launch-all: карточка запускается независимо от типа пробела покрытия."""
        coverage = {
            "empty": [{"city": "Онлайн", "adset_type": "L2", "count": 0}],
            "thin": [],
            "ok_count": 0,
            "by_group": {},
            "generated_at": "2026-06-25T10:00:00+05:00",
        }
        offline_cards = [
            {
                "id": "card1",
                "name": "Петров / PRODA карточка",
                "labels": ["PRODA"],
                "desc": "",
                "dueComplete": False,
            }
        ]
        recs = decide_launches(coverage, offline_cards)
        assert len(recs) == 1
        assert recs[0]["card_id"] == "card1"

    def test_proda_карточка_назначается_в_обычный_пробел(self, clean_state):
        """leadgen (PRODA) карточка правильно идёт в обычный пробел города."""
        coverage = {
            "empty": [{"city": "CityD", "adset_type": "L2", "count": 0}],
            "thin": [],
            "ok_count": 0,
            "by_group": {},
            "generated_at": "2026-06-25T10:00:00+05:00",
        }
        proda_card = [
            {
                "id": "proda_card",
                "name": "Петров / PRODA",
                "labels": ["PRODA"],
                "desc": "",
                "dueComplete": False,
            }
        ]
        recs = decide_launches(coverage, proda_card)
        assert len(recs) == 1
        assert recs[0]["card_id"] == "proda_card"
        assert recs[0]["campaign_type"] == "leadgen"
        assert recs[0]["cities"] is None

    def test_онлайн_пробел_с_онлайн_карточкой_назначается(self, clean_state):
        """mql_online карточка правильно идёт в Онлайн-пробел."""
        # mql_online через _detect_campaign_type вернёт leadgen (нет маппинга),
        # поэтому онлайн-карточку надо явно закодить — проверяем логику _campaign_type_fits_gap
        from services.auto_launch import _campaign_type_fits_gap
        assert _campaign_type_fits_gap("mql_online", "Онлайн") is True
        assert _campaign_type_fits_gap("prodb_online", "Онлайн") is True
        assert _campaign_type_fits_gap("leadgen", "Онлайн") is False
        assert _campaign_type_fits_gap("leadgen_prodb", "Онлайн") is False

    def test_смешанные_пробелы_правильный_матчинг(self, clean_state):
        """Обычный пробел берёт leadgen, Онлайн-пробел без подходящих карточек пропускается."""
        coverage = {
            "empty": [
                {"city": "CityB", "adset_type": "L2", "count": 0},
                {"city": "Онлайн", "adset_type": "L2", "count": 0},
            ],
            "thin": [],
            "ok_count": 0,
            "by_group": {},
            "generated_at": "2026-06-25T10:00:00+05:00",
        }
        cards = [
            {
                "id": "proda1",
                "name": "PRODA карточка 1",
                "labels": ["PRODA"],
                "desc": "",
                "dueComplete": False,
            },
            {
                "id": "proda2",
                "name": "PRODA карточка 2",
                "labels": ["PRODA"],
                "desc": "",
                "dueComplete": False,
            },
        ]
        recs = decide_launches(coverage, cards)
        # Launch-all: обе карточки запускаются, пробелы больше не матчатся
        assert len(recs) == 2
        for rec in recs:
            assert rec["cities"] is None
            assert rec["campaign_type"] == "leadgen"


# ---------------------------------------------------------------------------
# Durable attempts: launched_ever только после подтверждённых ad_id
# ---------------------------------------------------------------------------

class TestDurableLaunchTracking:
    def test_до_create_есть_attempt_но_нет_launched_ever(self, clean_state):
        """PREPARED не выдаётся за успешный запуск карточки."""
        from services.auto_launch import _execute_launch, _load_auto_launch_state

        saved_states = []

        def mock_launch_single(**kwargs):
            # В момент вызова launch_single — читаем state с диска
            state_on_disk = _load_auto_launch_state()
            saved_states.append(state_on_disk.copy())

        state = {
            "launched_today": [],
            "launched_ever": {},
            "last_launch_date": None,
        }
        rec = {
            "card_id": "card_test",
            "card_name": "Тест карточка",
            "campaign_type": "leadgen",
            "cities": ["CityA"],
        }

        with patch("agent.launcher.launch_single", side_effect=mock_launch_single), \
             pytest.raises(RuntimeError, match="Частичный запуск"):
            _execute_launch(rec, state, "2026-06-25")

        # До CREATE сохраняется только durable attempt.
        assert len(saved_states) == 1
        assert "card_test" not in saved_states[0]["launched_ever"]
        attempts = list(saved_states[0]["launch_attempts"].values())
        assert len(attempts) == 1
        assert attempts[0]["phase"] == "PREPARED"

    def test_полный_сбой_не_пишет_launched_ever_и_retryable(self, clean_state):
        """Ноль ad_id оставляет карточку доступной следующему прогону."""
        from services.auto_launch import _execute_launch, _load_auto_launch_state

        state = {
            "launched_today": [],
            "launched_ever": {},
            "last_launch_date": None,
        }
        rec = {
            "card_id": "card_fail",
            "card_name": "Проблемная карточка",
            "campaign_type": "leadgen",
            "cities": ["CityA"],
        }

        # launch_single падает с ошибкой (симулируем сбой на Trello)
        with patch("agent.launcher.launch_single", side_effect=RuntimeError("Trello упал")):
            try:
                _execute_launch(rec, state, "2026-06-25")
            except Exception:
                pass  # ожидаем ошибку

        # Полный сбой не превращается в вечный «успех».
        state_after = _load_auto_launch_state()
        assert "card_fail" not in state_after["launched_ever"]
        recs = decide_launches({}, [{
            "id": "card_fail",
            "name": "Проблемная карточка",
            "labels": ["PRODA"],
        }])
        assert [item["card_id"] for item in recs] == ["card_fail"]


class TestPartialCityRetryAndReconciliation:
    def _rec(self):
        return {
            "card_id": "card_partial",
            "card_name": "Частичная карточка",
            "campaign_type": "leadgen",
            "cities": ["CityA", "CityB"],
            "labels": ["PRODA"],
        }

    def test_partial_повторяет_только_missing_city(self, clean_state):
        from services.auto_launch import (
            _execute_launch,
            _finalize_launch_attempt,
            _load_auto_launch_state,
            _prepare_launch_attempt,
            _record_city_failure,
            _record_city_success,
        )

        rec = self._rec()
        key = _prepare_launch_attempt(rec, rec["cities"])
        _record_city_success(key, "CityA", ["ad-citya"])
        _record_city_failure(key, "CityB", "adset full")
        assert _finalize_launch_attempt(key) == "PARTIAL"

        cards = [{"id": rec["card_id"], "name": rec["card_name"], "labels": ["PRODA"]}]
        retry_rec = decide_launches({}, cards)[0]
        assert retry_rec["cities"] == ["CityB"]

        def launch_missing(**kwargs):
            assert kwargs["cities"] == ["CityB"]
            kwargs["status"]["log"].append("✅ CityB: ad-cityb")

        state = _load_auto_launch_state()
        with patch("agent.launcher.launch_single", side_effect=launch_missing), \
             patch("services.auto_launch._get_autopilot_config", return_value={}), \
             patch("services.auto_launch._record_hypothesis"):
            _execute_launch(retry_rec, state, "2026-07-20")

        entry = _load_auto_launch_state()["launched_ever"][rec["card_id"]]
        assert entry["complete"] is True
        assert entry["ads_by_city"] == {
            "CityA": ["ad-citya"],
            "CityB": ["ad-cityb"],
        }

    def test_crash_exact_match_восстанавливает_id_без_create(self, clean_state):
        from services.auto_launch import (
            _execute_launch,
            _load_auto_launch_state,
            _prepare_city_launch,
            _prepare_launch_attempt,
            _reconcile_launch_attempt,
        )

        rec = {**self._rec(), "cities": ["CityA"]}
        key = _prepare_launch_attempt(rec, ["CityA"])
        _prepare_city_launch(key, "CityA", "adset-1", ["CityA | Часть [PRODA]"], 1)
        created_time = _load_auto_launch_state()["launch_attempts"][key]["started_at"]
        ads = [{
            "id": "ad-recovered",
            "name": "CityA | Часть [PRODA]",
            "adset_id": "adset-1",
            "created_time": created_time,
        }]
        with patch("services.auto_launch._fetch_reconciliation_ads", return_value=ads):
            assert _reconcile_launch_attempt(key) == "SUCCEEDED"

        state = _load_auto_launch_state()
        with patch("agent.launcher.launch_single") as mock_launch:
            _execute_launch(rec, state, "2026-07-20")
        mock_launch.assert_not_called()
        assert state["launched_ever"][rec["card_id"]]["ad_ids"] == ["ad-recovered"]

    def test_crash_after_provider_create_and_callback_failure_zero_blocks_retry(
        self,
        clean_state,
    ):
        """Zero inventory после возможного CREATE не разрешает второй CREATE."""
        from services.auto_launch import (
            _execute_launch,
            _load_auto_launch_state,
            _mark_city_create_started,
            _prepare_city_launch,
        )

        rec = {**self._rec(), "cities": ["CityA"]}
        expected_name = "CityA | Частичная карточка [PRODA]"
        provider_calls: list[str] = []

        def gateway_unknown_after_create(**_kwargs):
            attempt_key = next(iter(_load_auto_launch_state()["launch_attempts"]))
            _prepare_city_launch(
                attempt_key,
                "CityA",
                "adset-1",
                [expected_name],
                1,
                media_manifest_sha256="a" * 64,
            )
            _mark_city_create_started(attempt_key, "CityA", "a" * 64)
            provider_calls.append("CREATE")
            raise RuntimeError("gateway result UNKNOWN")

        state = _load_auto_launch_state()
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "account-1"),
        ), patch(
            "services.auto_launch._get_autopilot_config",
            return_value={"replacement": {"enabled": False}},
        ), patch(
            "agent.launcher.launch_single",
            side_effect=gateway_unknown_after_create,
        ), pytest.raises(RuntimeError, match="gateway result UNKNOWN"):
            _execute_launch(rec, state, "2026-07-21")

        second_launch = MagicMock()
        state = _load_auto_launch_state()
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "account-1"),
        ), patch(
            "services.auto_launch._fetch_reconciliation_ads",
            return_value=[],
        ), patch("services.auto_launch._send_telegram"), patch(
            "agent.launcher.launch_single",
            second_launch,
        ), pytest.raises(RuntimeError, match="reconciliation"):
            _execute_launch(rec, state, "2026-07-21")

        assert provider_calls == ["CREATE"]
        second_launch.assert_not_called()
        attempt = next(iter(_load_auto_launch_state()["launch_attempts"].values()))
        assert attempt["phase"] == "BLOCKED"

    def test_zero_before_provider_create_remains_controlled_retryable(self, clean_state):
        """Capacity/template reject после plan, но до CREATE_STARTED можно повторить."""
        from services.auto_launch import (
            _prepare_city_launch,
            _prepare_launch_attempt,
            _reconcile_launch_attempt,
        )

        rec = {**self._rec(), "cities": ["CityA"]}
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "account-1"),
        ):
            key = _prepare_launch_attempt(rec, ["CityA"])
        _prepare_city_launch(
            key,
            "CityA",
            "adset-1",
            ["CityA | Креатив"],
            1,
            media_manifest_sha256="a" * 64,
        )

        with patch("services.auto_launch._fetch_reconciliation_ads", return_value=[]):
            assert _reconcile_launch_attempt(key) == "FAILED_RETRYABLE"

        attempt = _load_auto_launch_state()["launch_attempts"][key]
        assert attempt["phase"] == "FAILED_RETRYABLE"
        assert "create_started_at" not in attempt["city_plan"]["CityA"]

    def test_reconcile_partial_multi_asset_блокирует_retry(self, clean_state):
        from services.auto_launch import (
            _load_auto_launch_state,
            _prepare_city_launch,
            _prepare_launch_attempt,
            _reconcile_launch_attempt,
        )

        rec = {**self._rec(), "cities": ["CityA"]}
        key = _prepare_launch_attempt(rec, ["CityA"])
        names = ["CityA | Креатив / feed [PRODA]", "CityA | Креатив / story [PRODA]"]
        _prepare_city_launch(key, "CityA", "adset-1", names, 2)
        started_at = _load_auto_launch_state()["launch_attempts"][key]["started_at"]
        one_of_two = [{
            "id": "ad-one",
            "name": names[0],
            "adset_id": "adset-1",
            "created_time": started_at,
        }]
        with patch("services.auto_launch._fetch_reconciliation_ads", return_value=one_of_two), \
             patch("services.auto_launch._send_telegram"):
            assert _reconcile_launch_attempt(key) == "BLOCKED"

    def test_late_approved_retry_extends_window_and_crash_recovers_exact_id(
        self,
        clean_state,
    ):
        """Exact manual retry сохраняет from и расширяет until для reconcile."""
        from datetime import datetime, timedelta
        from services.auto_launch import (
            _load_auto_launch_state,
            _prepare_city_launch,
            _prepare_launch_attempt,
            _reconcile_launch_attempt,
        )

        rec = {**self._rec(), "cities": ["CityA"]}
        manifest_hash = "a" * 64
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "account-1"),
        ):
            key = _prepare_launch_attempt(rec, ["CityA"])
        names = ["CityA | Креатив"]
        _prepare_city_launch(
            key,
            "CityA",
            "adset-1",
            names,
            1,
            media_manifest_sha256=manifest_hash,
        )
        state = _load_auto_launch_state()
        attempt = state["launch_attempts"][key]
        original_from = attempt["city_plan"]["CityA"]["reconcile_from"]
        original_until = datetime.fromisoformat(
            attempt["city_plan"]["CityA"]["reconcile_until"]
        )
        retry_started = original_until + timedelta(hours=3)
        attempt["phase"] = "PREPARED"
        _save_auto_launch_state(state)

        # Позднее одобрение — это ход настоящих часов, а не started_at:
        # окно строится от текущего времени (started_at в бою не обновляется).
        class _RetryClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return retry_started if tz is None else retry_started.astimezone(tz)

        with patch("services.auto_launch.datetime", _RetryClock):
            _prepare_city_launch(
                key,
                "CityA",
                "adset-1",
                names,
                1,
                media_manifest_sha256=manifest_hash,
            )
        plan = _load_auto_launch_state()["launch_attempts"][key]["city_plan"]["CityA"]
        assert plan["reconcile_from"] == original_from
        assert datetime.fromisoformat(plan["reconcile_until"]) == retry_started + timedelta(
            hours=2
        )

        ads = [{
            "id": "ad-late-recovered",
            "name": names[0],
            "adset_id": "adset-1",
            "created_time": retry_started.isoformat(),
        }]
        with patch("services.auto_launch._fetch_reconciliation_ads", return_value=ads):
            assert _reconcile_launch_attempt(key) == "SUCCEEDED"

        assert _load_auto_launch_state()["launch_attempts"][key]["ads_by_city"] == {
            "CityA": ["ad-late-recovered"]
        }

    def test_existing_city_plan_rejects_changed_media_hash(self, clean_state):
        """Связанный plan нельзя переписать теми же names с другими bytes."""
        from services.auto_launch import _prepare_city_launch, _prepare_launch_attempt

        rec = {**self._rec(), "cities": ["CityA"]}
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "account-1"),
        ):
            key = _prepare_launch_attempt(rec, ["CityA"])
        names = ["CityA | Креатив"]
        _prepare_city_launch(
            key,
            "CityA",
            "adset-1",
            names,
            1,
            media_manifest_sha256="a" * 64,
        )

        with pytest.raises(RuntimeError, match="city plan mismatch"):
            _prepare_city_launch(
                key,
                "CityA",
                "adset-1",
                names,
                1,
                media_manifest_sha256="b" * 64,
            )

        attempt = _load_auto_launch_state()["launch_attempts"][key]
        assert attempt["phase"] == "BLOCKED"
        assert attempt["city_plan"]["CityA"]["media_manifest_sha256"] == "a" * 64

    def test_online_crash_reconciles_в_том_же_кабинете_без_create(self, clean_state):
        from services.auto_launch import (
            _execute_launch,
            _load_auto_launch_state,
            _prepare_city_launch,
            _prepare_launch_attempt,
        )

        rec = {
            "card_id": "card-online-crash",
            "card_name": "Онлайн карточка",
            "campaign_type": "mql_online",
            "cities": ["Онлайн"],
        }
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("online", "online-account-1"),
        ):
            key = _prepare_launch_attempt(rec, ["Онлайн"])
        expected_name = "Онлайн | Онлайн карточка [ОБЩАЯ]"
        _prepare_city_launch(key, "Онлайн", "online-adset-1", [expected_name], 1)
        started_at = _load_auto_launch_state()["launch_attempts"][key]["started_at"]
        ads = [{
            "id": "online-ad-recovered",
            "name": expected_name,
            "adset_id": "online-adset-1",
            "created_time": started_at,
        }]

        state = _load_auto_launch_state()
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("online", "online-account-1"),
        ), patch(
            "services.auto_launch._fetch_reconciliation_ads",
            return_value=ads,
        ) as mock_fetch, patch("agent.launcher.launch_single") as mock_launch:
            _execute_launch(rec, state, "2026-07-20")

        mock_fetch.assert_called_once_with(
            "online", "online-account-1", ["online-adset-1"]
        )
        mock_launch.assert_not_called()
        assert state["launched_ever"][rec["card_id"]]["ad_ids"] == [
            "online-ad-recovered"
        ]

    def test_reconciliation_listing_входит_в_online_context(self):
        from contextlib import contextmanager
        from services.auto_launch import _fetch_reconciliation_ads

        entered = []

        @contextmanager
        def fake_fb_account(kind):
            entered.append(kind)
            yield

        with patch("services.fb_token_provider.fb_account", side_effect=fake_fb_account), \
             patch(
                 "integrations.facebook.fetch_ads_for_launch_reconciliation",
                 return_value=[],
             ) as mock_fetch:
            assert _fetch_reconciliation_ads("online", "online-account-1") == []

        assert entered == ["online"]
        mock_fetch.assert_called_once_with(
            expected_account_id="online-account-1", adset_ids=None
        )

    def test_partial_account_mismatch_блокирует_create(self, clean_state):
        from services.auto_launch import (
            _execute_launch,
            _finalize_launch_attempt,
            _load_auto_launch_state,
            _prepare_launch_attempt,
            _record_city_failure,
            _record_city_success,
        )

        rec = self._rec()
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "account-A"),
        ):
            key = _prepare_launch_attempt(rec, rec["cities"])
        _record_city_success(key, "CityA", ["ad-citya"])
        _record_city_failure(key, "CityB", "capacity")
        assert _finalize_launch_attempt(key) == "PARTIAL"

        state = _load_auto_launch_state()
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "account-B"),
        ), patch("agent.launcher.launch_single") as mock_launch, \
             pytest.raises(RuntimeError, match="account mismatch"):
            _execute_launch(rec, state, "2026-07-20")

        mock_launch.assert_not_called()
        assert _load_auto_launch_state()["launch_attempts"][key]["phase"] == "BLOCKED"

    def test_legacy_prepared_backfill_до_create(self, clean_state):
        from services.auto_launch import (
            _execute_launch,
            _load_auto_launch_state,
            _prepare_launch_attempt,
        )

        rec = {**self._rec(), "card_id": "legacy-prepared", "cities": ["CityA"]}
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "old-account"),
        ):
            key = _prepare_launch_attempt(rec, ["CityA"])
        legacy_state = _load_auto_launch_state()
        legacy_state["launch_attempts"][key].pop("fb_account_kind")
        legacy_state["launch_attempts"][key].pop("fb_account_id")
        _save_auto_launch_state(legacy_state)

        def launch_after_backfill(**kwargs):
            attempt = _load_auto_launch_state()["launch_attempts"][key]
            assert attempt["fb_account_kind"] == "offline"
            assert attempt["fb_account_id"] == "current-account"
            kwargs["status"]["log"].append("✅ CityA: ad-new")

        state = _load_auto_launch_state()
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "current-account"),
        ), patch("agent.launcher.launch_single", side_effect=launch_after_backfill):
            _execute_launch(rec, state, "2026-07-20")

        assert state["launched_ever"][rec["card_id"]]["complete"] is True

    def test_legacy_partial_with_created_ad_without_account_is_blocked(self, clean_state):
        from services.auto_launch import (
            _execute_launch,
            _finalize_launch_attempt,
            _load_auto_launch_state,
            _prepare_launch_attempt,
            _record_city_failure,
            _record_city_success,
        )

        rec = {**self._rec(), "card_id": "legacy-partial"}
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "old-account"),
        ):
            key = _prepare_launch_attempt(rec, rec["cities"])
        _record_city_success(key, "CityA", ["ad-old"])
        _record_city_failure(key, "CityB", "capacity")
        assert _finalize_launch_attempt(key) == "PARTIAL"
        legacy_state = _load_auto_launch_state()
        legacy_state["launch_attempts"][key].pop("fb_account_kind")
        legacy_state["launch_attempts"][key].pop("fb_account_id")
        _save_auto_launch_state(legacy_state)

        state = _load_auto_launch_state()
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "current-account"),
        ), patch("agent.launcher.launch_single") as mock_launch, pytest.raises(
            RuntimeError, match="CREATE evidence"
        ):
            _execute_launch(rec, state, "2026-07-20")

        mock_launch.assert_not_called()
        attempt = _load_auto_launch_state()["launch_attempts"][key]
        assert attempt["phase"] == "BLOCKED"
        assert attempt["ads_by_city"] == {"CityA": ["ad-old"]}
        assert "fb_account_id" not in attempt

    def test_legacy_launching_without_account_remains_blocked(self, clean_state):
        from services.auto_launch import (
            _execute_launch,
            _load_auto_launch_state,
            _prepare_city_launch,
            _prepare_launch_attempt,
        )

        rec = {**self._rec(), "card_id": "legacy-launching", "cities": ["CityA"]}
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "old-account"),
        ):
            key = _prepare_launch_attempt(rec, ["CityA"])
        _prepare_city_launch(key, "CityA", "adset-1", ["CityA | Legacy"], 1)
        legacy_state = _load_auto_launch_state()
        legacy_state["launch_attempts"][key].pop("fb_account_kind")
        legacy_state["launch_attempts"][key].pop("fb_account_id")
        _save_auto_launch_state(legacy_state)

        state = _load_auto_launch_state()
        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "current-account"),
        ), patch("services.auto_launch._fetch_reconciliation_ads") as mock_fetch, \
             patch("agent.launcher.launch_single") as mock_launch, \
             pytest.raises(RuntimeError, match="legacy LAUNCHING"):
            _execute_launch(rec, state, "2026-07-20")

        mock_fetch.assert_not_called()
        mock_launch.assert_not_called()
        assert _load_auto_launch_state()["launch_attempts"][key]["phase"] == "BLOCKED"


class TestActiveRunLeaseAndDailyRollover:
    def test_второй_concurrent_active_run_не_доходит_до_create(self, clean_state):
        from services.auto_launch import (
            _acquire_active_run_lease,
            _release_active_run_lease,
        )

        first_lease = _acquire_active_run_lease()
        assert first_lease is not None
        try:
            with patch("services.auto_launch._run_auto_launch_inner") as mock_inner:
                result = run_auto_launch(mode="active", max_launches=1)
            mock_inner.assert_not_called()
            assert "другим процессом" in result["skipped_reason"]
        finally:
            _release_active_run_lease(first_lease)

    def test_expired_lease_не_создаёт_вечный_deadlock(self, clean_state):
        from services.auto_launch import (
            _acquire_active_run_lease,
            _load_auto_launch_state,
            _release_active_run_lease,
        )

        expired = {
            "schema_version": 2,
            "launched_today": [],
            "launched_ever": {},
            "launch_attempts": {},
            "last_launch_date": None,
            "active_run_lease": {
                "run_id": "crashed-run",
                "expires_at": "2020-01-01T00:00:00+05:00",
            },
        }
        _save_auto_launch_state(expired)
        lease = _acquire_active_run_lease()
        assert lease is not None
        _release_active_run_lease(lease)
        assert "active_run_lease" not in _load_auto_launch_state()

    def test_proposal_never_writes_verified_daily_success(
        self, sample_coverage, clean_state
    ):
        """Предложение владельцу не помечает карточку запущенной.

        Раньше дневной success-счётчик писался на диск ДО исполнения. Теперь
        producer только предлагает, поэтому verified-состояние
        (last_launch_date/launched_today) обязано остаться нетронутым: иначе
        неодобренное предложение съело бы дневной слот навсегда и выглядело бы
        как реальный запуск.
        """
        from services.auto_launch import _get_today_str, _load_auto_launch_state

        card = {
            "id": "daily-card",
            "name": "Дневная карточка",
            "labels": ["PRODA"],
            "pos": 1.0,
        }
        yesterday_state = {
            "schema_version": 2,
            "launched_today": ["old-card"],
            "launched_ever": {"old-card": "2026-07-19T10:00:00+05:00"},
            "launch_attempts": {},
            "last_launch_date": "2026-07-19",
        }
        _save_auto_launch_state(yesterday_state)
        proposed: list[str] = []

        cfg = {
            "enabled": True,
            "kill_switch": False,
            "launch_enabled": True,
            "max_launches_per_day": 1,
            "launch_checker": {"mode": "enforce"},
        }
        with patch("services.auto_launch._get_autopilot_config", return_value=cfg), \
             patch("services.auto_launch._analyze_coverage", return_value=sample_coverage), \
             patch("services.auto_launch._get_done_list_id", return_value="list1"), \
             patch("services.auto_launch._get_unlaunched_cards", return_value=[card]), \
             patch("services.auto_launch._send_telegram"), \
             patch(
                 "agent.launcher.launch_single",
                 side_effect=_proposal_launcher(proposed),
             ) as mock_launch:
            first = run_auto_launch(mode="active", max_launches=1)

        assert proposed == ["daily-card"]
        assert [item["card_id"] for item in first["proposals"]] == ["daily-card"]
        assert first["launched"] == []
        mock_launch.assert_called_once()

        # Вчерашний verified-срез не переписан «успехом» предложения
        disk_state = _load_auto_launch_state()
        assert disk_state["last_launch_date"] == "2026-07-19"
        assert disk_state["launched_today"] == ["old-card"]
        assert "daily-card" not in disk_state["launched_ever"]
        # Но durable попытка зафиксирована — по ней потом найдут proposal
        assert disk_state["launch_attempts"]
        assert _get_today_str() != "2026-07-19"

    def test_daily_cap_counts_pending_proposals_across_runs(
        self, sample_coverage, clean_state
    ):
        """max_launches_per_day=1 → второй прогон за день не предлагает ещё раз.

        Сообщение самого сервиса обещает считать «verified запусков и pending
        предложений», значит уже созданное предложение обязано занимать дневной
        слот и между прогонами крона: иначе за сутки владелец получает
        предложений больше дневного лимита.
        """
        cards = [
            {"id": "card-one", "name": "Первая", "labels": ["PRODA"], "pos": 1.0},
            {"id": "card-two", "name": "Вторая", "labels": ["PRODA"], "pos": 2.0},
        ]
        proposed: list[str] = []

        cfg = {
            "enabled": True,
            "kill_switch": False,
            "launch_enabled": True,
            "max_launches_per_day": 1,
            "launch_checker": {"mode": "enforce"},
        }
        with patch("services.auto_launch._get_autopilot_config", return_value=cfg), \
             patch("services.auto_launch._analyze_coverage", return_value=sample_coverage), \
             patch("services.auto_launch._get_done_list_id", return_value="list1"), \
             patch("services.auto_launch._get_unlaunched_cards", return_value=cards), \
             patch("services.auto_launch._send_telegram"), \
             patch(
                 "agent.launcher.launch_single",
                 side_effect=_proposal_launcher(proposed),
             ):
            first = run_auto_launch(mode="active", max_launches=1)
            second = run_auto_launch(mode="active", max_launches=1)

        assert len(first["proposals"]) == 1
        assert second["proposals"] == [], (
            "дневной лимит 1 исчерпан первым прогоном, второй прогон всё равно "
            f"создал предложения: {second['proposals']}"
        )
        assert "лимит" in (second["skipped_reason"] or "")

    def test_token_redacted_before_state_or_result(self):
        from services.auto_launch import _safe_error

        sanitized = _safe_error(
            "GET https://graph.facebook.com/ads?access_token=TOP_SECRET&limit=10"
        )
        assert "TOP_SECRET" not in sanitized
        assert "<redacted>" in sanitized


# ---------------------------------------------------------------------------
# Регресс-тест: cities=None в рекомендации не должен ронять Telegram-сводку
# ---------------------------------------------------------------------------
# Баг: после launch-all все рекомендации имеют cities=None (см. TestDecidesLaunches
# .test_запускает_все_готовые_карточки — decide_launches всегда ставит cities=None).
# ", ".join(rec.get("cities", [])) падает "can only join an iterable" на None,
# т.к. .get("cities", []) возвращает default ТОЛЬКО если ключа нет, а не когда
# значение уже None. Прогон прерывался на первой же такой рекомендации.

class TestTelegramSummaryCitiesNone:
    def test_dry_run_сводка_с_cities_none_не_падает(self):
        """_send_dry_run_telegram с рекомендацией cities=None не бросает исключение
        и показывает «все города» вместо городов."""
        from services.auto_launch import _send_dry_run_telegram

        rec = {
            "card_id": "card1",
            "card_name": "Петров / Тема А",
            "campaign_type": "leadgen",
            "cities": None,
            "reason": "launch-all",
        }
        sent_texts = []
        _send_dry_run_telegram([rec], lambda text, channel="ads": sent_texts.append(text))

        assert len(sent_texts) == 1
        assert "все города" in sent_texts[0]

    def test_active_сводка_с_cities_none_не_падает(self):
        """_send_active_telegram с рекомендацией cities=None не бросает исключение
        и показывает «все города» вместо городов."""
        from services.auto_launch import _send_active_telegram

        rec = {
            "card_id": "card1",
            "card_name": "Петров / Тема А",
            "campaign_type": "leadgen",
            "cities": None,
            "reason": "launch-all",
        }
        sent_texts = []
        _send_active_telegram([rec], lambda text, channel="ads": sent_texts.append(text))

        assert len(sent_texts) == 1
        assert "все города" in sent_texts[0]


# ---------------------------------------------------------------------------
# Журнал гипотез (Фаза 4) — интеграция в _execute_launch
# ---------------------------------------------------------------------------
# После успешного launch_single должна регистрироваться гипотеза из лога.
# Сбой регистрации НЕ должен ронять запуск (запуск важнее журнала).
# При hypothesist.enabled=false — гипотеза не регистрируется.

class TestHypothesisJournalIntegration:
    def _make_rec(self):
        return {
            "card_id": "card_hyp",
            "card_name": "Петров / Тема А",
            "campaign_type": "leadgen",
            "cities": ["CityA"],
        }

    def _make_state(self):
        return {
            "launched_today": [],
            "launched_ever": {},
            "last_launch_date": None,
        }

    def test_успешный_запуск_регистрирует_гипотезу_из_лога(self, clean_state):
        """После успешного запуска record_hypothesis вызывается с ad_ids,
        распарсенными из status['log']."""
        from services.auto_launch import _execute_launch

        def mock_launch_single(**kwargs):
            kwargs["status"]["log"].append("✅ CityA: 12345")

        with patch("agent.launcher.launch_single", side_effect=mock_launch_single), \
             patch(
                 "services.auto_launch._get_autopilot_config",
                 return_value={"hypothesist": {"enabled": True}},
             ), \
             patch("services.auto_launch._record_hypothesis") as mock_record:
            _execute_launch(self._make_rec(), self._make_state(), "2026-07-02")

        mock_record.assert_called_once_with(
            "Петров / Тема А",
            "leadgen",
            {"CityA": ["12345"]},
            None,
        )

    def test_сбой_регистрации_гипотезы_не_ломает_запуск(self, clean_state):
        """record_hypothesis бросает исключение — _execute_launch не падает
        (запуск важнее журнала, только logger.warning)."""
        from services.auto_launch import _execute_launch

        def mock_launch_single(**kwargs):
            kwargs["status"]["log"].append("✅ CityA: 12345")

        with patch("agent.launcher.launch_single", side_effect=mock_launch_single), \
             patch(
                 "services.auto_launch._get_autopilot_config",
                 return_value={"hypothesist": {"enabled": True}},
             ), \
             patch(
                 "services.auto_launch._record_hypothesis",
                 side_effect=RuntimeError("KB не инициализирована"),
             ):
            # Не должно бросить исключение наружу
            _execute_launch(self._make_rec(), self._make_state(), "2026-07-02")

    def test_enabled_false_не_регистрирует_гипотезу(self, clean_state):
        """При hypothesist.enabled=False record_hypothesis не вызывается."""
        from services.auto_launch import _execute_launch

        def mock_launch_single(**kwargs):
            kwargs["status"]["log"].append("✅ CityA: 12345")

        with patch("agent.launcher.launch_single", side_effect=mock_launch_single), \
             patch(
                 "services.auto_launch._get_autopilot_config",
                 return_value={"hypothesist": {"enabled": False}},
             ), \
             patch("services.auto_launch._record_hypothesis") as mock_record:
            _execute_launch(self._make_rec(), self._make_state(), "2026-07-02")

        mock_record.assert_not_called()


# ---------------------------------------------------------------------------
# Имена карточек в launched_ever (пункт 2 пакета мелочей) + остаток очереди
# ---------------------------------------------------------------------------

class TestLaunchedEverStoresName:
    """launched_ever теперь хранит {"at": iso, "name": str} — старый формат
    (просто строка iso_datetime) остаётся читаемым (обратная совместимость)."""

    def test_execute_launch_сохраняет_name_вместе_с_датой(self, clean_state):
        """После _execute_launch запись в launched_ever — dict с at и name."""
        from services.auto_launch import _execute_launch, _load_auto_launch_state

        def mock_launch_single(**kwargs):
            kwargs["status"]["log"].append("✅ CityA: ad-name-test")

        state = {"launched_today": [], "launched_ever": {}, "last_launch_date": None}
        rec = {
            "card_id": "card_name_test",
            "card_name": "Петров / Тестовая карточка",
            "campaign_type": "leadgen",
            "cities": ["CityA"],
        }

        with patch("agent.launcher.launch_single", side_effect=mock_launch_single):
            _execute_launch(rec, state, "2026-07-07")

        state_after = _load_auto_launch_state()
        entry = state_after["launched_ever"]["card_name_test"]
        assert isinstance(entry, dict)
        assert entry["name"] == "Петров / Тестовая карточка"
        assert entry["at"]

    def test_launched_at_читает_старый_формат_строка(self):
        """_launched_at на старой записи (просто строка iso_datetime) не падает."""
        from services.auto_launch import _launched_at

        assert _launched_at("2026-07-07T10:00:00+05:00") == "2026-07-07T10:00:00+05:00"

    def test_launched_at_читает_новый_формат_dict(self):
        """_launched_at на новой записи (dict с at/name) достаёт дату."""
        from services.auto_launch import _launched_at

        entry = {"at": "2026-07-07T10:00:00+05:00", "name": "Карточка"}
        assert _launched_at(entry) == "2026-07-07T10:00:00+05:00"

    def test_launched_name_старый_формат_пустая_строка(self):
        """_launched_name на старой записи (строка без имени) — пустая строка,
        не бросает исключение."""
        from services.auto_launch import _launched_name

        assert _launched_name("2026-07-07T10:00:00+05:00") == ""

    def test_launched_name_новый_формат(self):
        """_launched_name на новой записи достаёт имя карточки."""
        from services.auto_launch import _launched_name

        entry = {"at": "2026-07-07T10:00:00+05:00", "name": "Карточка X"}
        assert _launched_name(entry) == "Карточка X"

    def test_decide_launches_работает_со_старым_форматом_state(self, sample_coverage, sample_cards, clean_state):
        """decide_launches читает already_launched из launched_ever.keys() —
        не важно, старый формат записи (строка) или новый (dict), ключи те же."""
        state = {
            "launched_today": ["card1"],
            "launched_ever": {"card1": "2026-06-24T10:00:00+05:00"},  # старый формат
            "last_launch_date": "2026-06-24",
        }
        _save_auto_launch_state(state)

        recs = decide_launches(sample_coverage, sample_cards)
        card_ids = [r["card_id"] for r in recs]
        assert "card1" not in card_ids


class TestActiveTelegramRemainingQueue:
    """Отчёт о запуске показывает остаток очереди «Готово» (пункт 2 пакета мелочей)."""

    def test_отчёт_содержит_остаток_очереди(self):
        from services.auto_launch import _send_active_telegram

        rec = {
            "card_id": "card1",
            "card_name": "Тест",
            "campaign_type": "leadgen",
            "cities": None,
            "reason": "запуск готовой карточки",
        }
        sent_texts = []
        _send_active_telegram([rec], lambda text, channel="ads": sent_texts.append(text), remaining_in_queue=4)

        assert 'В очереди «Готово»: 4' in sent_texts[0]

    def test_без_remaining_in_queue_строка_не_добавляется(self):
        """Обратная совместимость: без явного remaining_in_queue строка остатка
        не появляется (не ломаем старые вызовы/тесты)."""
        from services.auto_launch import _send_active_telegram

        rec = {
            "card_id": "card1",
            "card_name": "Тест",
            "campaign_type": "leadgen",
            "cities": None,
            "reason": "запуск готовой карточки",
        }
        sent_texts = []
        _send_active_telegram([rec], lambda text, channel="ads": sent_texts.append(text))

        assert "осталось" not in sent_texts[0]

    def test_run_auto_launch_отчитывается_об_очереди_без_второго_запроса_trello(
        self, sample_coverage, sample_cards, clean_state
    ):
        """Отчёт о предложениях считает очередь по уже прочитанным карточкам.

        Раньше active-путь отправлял отчёт о выполненных запусках со строкой
        «В очереди «Готово»: N». Реальных запусков в producer больше нет, поэтому
        владелец получает отчёт о предложениях — но инвариант тот же: остаток
        очереди берётся из уже прочитанного списка карточек, без повторного
        запроса к Trello.
        """
        sent_texts = []

        def fake_send_telegram(text, channel="ads"):
            sent_texts.append(text)
            return True

        with patch("services.auto_launch._get_autopilot_config", return_value={
            "enabled": True,
            "kill_switch": False,
            "launch_enabled": True,
            "max_launches_per_day": 10,
            "launch_checker": {"mode": "enforce"},
        }), patch("services.auto_launch._analyze_coverage", return_value=sample_coverage), \
           patch("services.auto_launch._get_done_list_id", return_value="list1"), \
           patch("services.auto_launch._get_unlaunched_cards", return_value=sample_cards) as mock_cards, \
           patch("services.auto_launch._send_telegram", side_effect=fake_send_telegram), \
           patch("agent.launcher.launch_single", side_effect=_proposal_launcher([])):

            result = run_auto_launch(mode="active", max_launches=10)

        # Trello прочитан ровно один раз — очередь считается по нему
        mock_cards.assert_called_once()
        assert sent_texts, "ожидали Telegram-отчёт владельцу"
        # Raw=4, checker допускает 3, одна veto-карточка blocked
        assert result["raw_count"] == 4
        assert result["raw_count"] == result["eligible_count"] + result["blocked_count"]
        report = sent_texts[0]
        assert f"Всего: {result['raw_count']}" in report
        assert f"Можно запустить: {result['eligible_count']}" in report
        assert f"Заблокировано: {result['blocked_count']}" in report


class TestLaunchedEverStoresAdIds:
    """launched_ever[card_id]["ad_ids"] — плоский список ad_id, добавленных
    сегодняшним запуском (источник для Контроля запуска)."""

    def test_execute_launch_сохраняет_ad_ids_из_лога(self, clean_state):
        """После успешного запуска с несколькими городами в логе — ad_ids
        содержит плоский список всех ad_id (без разбивки по городам)."""
        from services.auto_launch import _execute_launch, _load_auto_launch_state

        def mock_launch_single(**kwargs):
            kwargs["status"]["log"].append("✅ CityA: 111")
            kwargs["status"]["log"].append("✅ CityB: 2 объявлений — 222, 333")

        state = {"launched_today": [], "launched_ever": {}, "last_launch_date": None}
        rec = {
            "card_id": "card_ad_ids",
            "card_name": "Тестовая карточка ad_ids",
            "campaign_type": "leadgen",
            "cities": ["CityA", "CityB"],
        }

        with patch("agent.launcher.launch_single", side_effect=mock_launch_single), \
             patch("services.auto_launch._get_autopilot_config", return_value={}), \
             patch("services.auto_launch._record_hypothesis"):
            _execute_launch(rec, state, "2026-07-07")

        state_after = _load_auto_launch_state()
        entry = state_after["launched_ever"]["card_ad_ids"]
        assert sorted(entry["ad_ids"]) == ["111", "222", "333"]

    def test_execute_launch_без_ad_id_не_добавляет_launched_ever(self, clean_state):
        """Ноль подтверждённых ad_id не создаёт ложную запись launched_ever."""
        from services.auto_launch import _execute_launch, _load_auto_launch_state

        def mock_launch_single(**kwargs):
            pass  # пустой лог

        state = {"launched_today": [], "launched_ever": {}, "last_launch_date": None}
        rec = {
            "card_id": "card_no_ads",
            "card_name": "Без объявлений",
            "campaign_type": "leadgen",
            "cities": ["CityA"],
        }

        with patch("agent.launcher.launch_single", side_effect=mock_launch_single), \
             patch("services.auto_launch._get_autopilot_config", return_value={}), \
             patch("services.auto_launch._record_hypothesis"), \
             pytest.raises(RuntimeError, match="Частичный запуск"):
            _execute_launch(rec, state, "2026-07-07")

        state_after = _load_auto_launch_state()
        assert "card_no_ads" not in state_after["launched_ever"]


# ---------------------------------------------------------------------------
# T4: продуктовая метка в новые запуски (docs/specs/ARCH-product-tags.md)
# decide_launches прокидывает labels карточки в рекомендацию, _execute_launch
# передаёт их в launch_single как trello_labels, launch_single определяет
# product через classify_product и передаёт в launch_creative.
# ---------------------------------------------------------------------------


class TestDecideLaunchesLabels:
    def test_labels_карточки_попадают_в_рекомендацию(self, sample_coverage, sample_cards, clean_state):
        """decide_launches кладёт labels кандидата в рекомендацию (rec['labels']) —
        нужно, чтобы _execute_launch мог прокинуть их в classify_product."""
        recs = decide_launches(sample_coverage, sample_cards)

        by_card_id = {r["card_id"]: r for r in recs}
        assert by_card_id["card1"]["labels"] == ["PRODA"]
        assert by_card_id["card2"]["labels"] == ["PRODB"]

    def test_labels_пустой_список_если_у_карточки_нет_меток(self, sample_coverage, clean_state):
        """Карточка без меток → rec['labels'] == [] (не падает, не None по умолчанию)."""
        cards = [
            {"id": "no_label_card", "name": "Без меток", "labels": [], "desc": "", "dueComplete": False},
        ]
        recs = decide_launches(sample_coverage, cards)
        assert len(recs) == 1
        assert recs[0]["labels"] == []


class TestExecuteLaunchPassesLabels:
    def test_execute_launch_передаёт_trello_labels_в_launch_single(self, clean_state):
        """_execute_launch передаёт rec['labels'] в launch_single как trello_labels."""
        from services.auto_launch import _execute_launch

        state = {"launched_today": [], "launched_ever": {}, "last_launch_date": None}
        rec = {
            "card_id": "card_labels",
            "card_name": "Петров / PRODA карточка",
            "campaign_type": "leadgen",
            "cities": ["CityA"],
            "labels": ["PRODA"],
        }

        with patch("agent.launcher.launch_single") as mock_launch_single, \
             patch("services.auto_launch._get_autopilot_config", return_value={}), \
             patch("services.auto_launch._record_hypothesis"), \
             pytest.raises(RuntimeError, match="Частичный запуск"):
            _execute_launch(rec, state, "2026-07-07")

        assert mock_launch_single.call_args.kwargs["trello_labels"] == ["PRODA"]

    def test_execute_launch_без_labels_в_rec_передаёт_none(self, clean_state):
        """Обратная совместимость: если в rec нет ключа 'labels' (старые тесты/
        вызовы), trello_labels=None, launch_single не падает."""
        from services.auto_launch import _execute_launch

        state = {"launched_today": [], "launched_ever": {}, "last_launch_date": None}
        rec = {
            "card_id": "card_no_labels_key",
            "card_name": "Без ключа labels",
            "campaign_type": "leadgen",
            "cities": ["CityA"],
        }

        with patch("agent.launcher.launch_single") as mock_launch_single, \
             patch("services.auto_launch._get_autopilot_config", return_value={}), \
             patch("services.auto_launch._record_hypothesis"), \
             pytest.raises(RuntimeError, match="Частичный запуск"):
            _execute_launch(rec, state, "2026-07-07")

        assert mock_launch_single.call_args.kwargs["trello_labels"] is None


# agent.launcher.launch_single — прямые тесты продуктовой метки. Мокаем ТОЛЬКО
# границы (Trello/Drive/Facebook), как в tests/test_launch_partial_failure.py.

def _make_status() -> dict:
    return {"running": True, "current": "Test Card", "progress": 0, "total": 0,
            "step": "", "step_pct": None, "log": []}


def _fake_media() -> dict:
    return {"type": "video", "paths": [__file__]}


def _test_authorization() -> ProviderLaunchAuthorization:
    return ProviderLaunchAuthorization("auth-product-tag", "test-secret")


class TestStagedProductTag:
    """Product вычисляется из server-owned Trello snapshot на staging boundary."""

    @pytest.mark.parametrize(
        ("name", "labels", "expected_product"),
        [
            ("Сидорова / Тема", ("PRODB",), "PRODB"),
            ("Петров / Тема А", ("PRODA",), "PRODA"),
            ("Просто тема", (), "ОБЩАЯ"),
            ("CityB | Тема А / prodb", (), "PRODB"),
        ],
    )
    def test_product_берётся_из_live_trello_snapshot(
        self,
        name,
        labels,
        expected_product,
    ):
        from services.approval_checker_models import LaunchSourceInput
        from services import launch_staging

        snapshot = SimpleNamespace(
            card_id="card1",
            board_id=launch_staging.TRELLO_BOARD_ID,
            list_id="ready-list",
            name=name,
            description="desc",
            due_complete=False,
            closed=False,
            labels=tuple({"name": label} for label in labels),
            attachments=(
                {
                    "id": "attachment-1",
                    "url": "https://drive.google.com/file/d/exact",
                },
            ),
        )
        source = LaunchSourceInput(
            card_id="card1",
            campaign_type="leadgen",
            requested_cities=("CityA",),
            as_carousel=False,
            origin_reference="test-product",
        )
        with patch.object(launch_staging, "get_card_snapshot", return_value=snapshot), patch.object(
            launch_staging, "get_done_list_id", return_value="ready-list"
        ), patch.object(launch_staging, "detect_language", return_value="L1"), patch(
            "integrations.gdrive.download_media", return_value=_fake_media()
        ):
            resolved = launch_staging._resolve_launch_source(source)

        assert resolved.product == expected_product


class TestLaunchCreativeProductInName:
    """Канонический planner имён совпадает с product tag contract.

    Provider CREATE отдельно покрыт tests/test_provider_launch_guard.py и не
    вызывается здесь без DB-backed authorization.
    """

    def test_product_задан_имя_объявления_с_тегом(self, tmp_path):
        """launch_creative(product='PRODB') → имя, переданное в create_image_ad,
        заканчивается на ' [PRODB]'."""
        from integrations.facebook import _expected_city_ad_names

        names = _expected_city_ad_names(
            "CityA", "CityB | Тема А", "image", [], [{"label": "fake"}], [], [], "PRODB"
        )
        assert names == ["CityA | CityB | Тема А [PRODB]"]

    def test_product_none_имя_без_тега(self, tmp_path):
        """launch_creative(product=None) (дефолт) — имя объявления без тега,
        обратная совместимость со старыми вызовами."""
        from integrations.facebook import _expected_city_ad_names

        names = _expected_city_ad_names(
            "CityA", "CityB | Тема А", "image", [], [{"label": "fake"}], [], [], None
        )
        assert names == ["CityA | CityB | Тема А"]


# ---------------------------------------------------------------------------
# «Все города» через карту маршрутизации город→кабинет
# ---------------------------------------------------------------------------

class TestStandardCitiesRouting:
    """_target_cities без явного списка = ключи карты launch_routing.

    CityF вернулась во «все города»: город переехал в кабинет cabinet_b,
    и конвейер маршрутизирует каждый город в его кабинет.
    """

    def test_все_города_включают_cityf(self, monkeypatch):
        import agent.scheduler as scheduler
        from services.auto_launch import _target_cities
        from services.launch_routing import all_cities

        monkeypatch.setattr(scheduler, "load_settings", lambda: {})
        cities = _target_cities({"campaign_type": "leadgen"})
        assert cities == all_cities()
        assert "CityF" in cities
        assert cities[:5] == ["CityA", "CityB", "CityC", "CityD", "CityE"]

    def test_онлайн_кампания_не_трогает_карту(self):
        from services.auto_launch import _target_cities

        assert _target_cities({"campaign_type": "mql_online"}) == ["Онлайн"]

    def test_явный_список_городов_не_подменяется(self):
        from services.auto_launch import _target_cities

        rec = {"campaign_type": "leadgen", "cities": ["CityA", "CityF"]}
        assert _target_cities(rec) == ["CityA", "CityF"]

    def test_исключение_города_через_settings_без_деплоя(self, monkeypatch):
        import agent.scheduler as scheduler
        from services.auto_launch import _target_cities

        monkeypatch.setattr(
            scheduler,
            "load_settings",
            lambda: {"launch_routing": {"cities": {"CityF": None}}},
        )
        cities = _target_cities({"campaign_type": "leadgen"})
        assert "CityF" not in cities
        assert cities == ["CityA", "CityB", "CityC", "CityD", "CityE"]


class TestMultiAccountReconciliation:
    """Города одной карточки в разных кабинетах: сверка читает каждый."""

    def _rec(self):
        return {
            "card_id": "card-multi",
            "card_name": "Мультикабинетная карточка",
            "campaign_type": "leadgen",
        }

    def test_city_plan_хранит_кабинет_города(self, clean_state):
        from services.auto_launch import (
            _load_auto_launch_state,
            _prepare_city_launch,
            _prepare_launch_attempt,
        )

        key = _prepare_launch_attempt(self._rec(), ["CityA"])
        _prepare_city_launch(
            key,
            "CityA",
            "adset-1",
            ["CityA | Мульти [PRODA]"],
            1,
            account_id="act_29716040622546856",
        )
        plan = _load_auto_launch_state()["launch_attempts"][key]["city_plan"]["CityA"]
        assert plan["account_id"] == "29716040622546856"

    def test_reconcile_читает_кабинет_каждого_города(self, clean_state):
        from services.auto_launch import (
            _load_auto_launch_state,
            _prepare_city_launch,
            _prepare_launch_attempt,
            _reconcile_launch_attempt,
        )

        rec = {**self._rec(), "cities": ["CityA", "CityF"]}
        key = _prepare_launch_attempt(rec, ["CityA", "CityF"])
        _prepare_city_launch(
            key, "CityA", "adset-a", ["CityA | Мульти [PRODA]"], 1,
            account_id="111",
        )
        _prepare_city_launch(
            key, "CityF", "adset-k", ["CityF | Мульти [PRODA]"], 1,
            account_id="222",
        )
        created_time = _load_auto_launch_state()["launch_attempts"][key]["started_at"]
        ads_per_account = {
            "111": [{
                "id": "ad-a",
                "name": "CityA | Мульти [PRODA]",
                "adset_id": "adset-a",
                "created_time": created_time,
            }],
            "222": [{
                "id": "ad-k",
                "name": "CityF | Мульти [PRODA]",
                "adset_id": "adset-k",
                "created_time": created_time,
            }],
        }
        fetched: list[str] = []

        def fake_fetch(account_kind, account_id, adset_ids=None):
            fetched.append((account_id, tuple(adset_ids or ())))
            return ads_per_account[account_id]

        with patch(
            "services.auto_launch._fetch_reconciliation_ads",
            side_effect=fake_fetch,
        ):
            assert _reconcile_launch_attempt(key) == "SUCCEEDED"

        assert sorted(fetched) == [("111", ("adset-a",)), ("222", ("adset-k",))]
        attempt = _load_auto_launch_state()["launch_attempts"][key]
        assert attempt["ads_by_city"] == {
            "CityA": ["ad-a"],
            "CityF": ["ad-k"],
        }

    def test_fetch_reconciliation_ads_строит_offline_контекст(self, monkeypatch):
        from contextlib import contextmanager

        import integrations.facebook as facebook_integration
        from services import auto_launch, fb_token_provider

        seen: dict[str, object] = {}

        @contextmanager
        def fake_fb_account(name):
            seen["context"] = name
            yield

        monkeypatch.setattr(fb_token_provider, "fb_account", fake_fb_account)
        monkeypatch.setattr(
            fb_token_provider,
            "offline_account_context",
            lambda account_id: f"offline:{account_id}",
        )
        monkeypatch.setattr(
            facebook_integration,
            "fetch_ads_for_launch_reconciliation",
            lambda expected_account_id, adset_ids=None: [],
        )
        assert auto_launch._fetch_reconciliation_ads("offline", "12345") == []
        assert seen["context"] == "offline:12345"


class TestPartialDrain:
    """Частичный запуск дозапускает города без объявлений новым предложением."""

    def _partial(self, *, unresolved=False):
        from services.auto_launch import (
            _load_auto_launch_state,
            _prepare_city_launch,
            _prepare_launch_attempt,
            _record_city_success,
            _save_auto_launch_state,
        )

        rec = {"card_id": "card-part", "card_name": "Частичная карточка", "campaign_type": "leadgen"}
        key = _prepare_launch_attempt(rec, ["CityA", "CityB", "CityC"])
        for city in ("CityA", "CityB"):
            _prepare_city_launch(key, city, f"adset-{city}", [f"{city} | Частичная [PRODA]"], 1, media_manifest_sha256="a" * 64)
        _record_city_success(key, "CityA", ["ad-1"])
        state = _load_auto_launch_state()
        attempt = state["launch_attempts"][key]
        attempt["phase"] = "PARTIAL"
        if unresolved:
            attempt["city_plan"]["CityB"]["create_started_at"] = "2026-09-23T10:00:00+05:00"
        _save_auto_launch_state(state)
        return key

    def test_отработанное_предложение_ротирует_частичную_попытку(self, clean_state):
        from services.auto_launch import _load_auto_launch_state, _rotate_poisoned_attempt_key

        key = self._partial()
        with patch("services.owner_action_repository.find_proposal_state_by_idempotency_key", return_value="EXECUTED"):
            assert _rotate_poisoned_attempt_key(key) is True
        attempt = _load_auto_launch_state()["launch_attempts"][key]
        assert attempt["phase"] == "PREPARED"
        assert attempt["pending_cities"] == ["CityB", "CityC"]
        assert attempt["ads_by_city"]["CityA"] == ["ad-1"], "созданное не теряется"
        assert set(attempt["city_plan"]) == {"CityA"}, "планы недостающих городов сброшены"

    def test_живое_или_неизвестное_не_ротируется(self, clean_state):
        from services.auto_launch import _rotate_poisoned_attempt_key

        key = self._partial()
        with patch("services.owner_action_repository.find_proposal_state_by_idempotency_key", return_value="ATTEMPT_STARTED"):
            assert _rotate_poisoned_attempt_key(key) is False
        with patch("services.owner_action_repository.find_proposal_state_by_idempotency_key", return_value="RECONCILE_REQUIRED"):
            assert _rotate_poisoned_attempt_key(key) is False

    def test_город_с_неизвестным_create_держит_ротацию(self, clean_state):
        from services.auto_launch import _rotate_poisoned_attempt_key

        key = self._partial(unresolved=True)
        with patch("services.owner_action_repository.find_proposal_state_by_idempotency_key", return_value="EXECUTED"):
            assert _rotate_poisoned_attempt_key(key) is False

    def test_сверка_отработанного_предложения_снимает_тупик_create_started(self, clean_state):
        """Раньше «после CREATE_STARTED объявлений нет» → BLOCKED навсегда; при отработанном предложении — факт."""
        from services.auto_launch import (
            _load_auto_launch_state,
            _reconcile_launch_attempt,
            _save_auto_launch_state,
        )

        key = self._partial(unresolved=True)
        state = _load_auto_launch_state()
        attempt = state["launch_attempts"][key]
        attempt["phase"] = "LAUNCHING"
        for city in ("CityA", "CityB"):
            attempt["city_plan"][city]["reconcile_from"] = "2026-09-23T00:00:00+05:00"
            attempt["city_plan"][city]["reconcile_until"] = "2026-09-23T23:00:00+05:00"
        _save_auto_launch_state(state)
        with patch("services.auto_launch._fetch_reconciliation_ads", return_value=[]), patch(
            "services.owner_action_repository.find_proposal_state_by_idempotency_key", return_value="EXECUTED"
        ):
            outcome = _reconcile_launch_attempt(key)
        attempt = _load_auto_launch_state()["launch_attempts"][key]
        assert outcome == "PARTIAL"
        assert "create_started_at" not in attempt["city_plan"]["CityB"]

        # То же без отработанного предложения — прежний строгий BLOCKED.
        key2 = key
        state = _load_auto_launch_state()
        attempt = state["launch_attempts"][key2]
        attempt["phase"] = "LAUNCHING"
        attempt["city_plan"]["CityB"]["create_started_at"] = "2026-09-23T10:00:00+05:00"
        _save_auto_launch_state(state)
        with patch("services.auto_launch._fetch_reconciliation_ads", return_value=[]), patch(
            "services.owner_action_repository.find_proposal_state_by_idempotency_key", return_value="ATTEMPT_STARTED"
        ), patch("services.auto_launch._send_telegram"):
            assert _reconcile_launch_attempt(key2) == "BLOCKED"


class TestResumable:
    """Попытка, ждущая дозапуска недостающих городов."""

    def _attempt(self, **changes):
        from datetime import datetime, timedelta

        from services.auto_launch import _TZ_LOCAL

        attempt = {
            "card_id": "card-r",
            "phase": "PREPARED",
            "target_cities": ["CityA", "CityB"],
            "ads_by_city": {"CityA": ["ad-1"]},
            "started_at": (datetime.now(_TZ_LOCAL) - timedelta(days=5)).isoformat(),
        }
        attempt.update(changes)
        return attempt

    def test_частичная_свежая_попытка_ждёт_дозапуска(self):
        from services.auto_launch import attempt_resumable, resumable_card_ids

        assert attempt_resumable(self._attempt()) is True
        assert resumable_card_ids({"launch_attempts": {"k": self._attempt()}}) == {"card-r"}

    def test_не_ждёт_если_всё_создано_ничего_не_создано_или_старше_30_дней(self):
        from datetime import datetime, timedelta

        from services.auto_launch import _TZ_LOCAL, attempt_resumable

        assert attempt_resumable(self._attempt(ads_by_city={"CityA": ["a"], "CityB": ["b"]})) is False
        assert attempt_resumable(self._attempt(ads_by_city={})) is False
        old = (datetime.now(_TZ_LOCAL) - timedelta(days=31)).isoformat()
        assert attempt_resumable(self._attempt(started_at=old)) is False
        assert attempt_resumable(self._attempt(phase="SUCCEEDED")) is False

    def test_прогон_ротирует_частичную_попытку_до_гейта(self, clean_state):
        from services.auto_launch import (
            _load_auto_launch_state,
            _reconcile_pending_cards,
            _save_auto_launch_state,
        )

        state = _load_auto_launch_state()
        state.setdefault("launch_attempts", {})["k"] = self._attempt(phase="PARTIAL", idempotency_key="old-key")
        _save_auto_launch_state(state)
        with patch("services.owner_action_repository.find_proposal_state_by_idempotency_key", return_value="EXECUTED"):
            new_state = _reconcile_pending_cards([{"id": "card-r"}], _load_auto_launch_state())
        attempt = new_state["launch_attempts"]["k"]
        assert attempt["phase"] == "PREPARED" and attempt["rotated_from"] == "old-key"
        assert attempt["pending_cities"] == ["CityB"]

    def test_чекер_пропускает_частичную_карточку_на_дозапуске(self):
        from services.launch_checker import _state_result

        state = {
            "launched_ever": {"card-r": {"complete": False, "ad_ids": ["ad-1"]}},
            "launch_attempts": {"k": self._attempt()},
        }
        assert _state_result("card-r", "check-1", state) is None
        # не ротирована (LAUNCHING) — прежний отказ
        state["launch_attempts"]["k"]["phase"] = "LAUNCHING"
        assert _state_result("card-r", "check-1", state).reason_codes == ("RECONCILIATION_REQUIRED",)


class TestPoisonedKeyDrain:
    """Дренаж: ротация отравленных ключей и выход из BLOCKED."""

    def _rec(self):
        return {
            "card_id": "card-poison",
            "card_name": "Отравленная карточка",
            "campaign_type": "leadgen",
        }

    def test_терминальное_предложение_ротирует_ключ(self, clean_state):
        from services.auto_launch import (
            _load_auto_launch_state,
            _prepare_launch_attempt,
            _rotate_poisoned_attempt_key,
        )

        key = _prepare_launch_attempt(self._rec(), ["CityA"])
        old_uuid = _load_auto_launch_state()["launch_attempts"][key]["idempotency_key"]
        with patch(
            "services.owner_action_repository.find_proposal_state_by_idempotency_key",
            return_value="BLOCKED_STALE",
        ):
            assert _rotate_poisoned_attempt_key(key) is True
        attempt = _load_auto_launch_state()["launch_attempts"][key]
        assert attempt["idempotency_key"] != old_uuid
        assert attempt["rotated_from"] == old_uuid
        assert attempt["phase"] == "PREPARED"

    def test_живое_предложение_не_ротируется(self, clean_state):
        from services.auto_launch import (
            _load_auto_launch_state,
            _prepare_launch_attempt,
            _rotate_poisoned_attempt_key,
        )

        key = _prepare_launch_attempt(self._rec(), ["CityA"])
        old_uuid = _load_auto_launch_state()["launch_attempts"][key]["idempotency_key"]
        with patch(
            "services.owner_action_repository.find_proposal_state_by_idempotency_key",
            return_value="PENDING_OWNER",
        ):
            assert _rotate_poisoned_attempt_key(key) is False
        assert (
            _load_auto_launch_state()["launch_attempts"][key]["idempotency_key"]
            == old_uuid
        )

    def test_create_evidence_блокирует_ротацию(self, clean_state):
        from services.auto_launch import (
            _load_auto_launch_state,
            _prepare_city_launch,
            _prepare_launch_attempt,
            _rotate_poisoned_attempt_key,
            _save_auto_launch_state,
        )

        key = _prepare_launch_attempt(self._rec(), ["CityA"])
        _prepare_city_launch(key, "CityA", "adset-1", ["CityA | Яд [PRODA]"], 1)
        # CREATE начинался: create_started_at — настоящее evidence, в отличие
        # от самого city_plan (он пишется и у отклонённых до провайдера).
        state = _load_auto_launch_state()
        state["launch_attempts"][key]["phase"] = "PREPARED"
        state["launch_attempts"][key]["city_plan"]["CityA"]["create_started_at"] = (
            "2026-09-01T10:00:00+05:00"
        )
        _save_auto_launch_state(state)
        with patch(
            "services.owner_action_repository.find_proposal_state_by_idempotency_key",
            return_value="EXPIRED",
        ):
            assert _rotate_poisoned_attempt_key(key) is False

    def test_blocked_без_create_ротируется_и_чистит_планы(self, clean_state):
        from services.auto_launch import (
            _load_auto_launch_state,
            _prepare_city_launch,
            _prepare_launch_attempt,
            _rotate_poisoned_attempt_key,
            _save_auto_launch_state,
        )

        key = _prepare_launch_attempt(self._rec(), ["CityA"])
        _prepare_city_launch(key, "CityA", "adset-1", ["CityA | Яд [PRODA]"], 1)
        state = _load_auto_launch_state()
        state["launch_attempts"][key]["phase"] = "BLOCKED"
        state["launch_attempts"][key]["errors_by_city"] = {
            "__reconciliation__": "слепая сверка"
        }
        _save_auto_launch_state(state)
        with patch(
            "services.owner_action_repository.find_proposal_state_by_idempotency_key",
            return_value="BLOCKED_STALE",
        ):
            assert _rotate_poisoned_attempt_key(key) is True
        attempt = _load_auto_launch_state()["launch_attempts"][key]
        assert attempt["phase"] == "PREPARED"
        assert attempt["city_plan"] == {}
        assert attempt["errors_by_city"] == {}
        assert attempt["pending_cities"] == ["CityA"]

    def test_reconcile_allow_blocked_выводит_из_blocked(self, clean_state):
        from services.auto_launch import (
            _load_auto_launch_state,
            _prepare_city_launch,
            _prepare_launch_attempt,
            _reconcile_launch_attempt,
            _save_auto_launch_state,
        )

        rec = {**self._rec(), "cities": ["CityA"]}
        key = _prepare_launch_attempt(rec, ["CityA"])
        _prepare_city_launch(key, "CityA", "adset-1", ["CityA | Яд [PRODA]"], 1)
        created_time = _load_auto_launch_state()["launch_attempts"][key]["started_at"]
        state = _load_auto_launch_state()
        state["launch_attempts"][key]["phase"] = "BLOCKED"
        state["launch_attempts"][key]["errors_by_city"] = {
            "__reconciliation__": "старая слепая сверка"
        }
        _save_auto_launch_state(state)

        ads = [{
            "id": "ad-unblocked",
            "name": "CityA | Яд [PRODA]",
            "adset_id": "adset-1",
            "created_time": created_time,
        }]
        # Без allow_blocked фаза не меняется.
        assert _reconcile_launch_attempt(key) == "BLOCKED"
        with patch("services.auto_launch._fetch_reconciliation_ads", return_value=ads):
            assert _reconcile_launch_attempt(key, allow_blocked=True) == "SUCCEEDED"
        attempt = _load_auto_launch_state()["launch_attempts"][key]
        assert attempt["ads_by_city"] == {"CityA": ["ad-unblocked"]}
        assert "__reconciliation__" not in attempt.get("errors_by_city", {})


class TestGatewayCreateMarkerAndWindow:
    """CREATE-маркер только после резервации; окно сверки от CREATE, не от started_at.

    Дефект: маркер писался до резервации durable-авторизации, падение
    резервации или исполнителя до первого claim оставляло ложную улику CREATE,
    сверка блокировала попытку навсегда, ротация ключа запрещалась. Окно
    сверки строилось от attempt.started_at, который в боевом пути не
    обновляется, — план ротированной попытки получал окно из прошлого месяца.
    """

    SHA = "a" * 64

    def _rec(self):
        return {
            "card_id": "card-marker",
            "card_name": "Маркерная карточка",
            "campaign_type": "leadgen",
        }

    def _launching_attempt(self):
        from services.auto_launch import _prepare_city_launch, _prepare_launch_attempt

        key = _prepare_launch_attempt(self._rec(), ["CityA"])
        _prepare_city_launch(
            key,
            "CityA",
            "adset-1",
            ["CityA | Маркер [PRODA]"],
            1,
            media_manifest_sha256=self.SHA,
        )
        return key

    def _manifest(self, key):
        idempotency_key = _load_auto_launch_state()["launch_attempts"][key][
            "idempotency_key"
        ]
        return SimpleNamespace(
            origin=ActionOrigin.AUTO_LAUNCH,
            idempotency_key=idempotency_key,
            manifest_id="manifest-marker",
            destinations=(
                SimpleNamespace(city="CityA", creatives=(SimpleNamespace(),)),
            ),
        )

    def _plan(self, key):
        return _load_auto_launch_state()["launch_attempts"][key]["city_plan"]["CityA"]

    def _mutate(self, manifest, *, reserve, execute, has_claims=False):
        from datetime import datetime, timezone

        from services.action_adapter_launch import LaunchActionAdapter

        with patch(
            "services.action_adapter_launch._require_launch", lambda item: item
        ), patch(
            "services.action_adapter_launch._reserve_manifest_authorization",
            side_effect=reserve,
        ), patch(
            "services.action_adapter_launch._execute_launch_manifest_unchecked",
            side_effect=execute,
        ), patch(
            "services.launch_repository.authorization_has_create_claims",
            side_effect=has_claims,
        ):
            return LaunchActionAdapter().mutate(
                manifest,
                datetime.now(timezone.utc),
                attempt=SimpleNamespace(),
            )

    def test_резервация_упала_маркер_не_пишется_ротация_разрешена(self, clean_state):
        from services.auto_launch import (
            _reconcile_launch_attempt,
            _rotate_poisoned_attempt_key,
        )

        key = self._launching_attempt()

        def reserve_fails(manifest, now):
            raise LaunchCheckBlocked("DUPLICATE_RESERVED", ("слот занят",), "check-1")

        executor = MagicMock()
        # Сбой до провайдера — FAILED без эффекта, а не исключение
        # (исключение уходило в PROVIDER_OUTCOME_UNKNOWN и замораживало остальные города).
        result = self._mutate(self._manifest(key), reserve=reserve_fails, execute=executor)
        assert result.result is ActionResult.FAILED and result.remote_may_have_changed is False
        assert result.reason_code.startswith("LAUNCH_NO_EFFECT_RESERVE:")

        executor.assert_not_called()
        assert "create_started_at" not in self._plan(key)
        with patch("services.auto_launch._fetch_reconciliation_ads", return_value=[]):
            assert _reconcile_launch_attempt(key) == "FAILED_RETRYABLE"
        with patch(
            "services.owner_action_repository.find_proposal_state_by_idempotency_key",
            return_value="FAILED_NO_EFFECT",
        ):
            assert _rotate_poisoned_attempt_key(key) is True

    def test_исполнитель_упал_до_claim_маркер_снимается(self, clean_state):
        from services.auto_launch import (
            _reconcile_launch_attempt,
            _rotate_poisoned_attempt_key,
        )

        key = self._launching_attempt()
        seen_marker_at_provider: list[bool] = []

        def execute_fails_before_claim(manifest, *, attempt, authorization):
            # Маркер обязан стоять durable до первого обращения к провайдеру.
            seen_marker_at_provider.append(bool(self._plan(key).get("create_started_at")))
            raise RuntimeError("медиа не прошло валидацию до claim")

        result = self._mutate(
            self._manifest(key),
            reserve=lambda manifest, now: ProviderLaunchAuthorization("auth-1", "s"),
            execute=execute_fails_before_claim,
            has_claims=lambda auth_id: False,
        )
        # Claim'ов нет → объявлений нет → FAILED без эффекта.
        assert result.result is ActionResult.FAILED and result.remote_may_have_changed is False
        assert result.reason_code.startswith("LAUNCH_NO_EFFECT_PRE_POST:RUNTIMEERROR:")

        assert seen_marker_at_provider == [True]
        assert "create_started_at" not in self._plan(key)
        assert _load_auto_launch_state()["launch_attempts"][key]["errors_by_city"]
        with patch("services.auto_launch._fetch_reconciliation_ads", return_value=[]):
            assert _reconcile_launch_attempt(key) == "FAILED_RETRYABLE"
        with patch(
            "services.owner_action_repository.find_proposal_state_by_idempotency_key",
            return_value="FAILED_NO_EFFECT",
        ):
            assert _rotate_poisoned_attempt_key(key) is True

    def test_исполнитель_упал_после_claim_маркер_остаётся_сверка_блокирует(
        self, clean_state
    ):
        from services.auto_launch import (
            _reconcile_launch_attempt,
            _rotate_poisoned_attempt_key,
        )

        key = self._launching_attempt()

        def execute_fails_after_claim(manifest, *, attempt, authorization):
            raise RuntimeError("Graph POST оборвался после claim")

        with pytest.raises(RuntimeError, match="после claim"):
            self._mutate(
                self._manifest(key),
                reserve=lambda manifest, now: ProviderLaunchAuthorization("auth-1", "s"),
                execute=execute_fails_after_claim,
                has_claims=lambda auth_id: True,
            )

        assert self._plan(key).get("create_started_at")
        with patch(
            "services.auto_launch._fetch_reconciliation_ads", return_value=[]
        ), patch("services.auto_launch._send_telegram"):
            assert _reconcile_launch_attempt(key) == "BLOCKED"
        attempt = _load_auto_launch_state()["launch_attempts"][key]
        assert "после CREATE_STARTED" in attempt["errors_by_city"]["__reconciliation__"]
        with patch(
            "services.owner_action_repository.find_proposal_state_by_idempotency_key",
            return_value="FAILED_NO_EFFECT",
        ):
            assert _rotate_poisoned_attempt_key(key) is False

    def test_бд_недоступна_маркер_остаётся(self, clean_state):
        import sqlite3

        key = self._launching_attempt()

        def db_down(auth_id):
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(RuntimeError, match="исполнитель"):
            self._mutate(
                self._manifest(key),
                reserve=lambda manifest, now: ProviderLaunchAuthorization("auth-1", "s"),
                execute=MagicMock(side_effect=RuntimeError("исполнитель упал")),
                has_claims=db_down,
            )

        assert self._plan(key).get("create_started_at")

    def test_снятие_маркера_не_трогает_город_с_записанными_ad_id(self, clean_state):
        from services.auto_launch import unmark_gateway_create_started

        key = self._launching_attempt()
        state = _load_auto_launch_state()
        attempt = state["launch_attempts"][key]
        attempt["city_plan"]["CityA"]["create_started_at"] = "2026-09-10T09:00:00+05:00"
        attempt["ads_by_city"] = {"CityA": ["ad-real"]}
        _save_auto_launch_state(state)

        unmark_gateway_create_started(self._manifest(key))

        assert self._plan(key)["create_started_at"] == "2026-09-10T09:00:00+05:00"

    def test_окно_нового_плана_от_текущего_времени_а_не_от_started_at(self, clean_state):
        from datetime import datetime, timedelta

        from services.auto_launch import (
            _TZ_LOCAL,
            _parse_fb_time,
            _prepare_city_launch,
            _prepare_launch_attempt,
        )

        key = _prepare_launch_attempt(self._rec(), ["CityA"])
        # Попытка живёт с прошлого месяца: started_at в боевом пути не обновляется.
        state = _load_auto_launch_state()
        state["launch_attempts"][key]["started_at"] = "2026-08-01T10:00:00+05:00"
        _save_auto_launch_state(state)

        before = datetime.now(_TZ_LOCAL)
        _prepare_city_launch(key, "CityA", "adset-1", ["CityA | Маркер [PRODA]"], 1)
        after = datetime.now(_TZ_LOCAL)

        plan = self._plan(key)
        reconcile_from = _parse_fb_time(plan["reconcile_from"])
        reconcile_until = _parse_fb_time(plan["reconcile_until"])
        assert before - timedelta(minutes=5) <= reconcile_from <= after - timedelta(minutes=5)
        assert before + timedelta(hours=2) <= reconcile_until <= after + timedelta(hours=2)

    def test_маркер_привязывает_окно_к_моменту_create(self, clean_state):
        from datetime import datetime, timedelta

        from services.auto_launch import (
            _TZ_LOCAL,
            _mark_city_create_started,
            _parse_fb_time,
            _reconcile_launch_attempt,
        )

        key = self._launching_attempt()
        # Стейджинг был вчера: окно плана давно закрыто, владелец одобряет сегодня.
        yesterday = datetime.now(_TZ_LOCAL) - timedelta(days=1)
        state = _load_auto_launch_state()
        plan = state["launch_attempts"][key]["city_plan"]["CityA"]
        plan["reconcile_from"] = (yesterday - timedelta(minutes=5)).isoformat()
        plan["reconcile_until"] = (yesterday + timedelta(hours=2)).isoformat()
        _save_auto_launch_state(state)

        before = datetime.now(_TZ_LOCAL)
        _mark_city_create_started(key, "CityA", self.SHA)
        after = datetime.now(_TZ_LOCAL)

        plan = self._plan(key)
        first_marker = plan["create_started_at"]
        reconcile_from = _parse_fb_time(plan["reconcile_from"])
        reconcile_until = _parse_fb_time(plan["reconcile_until"])
        assert before - timedelta(minutes=5) <= reconcile_from <= after - timedelta(minutes=5)
        assert before + timedelta(hours=2) <= reconcile_until <= after + timedelta(hours=2)

        # Повторный маркер (retry после настоящего CREATE_STARTED) начало окна
        # не двигает и первый маркер сохраняет.
        _mark_city_create_started(key, "CityA", self.SHA)
        plan = self._plan(key)
        assert plan["create_started_at"] == first_marker
        assert _parse_fb_time(plan["reconcile_from"]) == reconcile_from

        # Объявление, созданное сегодня после маркера, сверка находит.
        ads = [{
            "id": "ad-today",
            "name": "CityA | Маркер [PRODA]",
            "adset_id": "adset-1",
            "created_time": datetime.now(_TZ_LOCAL).isoformat(),
        }]
        with patch("services.auto_launch._fetch_reconciliation_ads", return_value=ads):
            assert _reconcile_launch_attempt(key) == "SUCCEEDED"
        assert _load_auto_launch_state()["launch_attempts"][key]["ads_by_city"] == {
            "CityA": ["ad-today"]
        }


class TestInFlightAttemptNotReconciled:
    """Попытка с живым предложением исполняется гейтвеем, прогон её не сверяет.

    Дефект: цепочка из 6 объявлений исполняется по одному claim за
    тик (30 мин), попытка часами стоит в LAUNCHING. Утренний прогон счёл её
    «зависшей после падения», сверил по инвентарю (2 из 6 созданы) → BLOCKED /
    FAILED_RETRYABLE, следующий claim упал в маркере CREATE → RECONCILE_REQUIRED,
    хвост цепочки не исполнился.
    """

    SHA = "b" * 64
    IN_FLIGHT = "services.owner_action_repository.proposal_in_flight_by_idempotency_key"

    def _rec(self):
        return {
            "card_id": "card-chain",
            "card_name": "Цепочка из шести",
            "campaign_type": "leadgen",
            "cities": ["CityA"],
        }

    def _launching_attempt(self):
        from services.auto_launch import _prepare_city_launch, _prepare_launch_attempt

        with patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "account-1"),
        ):
            key = _prepare_launch_attempt(self._rec(), ["CityA"])
        _prepare_city_launch(
            key,
            "CityA",
            "adset-1",
            ["CityA | Цепочка / 1 [PRODA]", "CityA | Цепочка / 2 [PRODA]"],
            2,
            media_manifest_sha256=self.SHA,
        )
        return key

    def _attempt(self, key):
        return _load_auto_launch_state()["launch_attempts"][key]

    def test_живое_задание_не_сверяется_и_фаза_не_меняется(self, clean_state):
        from services.auto_launch import (
            _mark_city_create_started,
            _reconcile_pending_cards,
        )

        key = self._launching_attempt()
        # Первый claim уже создал объявление: маркер стоит, ad_id ещё не записан.
        _mark_city_create_started(key, "CityA", self.SHA)
        fetch = MagicMock(return_value=[])

        with patch(self.IN_FLIGHT, return_value=True) as in_flight, patch(
            "services.auto_launch._fetch_reconciliation_ads", fetch
        ), patch("services.auto_launch._send_telegram") as telegram:
            state = _reconcile_pending_cards([{"id": "card-chain"}], _load_auto_launch_state())

        in_flight.assert_called_once_with(self._attempt(key)["idempotency_key"])
        fetch.assert_not_called()
        telegram.assert_not_called()
        assert state["launch_attempts"][key]["phase"] == "LAUNCHING"
        assert self._attempt(key)["phase"] == "LAUNCHING"
        assert "__reconciliation__" not in self._attempt(key).get("errors_by_city", {})
        # Следующий claim цепочки ставит маркер без ошибки.
        _mark_city_create_started(key, "CityA", self.SHA)

    def test_после_падения_процесса_сверка_работает_как_раньше(self, clean_state):
        from services.auto_launch import _reconcile_pending_cards

        key = self._launching_attempt()

        with patch(self.IN_FLIGHT, return_value=False), patch(
            "services.auto_launch._fetch_reconciliation_ads", return_value=[]
        ):
            _reconcile_pending_cards([{"id": "card-chain"}], _load_auto_launch_state())

        attempt = self._attempt(key)
        assert attempt["phase"] == "FAILED_RETRYABLE"
        assert "CREATE не начинался" in attempt["errors_by_city"]["CityA"]

    def test_бд_недоступна_сверка_не_выполняется(self, clean_state):
        import sqlite3

        from services.auto_launch import _reconcile_pending_cards

        key = self._launching_attempt()
        fetch = MagicMock(return_value=[])

        with patch(
            self.IN_FLIGHT, side_effect=sqlite3.OperationalError("database is locked")
        ), patch("services.auto_launch._fetch_reconciliation_ads", fetch):
            _reconcile_pending_cards([{"id": "card-chain"}], _load_auto_launch_state())

        fetch.assert_not_called()
        assert self._attempt(key)["phase"] == "LAUNCHING"

    def test_чужая_карточка_в_прогоне_не_проверяется_по_бд(self, clean_state):
        from services.auto_launch import _reconcile_pending_cards

        key = self._launching_attempt()

        with patch(self.IN_FLIGHT) as in_flight:
            _reconcile_pending_cards([{"id": "card-other"}], _load_auto_launch_state())

        in_flight.assert_not_called()
        assert self._attempt(key)["phase"] == "LAUNCHING"

    def test_маркер_поднимает_failed_retryable_в_launching(self, clean_state):
        from services.auto_launch import _mark_city_create_started

        key = self._launching_attempt()
        state = _load_auto_launch_state()
        attempt = state["launch_attempts"][key]
        attempt["phase"] = "FAILED_RETRYABLE"
        attempt["errors_by_city"] = {
            "CityA": "reconciliation: CREATE не начинался, объявления не найдены"
        }
        _save_auto_launch_state(state)

        _mark_city_create_started(key, "CityA", self.SHA)

        attempt = self._attempt(key)
        assert attempt["phase"] == "LAUNCHING"
        assert attempt["city_plan"]["CityA"]["create_started_at"]
        assert "CityA" not in attempt["errors_by_city"]

    def test_маркер_поднимает_partial_в_launching(self, clean_state):
        from services.auto_launch import _mark_city_create_started

        key = self._launching_attempt()
        state = _load_auto_launch_state()
        state["launch_attempts"][key]["phase"] = "PARTIAL"
        _save_auto_launch_state(state)

        _mark_city_create_started(key, "CityA", self.SHA)

        assert self._attempt(key)["phase"] == "LAUNCHING"

    def test_маркер_отвергает_blocked_и_reconciling(self, clean_state):
        from services.auto_launch import _mark_city_create_started

        key = self._launching_attempt()
        for phase in ("BLOCKED", "RECONCILING"):
            state = _load_auto_launch_state()
            state["launch_attempts"][key]["phase"] = phase
            _save_auto_launch_state(state)
            with pytest.raises(RuntimeError, match="требует LAUNCHING"):
                _mark_city_create_started(key, "CityA", self.SHA)
            assert self._attempt(key)["phase"] == phase

    def test_прямое_исполнение_не_сверяет_и_не_запускает_при_живом_предложении(
        self, clean_state
    ):
        from services.auto_launch import _execute_launch

        key = self._launching_attempt()
        fetch = MagicMock(return_value=[])
        second_launch = MagicMock()

        with patch(self.IN_FLIGHT, return_value=True), patch(
            "services.auto_launch._resolve_launch_account",
            return_value=("offline", "account-1"),
        ), patch("services.auto_launch._fetch_reconciliation_ads", fetch), patch(
            "agent.launcher.launch_single", second_launch
        ), pytest.raises(RuntimeError, match="исполняется гейтвеем"):
            _execute_launch(self._rec(), _load_auto_launch_state(), "2026-09-11")

        fetch.assert_not_called()
        second_launch.assert_not_called()
        assert self._attempt(key)["phase"] == "LAUNCHING"
