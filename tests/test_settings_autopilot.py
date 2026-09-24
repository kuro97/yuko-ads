"""
Тесты персистентности полей autopilot через POST /api/settings.

Проверяем:
- launch_enabled и max_launches_per_day сохраняются и возвращаются
- kill_switch и min_days_protect сохраняются
- Существующие поля (enabled, mode, thresholds) не теряются при мерже
- get_autopilot_config читает launch_enabled из сохранённого конфига
"""

import sys
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

# Добавляем корень проекта в path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта web.app
_google_mock = MagicMock()
_genai_mock = MagicMock()
_genai_types_mock = MagicMock()
sys.modules.setdefault("google", _google_mock)
sys.modules.setdefault("google.genai", _genai_mock)
sys.modules.setdefault("google.genai.types", _genai_types_mock)
_google_mock.genai = _genai_mock

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from agent.database import init_db  # noqa: E402
from web.app import app  # noqa: E402

API_KEY = "test-secret-key"
HEADERS = {"X-API-Key": API_KEY}


@pytest.fixture
def client(tmp_path):
    """TestClient с временной БД и изолированным settings.json."""
    db_path = str(tmp_path / "test.db")
    init_db(db_path=db_path, json_path=str(tmp_path / "nonexistent.json"))

    # Перенаправляем SETTINGS_FILE на tmp_path, чтобы не трогать прод-файл
    settings_path = tmp_path / "settings.json"
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        yield TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# launch_enabled — главный кейс
# ---------------------------------------------------------------------------

def test_launch_enabled_persists(client):
    """POST autopilot.launch_enabled=true → GET возвращает launch_enabled=true."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"launch_enabled": True}},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["autopilot"]["launch_enabled"] is True

    # Повторный GET должен тоже вернуть launch_enabled=true
    get_resp = client.get("/api/settings", headers=HEADERS)
    assert get_resp.status_code == 200
    assert get_resp.json()["autopilot"]["launch_enabled"] is True


def test_launch_enabled_false_by_default(client):
    """По умолчанию launch_enabled=false (не включается без явного запроса)."""
    get_resp = client.get("/api/settings", headers=HEADERS)
    assert get_resp.status_code == 200
    # Дефолт: поле может отсутствовать или быть False
    ap = get_resp.json().get("autopilot") or {}
    assert ap.get("launch_enabled", False) is False


# ---------------------------------------------------------------------------
# max_launches_per_day
# ---------------------------------------------------------------------------

def test_max_launches_per_day_persists(client):
    """POST autopilot.max_launches_per_day=3 → сохраняется и возвращается."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"max_launches_per_day": 3}},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["autopilot"]["max_launches_per_day"] == 3

    get_resp = client.get("/api/settings", headers=HEADERS)
    assert get_resp.json()["autopilot"]["max_launches_per_day"] == 3


def test_max_launches_per_day_validation(client):
    """max_launches_per_day вне диапазона [1, 10] → 400."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"max_launches_per_day": 0}},
        headers=HEADERS,
    )
    assert resp.status_code == 400

    resp2 = client.post(
        "/api/settings",
        json={"autopilot": {"max_launches_per_day": 11}},
        headers=HEADERS,
    )
    assert resp2.status_code == 400


# ---------------------------------------------------------------------------
# kill_switch и min_days_protect
# ---------------------------------------------------------------------------

def test_kill_switch_persists(client):
    """POST autopilot.kill_switch=true → сохраняется."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"kill_switch": True}},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["autopilot"]["kill_switch"] is True


def test_min_days_protect_persists(client):
    """POST autopilot.min_days_protect=7 → сохраняется."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"min_days_protect": 7}},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["autopilot"]["min_days_protect"] == 7


# ---------------------------------------------------------------------------
# Мерж: существующие поля не теряются
# ---------------------------------------------------------------------------

def test_merge_preserves_existing_fields(client):
    """Установка launch_enabled не затирает уже сохранённые enabled/mode."""
    # Сначала устанавливаем enabled и mode
    client.post(
        "/api/settings",
        json={"autopilot": {"enabled": True, "mode": "active"}},
        headers=HEADERS,
    )

    # Потом добавляем launch_enabled
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"launch_enabled": True}},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    ap = resp.json()["autopilot"]
    # Старые поля должны остаться
    assert ap["enabled"] is True
    assert ap["mode"] == "active"
    # Новое поле добавилось
    assert ap["launch_enabled"] is True


def test_merge_preserves_thresholds(client):
    """Обновление autopilot не удаляет thresholds из settings."""
    # max_cpl входит в DEFAULT_THRESHOLDS → будет сохранён
    client.post(
        "/api/settings/thresholds",
        json={"max_cpl": 55.0},
        headers=HEADERS,
    )

    # Потом меняем autopilot
    client.post(
        "/api/settings",
        json={"autopilot": {"launch_enabled": True}},
        headers=HEADERS,
    )

    # Пороги должны остаться
    get_resp = client.get("/api/settings", headers=HEADERS)
    assert get_resp.json().get("thresholds", {}).get("max_cpl") == 55.0


# ---------------------------------------------------------------------------
# get_autopilot_config читает launch_enabled из файла
# ---------------------------------------------------------------------------

def test_get_autopilot_config_reads_launch_enabled(tmp_path):
    """get_autopilot_config подхватывает launch_enabled=true из settings.json."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"autopilot": {"launch_enabled": True, "max_launches_per_day": 2}}),
        encoding="utf-8",
    )

    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config
        cfg = get_autopilot_config()

    assert cfg["launch_enabled"] is True
    assert cfg["max_launches_per_day"] == 2


def test_get_autopilot_config_default_launch_disabled(tmp_path):
    """Без записи в settings.json launch_enabled дефолтно False."""
    # Файл не существует — чистые дефолты
    settings_path = tmp_path / "settings_empty.json"
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config
        cfg = get_autopilot_config()

    assert cfg["launch_enabled"] is False


# ---------------------------------------------------------------------------
# launch_checker — server-owned mode и глубокий merge
# ---------------------------------------------------------------------------


def test_get_autopilot_config_defaults_launch_checker_to_observe(tmp_path):
    settings_path = tmp_path / "settings_empty.json"
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config

        cfg = get_autopilot_config()

    assert cfg["launch_checker"] == {"mode": "observe"}


def test_get_autopilot_config_deep_merges_partial_launch_checker(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"autopilot": {"launch_checker": {}}}),
        encoding="utf-8",
    )
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config

        cfg = get_autopilot_config()

    assert cfg["launch_checker"] == {"mode": "observe"}


def test_get_autopilot_config_rejects_unsafe_file_mode_to_observe(tmp_path):
    """Ручная правка settings.json не превращает неизвестный mode в bypass."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "autopilot": {
                    "launch_checker": {"mode": "active", "unknown": True}
                }
            }
        ),
        encoding="utf-8",
    )
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config

        cfg = get_autopilot_config()

    assert cfg["launch_checker"] == {"mode": "observe"}


def test_launch_checker_mode_persists_through_settings_api(client):
    response = client.post(
        "/api/settings",
        json={"autopilot": {"launch_checker": {"mode": "enforce"}}},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.text
    assert response.json()["autopilot"]["launch_checker"] == {"mode": "enforce"}
    assert client.get("/api/settings", headers=HEADERS).json()["autopilot"][
        "launch_checker"
    ] == {"mode": "enforce"}


@pytest.mark.parametrize(
    "launch_checker",
    [
        {"mode": "active"},
        {"unknown": True},
        "observe",
    ],
)
def test_launch_checker_invalid_settings_api_returns_400(client, launch_checker):
    response = client.post(
        "/api/settings",
        json={"autopilot": {"launch_checker": launch_checker}},
        headers=HEADERS,
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Budget Scaler — scale_enabled и числовые лимиты
# ---------------------------------------------------------------------------

def test_scale_enabled_persists(client):
    """POST autopilot.scale_enabled=true → GET показывает scale_enabled=true."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"scale_enabled": True}},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["autopilot"]["scale_enabled"] is True

    get_resp = client.get("/api/settings", headers=HEADERS)
    assert get_resp.status_code == 200
    assert get_resp.json()["autopilot"]["scale_enabled"] is True


def test_scale_enabled_false_by_default(client):
    """По умолчанию scale_enabled=false (не включается без явного запроса)."""
    get_resp = client.get("/api/settings", headers=HEADERS)
    assert get_resp.status_code == 200
    ap = get_resp.json().get("autopilot") or {}
    assert ap.get("scale_enabled", False) is False


def test_max_budget_increase_pct_persists(client):
    """POST autopilot.max_budget_increase_pct=30 → сохраняется и возвращается."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"max_budget_increase_pct": 30}},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["autopilot"]["max_budget_increase_pct"] == 30

    get_resp = client.get("/api/settings", headers=HEADERS)
    assert get_resp.json()["autopilot"]["max_budget_increase_pct"] == 30


def test_max_budget_increase_pct_validation(client):
    """max_budget_increase_pct вне диапазона [1, 100] → 400."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"max_budget_increase_pct": 0}},
        headers=HEADERS,
    )
    assert resp.status_code == 400

    resp2 = client.post(
        "/api/settings",
        json={"autopilot": {"max_budget_increase_pct": 101}},
        headers=HEADERS,
    )
    assert resp2.status_code == 400


def test_max_adset_budget_mult_persists(client):
    """POST autopilot.max_adset_budget_mult=3.0 → сохраняется."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"max_adset_budget_mult": 3.0}},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["autopilot"]["max_adset_budget_mult"] == 3.0


def test_max_adset_budget_mult_validation(client):
    """max_adset_budget_mult вне диапазона [1.0, 5.0] → 400."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"max_adset_budget_mult": 0.5}},
        headers=HEADERS,
    )
    assert resp.status_code == 400

    resp2 = client.post(
        "/api/settings",
        json={"autopilot": {"max_adset_budget_mult": 6.0}},
        headers=HEADERS,
    )
    assert resp2.status_code == 400


def test_scale_keys_persist_together(client):
    """Все scale-ключи одновременно сохраняются и не затирают launch-ключи."""
    # Сначала устанавливаем launch_enabled
    client.post(
        "/api/settings",
        json={"autopilot": {"launch_enabled": True, "max_launches_per_day": 2}},
        headers=HEADERS,
    )

    # Затем устанавливаем scale-ключи
    resp = client.post(
        "/api/settings",
        json={"autopilot": {
            "scale_enabled": True,
            "max_budget_increase_pct": 25,
            "max_adset_budget_mult": 2.5,
            "max_adset_daily_budget": 500,
            "max_total_daily_budget": 5000,
            "max_scales_per_run": 3,
        }},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    ap = resp.json()["autopilot"]

    # Scale-ключи записались
    assert ap["scale_enabled"] is True
    assert ap["max_budget_increase_pct"] == 25
    assert ap["max_adset_budget_mult"] == 2.5
    assert ap["max_adset_daily_budget"] == 500
    assert ap["max_total_daily_budget"] == 5000
    assert ap["max_scales_per_run"] == 3

    # Launch-ключи не потерялись
    assert ap["launch_enabled"] is True
    assert ap["max_launches_per_day"] == 2


def test_get_autopilot_config_reads_scale_enabled(tmp_path):
    """get_autopilot_config подхватывает scale_enabled=true из settings.json."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"autopilot": {"scale_enabled": True, "max_budget_increase_pct": 40}}),
        encoding="utf-8",
    )

    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config
        cfg = get_autopilot_config()

    assert cfg["scale_enabled"] is True
    assert cfg["max_budget_increase_pct"] == 40


def test_scale_enabled_default_false_in_config(tmp_path):
    """Без записи в settings.json scale_enabled дефолтно False."""
    settings_path = tmp_path / "settings_empty2.json"
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config
        cfg = get_autopilot_config()

    assert cfg["scale_enabled"] is False


# ---------------------------------------------------------------------------
# Adset Cleaner — вложенный блок autopilot.cleaner
# ---------------------------------------------------------------------------

def test_settings_принимает_cleaner_блок(client):
    """POST autopilot.cleaner={enabled:true,...} → 200, конфиг сохранён,
    get_autopilot_config видит cleaner."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": {
            "enabled": True,
            "dry_run": False,
            "proactive_enabled": True,
            "stale_days": 15,
            "adset_threshold": 45,
            "target_free": 5,
            "critical_free_slots": 2,
            "hard_reserve_slots": 1,
            "allow_irreversible_delete": False,
            "max_manifest_candidates_per_adset": 10,
            "max_deletes_per_workflow": 2,
            "alert_dedup_hours": 6,
            "managed_account_kinds": ["offline"],
        }}},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    cleaner = resp.json()["autopilot"]["cleaner"]
    assert cleaner["enabled"] is True
    assert cleaner["dry_run"] is False
    assert cleaner["stale_days"] == 15
    assert cleaner["adset_threshold"] == 45
    assert cleaner["target_free"] == 5
    assert cleaner["allow_irreversible_delete"] is False

    get_resp = client.get("/api/settings", headers=HEADERS)
    assert get_resp.json()["autopilot"]["cleaner"]["enabled"] is True


def test_settings_cleaner_неизвестный_ключ_400(client):
    """POST autopilot.cleaner={foo:1} (неизвестный ключ) → 400."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": {"foo": 1}}},
        headers=HEADERS,
    )
    assert resp.status_code == 400


def test_settings_cleaner_не_объект_400(client):
    """POST autopilot.cleaner=не объект → 400."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": "oops"}},
        headers=HEADERS,
    )
    assert resp.status_code == 400


@pytest.mark.parametrize("field,low,high", [
    ("stale_days", 15, 365),
    ("hard_reserve_slots", 1, 5),
    ("max_manifest_candidates_per_adset", 1, 10),
    ("max_deletes_per_workflow", 1, 5),
    ("alert_dedup_hours", 1, 24),
])
def test_settings_cleaner_границы_полей(client, field, low, high):
    """Новые safety-границы cleaner отклоняют значения вне контракта."""
    # Ниже нижней границы
    resp_low = client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": {field: low - 1}}},
        headers=HEADERS,
    )
    assert resp_low.status_code == 400, f"{field}={low - 1} должен быть отвергнут"

    # Выше верхней границы
    resp_high = client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": {field: high + 1}}},
        headers=HEADERS,
    )
    assert resp_high.status_code == 400, f"{field}={high + 1} должен быть отвергнут"

    # На границах — валидно
    resp_ok_low = client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": {field: low}}},
        headers=HEADERS,
    )
    assert resp_ok_low.status_code == 200, resp_ok_low.text
    assert resp_ok_low.json()["autopilot"]["cleaner"][field] == low

    resp_ok_high = client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": {field: high}}},
        headers=HEADERS,
    )
    assert resp_ok_high.status_code == 200, resp_ok_high.text
    assert resp_ok_high.json()["autopilot"]["cleaner"][field] == high


@pytest.mark.parametrize(
    "payload",
    [
        {"target_free": 1, "adset_threshold": 49, "critical_free_slots": 0},
        {"target_free": 10, "adset_threshold": 40, "critical_free_slots": 10},
    ],
)
def test_settings_cleaner_threshold_target_границы(client, payload):
    response = client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": payload}},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.text
    cleaner = response.json()["autopilot"]["cleaner"]
    assert cleaner["adset_threshold"] == 50 - cleaner["target_free"]
    assert cleaner["critical_free_slots"] <= cleaner["target_free"]


def test_settings_cleaner_legacy_max_deletes_per_run_400(client):
    response = client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": {"max_deletes_per_run": 1}}},
        headers=HEADERS,
    )

    assert response.status_code == 400


@pytest.mark.parametrize(
    "field",
    ["enabled", "dry_run", "proactive_enabled", "allow_irreversible_delete"],
)
@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
def test_settings_cleaner_safety_bool_строгий_json(client, field, value):
    response = client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": {field: value}}},
        headers=HEADERS,
    )

    assert response.status_code == 400


def test_settings_replacement_roundtrip_disabled_and_deep_merge(client):
    first = client.post(
        "/api/settings",
        json={"autopilot": {"replacement": {
            "enabled": False,
            "verify_interval_minutes": 30,
            "max_pending_hours": 48,
        }}},
        headers=HEADERS,
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/settings",
        json={"autopilot": {"replacement": {"max_pending_hours": 72}}},
        headers=HEADERS,
    )
    assert second.status_code == 200, second.text
    replacement = second.json()["autopilot"]["replacement"]
    assert replacement == {
        "enabled": False,
        "verify_interval_minutes": 30,
        "max_pending_hours": 72,
    }
    assert client.get("/api/settings", headers=HEADERS).json()["autopilot"][
        "replacement"
    ] == replacement


@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
def test_settings_replacement_enabled_строгий_json(client, value):
    response = client.post(
        "/api/settings",
        json={"autopilot": {"replacement": {"enabled": value}}},
        headers=HEADERS,
    )

    assert response.status_code == 400


def test_settings_cleaner_мерж_не_теряет_поля_autopilot(client):
    """Установка cleaner-блока не затирает уже сохранённые верхнеуровневые
    поля autopilot (enabled и т.д.)."""
    client.post(
        "/api/settings",
        json={"autopilot": {"enabled": True, "mode": "active"}},
        headers=HEADERS,
    )

    resp = client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": {"enabled": True}}},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    ap = resp.json()["autopilot"]
    assert ap["enabled"] is True
    assert ap["mode"] == "active"
    assert ap["cleaner"]["enabled"] is True


def test_settings_cleaner_частичный_мерж_сохраняет_остальные_поля_cleaner(client):
    """Повторный POST только с одним полем cleaner не стирает остальные
    ранее сохранённые поля cleaner-блока (мерж поверх существующего)."""
    client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": {"enabled": True, "stale_days": 45}}},
        headers=HEADERS,
    )

    resp = client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": {"dry_run": False}}},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    cleaner = resp.json()["autopilot"]["cleaner"]
    assert cleaner["enabled"] is True
    assert cleaner["stale_days"] == 45
    assert cleaner["dry_run"] is False


def test_get_autopilot_config_default_cleaner_disabled(tmp_path):
    """Без записи в settings.json cleaner дефолтно enabled=false, dry_run=true."""
    settings_path = tmp_path / "settings_empty_cleaner.json"
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config
        cfg = get_autopilot_config()

    cleaner = cfg["cleaner"]
    assert cleaner["enabled"] is False
    assert cleaner["dry_run"] is True
    assert cleaner["stale_days"] == 15
    assert cleaner["adset_threshold"] == 45
    assert cleaner["target_free"] == 5
    assert cleaner["critical_free_slots"] == 2
    assert cleaner["hard_reserve_slots"] == 1
    assert cleaner["allow_irreversible_delete"] is False
    assert cleaner["max_manifest_candidates_per_adset"] == 10
    assert cleaner["max_deletes_per_workflow"] == 2
    assert cleaner["managed_account_kinds"] == ["offline"]
    assert "max_deletes_per_run" not in cleaner


def test_get_autopilot_config_deep_merges_cleaner_and_replacement_defaults(tmp_path):
    settings_path = tmp_path / "settings_partial_replacement.json"
    settings_path.write_text(
        json.dumps({
            "autopilot": {
                "cleaner": {"enabled": True},
                "replacement": {"max_pending_hours": 72},
            }
        }),
        encoding="utf-8",
    )
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config

        config = get_autopilot_config()

    assert config["cleaner"]["enabled"] is True
    assert config["cleaner"]["dry_run"] is True
    assert config["cleaner"]["target_free"] == 5
    assert config["cleaner"]["allow_irreversible_delete"] is False
    assert config["replacement"] == {
        "enabled": False,
        "verify_interval_minutes": 30,
        "max_pending_hours": 72,
    }


def test_settings_recovery_roundtrip_and_deep_merge(client):
    first = client.post(
        "/api/settings",
        json={"autopilot": {"recovery": {
            "enabled": False,
            "since": "2026-07-02T00:00:00+05:00",
            "max_cards_per_day": 1,
            "managed_account_kinds": ["offline", "online"],
        }}},
        headers=HEADERS,
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/settings",
        json={"autopilot": {"recovery": {"max_cards_per_day": 2}}},
        headers=HEADERS,
    )
    assert second.status_code == 200, second.text
    recovery = second.json()["autopilot"]["recovery"]
    assert recovery == {
        "enabled": False,
        "since": "2026-07-02T00:00:00+05:00",
        "max_cards_per_day": 2,
        "managed_account_kinds": ["offline", "online"],
    }
    assert client.get("/api/settings", headers=HEADERS).json()["autopilot"][
        "recovery"
    ] == recovery


def test_get_autopilot_config_recovery_defaults(tmp_path):
    settings_path = tmp_path / "settings_empty_recovery.json"
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config

        recovery = get_autopilot_config()["recovery"]

    assert recovery == {
        "enabled": False,
        "since": "2026-07-01T00:00:00+05:00",
        "max_cards_per_day": 1,
        "managed_account_kinds": ["offline", "online"],
    }


def test_get_autopilot_config_kill_switch_dominates_mutation_flags(tmp_path):
    settings_path = tmp_path / "settings_killed.json"
    settings_path.write_text(
        json.dumps({
            "autopilot": {
                "kill_switch": True,
                "cleaner": {
                    "enabled": True,
                    "dry_run": False,
                    "proactive_enabled": True,
                    "allow_irreversible_delete": True,
                },
                "replacement": {"enabled": True},
                "recovery": {"enabled": True},
            }
        }),
        encoding="utf-8",
    )
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config

        config = get_autopilot_config()

    assert config["cleaner"]["enabled"] is False
    assert config["cleaner"]["dry_run"] is True
    assert config["cleaner"]["proactive_enabled"] is True
    assert config["cleaner"]["allow_irreversible_delete"] is False
    assert config["replacement"]["enabled"] is False
    assert config["recovery"]["enabled"] is False


def test_get_autopilot_config_drops_legacy_cleaner_delete_cap(tmp_path):
    settings_path = tmp_path / "settings_legacy_cleaner.json"
    settings_path.write_text(
        json.dumps({"autopilot": {"cleaner": {"max_deletes_per_run": 10}}}),
        encoding="utf-8",
    )
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config

        cleaner = get_autopilot_config()["cleaner"]

    assert "max_deletes_per_run" not in cleaner
    assert cleaner["max_deletes_per_workflow"] == 2


def test_get_autopilot_config_invalid_nested_blocks_fall_back_safe(tmp_path):
    settings_path = tmp_path / "settings_invalid_nested.json"
    settings_path.write_text(
        json.dumps({
            "autopilot": {
                "cleaner": "active",
                "replacement": ["enabled"],
                "recovery": True,
            }
        }),
        encoding="utf-8",
    )
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config

        config = get_autopilot_config()

    assert config["cleaner"]["enabled"] is False
    assert config["cleaner"]["dry_run"] is True
    assert config["replacement"]["enabled"] is False
    assert config["recovery"]["enabled"] is False


# ---------------------------------------------------------------------------
# Guardian (Страж) — вложенный блок autopilot.guardian (ARCH-phase1-guardian §6.1)
# ---------------------------------------------------------------------------

def test_settings_принимает_guardian_блок(client):
    """POST autopilot.guardian={...} валидный → 200, все поля сохранены."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"guardian": {
            "enabled": True,
            "early_dry_run": False,
            "early_min_age_hours": 48,
            "early_min_spend": 20.0,
            "early_day1_zero_spend": 30.0,
            "early_cpl_mult": 4.0,
            "early_day23_min_spend": 25.0,
            "wnc_dry_run": False,
            "wnc_min_spend": 200.0,
            "wnc_min_leads": 15,
            "wnc_min_days": 5,
        }}},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    guardian = resp.json()["autopilot"]["guardian"]
    assert guardian["enabled"] is True
    assert guardian["early_dry_run"] is False
    assert guardian["early_min_age_hours"] == 48
    assert guardian["early_min_spend"] == 20.0
    assert guardian["early_day1_zero_spend"] == 30.0
    assert guardian["early_cpl_mult"] == 4.0
    assert guardian["early_day23_min_spend"] == 25.0
    assert guardian["wnc_dry_run"] is False
    assert guardian["wnc_min_spend"] == 200.0
    assert guardian["wnc_min_leads"] == 15
    assert guardian["wnc_min_days"] == 5

    get_resp = client.get("/api/settings", headers=HEADERS)
    assert get_resp.json()["autopilot"]["guardian"]["enabled"] is True


def test_settings_guardian_неизвестный_ключ_400(client):
    """POST autopilot.guardian={foo:1} (неизвестный под-ключ) → 400."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"guardian": {"foo": 1}}},
        headers=HEADERS,
    )
    assert resp.status_code == 400


def test_settings_guardian_не_объект_400(client):
    """POST autopilot.guardian=не объект → 400."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"guardian": "oops"}},
        headers=HEADERS,
    )
    assert resp.status_code == 400


@pytest.mark.parametrize("field,low,high", [
    ("early_min_age_hours", 1, 168),
    ("wnc_min_leads", 1, 1000),
    ("wnc_min_days", 1, 30),
])
def test_settings_guardian_int_границы_полей(client, field, low, high):
    """Границы валидации int-полей: early_min_age_hours [1,168],
    wnc_min_leads [1,1000], wnc_min_days [1,30] — вне диапазона → 400, внутри → 200."""
    resp_low = client.post(
        "/api/settings",
        json={"autopilot": {"guardian": {field: low - 1}}},
        headers=HEADERS,
    )
    assert resp_low.status_code == 400, f"{field}={low - 1} должен быть отвергнут"

    resp_high = client.post(
        "/api/settings",
        json={"autopilot": {"guardian": {field: high + 1}}},
        headers=HEADERS,
    )
    assert resp_high.status_code == 400, f"{field}={high + 1} должен быть отвергнут"

    resp_ok_low = client.post(
        "/api/settings",
        json={"autopilot": {"guardian": {field: low}}},
        headers=HEADERS,
    )
    assert resp_ok_low.status_code == 200, resp_ok_low.text
    assert resp_ok_low.json()["autopilot"]["guardian"][field] == low

    resp_ok_high = client.post(
        "/api/settings",
        json={"autopilot": {"guardian": {field: high}}},
        headers=HEADERS,
    )
    assert resp_ok_high.status_code == 200, resp_ok_high.text
    assert resp_ok_high.json()["autopilot"]["guardian"][field] == high


@pytest.mark.parametrize("field", [
    "early_min_spend", "early_day1_zero_spend", "early_cpl_mult",
    "early_day23_min_spend", "wnc_min_spend",
])
def test_settings_guardian_float_отрицательное_значение_400(client, field):
    """Float-пороги guardian (early_min_spend и др.) должны быть >= 0 → отрицательное 400."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"guardian": {field: -1.0}}},
        headers=HEADERS,
    )
    assert resp.status_code == 400, f"{field}=-1.0 должен быть отвергнут"

    resp_ok = client.post(
        "/api/settings",
        json={"autopilot": {"guardian": {field: 0.0}}},
        headers=HEADERS,
    )
    assert resp_ok.status_code == 200, resp_ok.text
    assert resp_ok.json()["autopilot"]["guardian"][field] == 0.0


def test_settings_guardian_мерж_не_теряет_поля_autopilot(client):
    """Установка guardian-блока не затирает уже сохранённые верхнеуровневые
    поля autopilot (enabled, mode) и соседний блок cleaner."""
    client.post(
        "/api/settings",
        json={"autopilot": {"enabled": True, "mode": "active"}},
        headers=HEADERS,
    )
    client.post(
        "/api/settings",
        json={"autopilot": {"cleaner": {"enabled": True}}},
        headers=HEADERS,
    )

    resp = client.post(
        "/api/settings",
        json={"autopilot": {"guardian": {"enabled": True}}},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    ap = resp.json()["autopilot"]
    assert ap["enabled"] is True
    assert ap["mode"] == "active"
    assert ap["cleaner"]["enabled"] is True
    assert ap["guardian"]["enabled"] is True


def test_settings_guardian_частичный_мерж_сохраняет_остальные_поля(client):
    """Повторный POST только с одним полем guardian не стирает остальные
    ранее сохранённые под-поля guardian-блока (мерж поверх существующего,
    как в cleaner)."""
    client.post(
        "/api/settings",
        json={"autopilot": {"guardian": {"enabled": True, "wnc_min_spend": 250.0}}},
        headers=HEADERS,
    )

    resp = client.post(
        "/api/settings",
        json={"autopilot": {"guardian": {"early_dry_run": False}}},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    guardian = resp.json()["autopilot"]["guardian"]
    assert guardian["enabled"] is True
    assert guardian["wnc_min_spend"] == 250.0
    assert guardian["early_dry_run"] is False


def test_get_autopilot_config_default_guardian_дефолты(tmp_path):
    """Без записи в settings.json guardian дефолтно = GUARDIAN_DEFAULTS
    (enabled=false, dry_run-флаги=true, пороги как в services.guardian)."""
    settings_path = tmp_path / "settings_empty_guardian.json"
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config
        cfg = get_autopilot_config()

    guardian = cfg["guardian"]
    assert guardian["enabled"] is False
    assert guardian["early_dry_run"] is True
    assert guardian["wnc_dry_run"] is True
    assert guardian["early_min_age_hours"] == 24
    assert guardian["early_min_spend"] == 15.0
    assert guardian["wnc_min_spend"] == 150.0
    assert guardian["wnc_min_leads"] == 10
    assert guardian["wnc_min_days"] == 3


def test_settings_autopilot_неизвестное_верхнеуровневое_поле_400(client):
    """POST autopilot с неизвестным верхнеуровневым полем (не guardian/cleaner) → 400
    (регресс-проверка: добавление 'guardian' в _ALLOWED_AP_KEYS не открыло дыру)."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"totally_unknown_field": 1}},
        headers=HEADERS,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Budget Scaler v2 (autopilot.scaler_v2) — HTTP roundtrip через POST/GET /api/settings
# (по итогам ревью).
# ---------------------------------------------------------------------------


def test_scaler_v2_http_roundtrip_persists(client):
    """POST autopilot.scaler_v2 → GET возвращает тот же блок без потерь."""
    resp = client.post(
        "/api/settings",
        json={
            "autopilot": {
                "scaler_v2": {
                    "waster_min_spend_usd": 20.0,
                    "require_fresh_7d": True,
                    "sanity_cap_ratio": 0.6,
                }
            }
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    v2 = resp.json()["autopilot"]["scaler_v2"]
    assert v2["waster_min_spend_usd"] == 20.0
    assert v2["require_fresh_7d"] is True
    assert v2["sanity_cap_ratio"] == 0.6

    get_resp = client.get("/api/settings", headers=HEADERS)
    assert get_resp.status_code == 200
    v2_get = get_resp.json()["autopilot"]["scaler_v2"]
    assert v2_get["waster_min_spend_usd"] == 20.0
    assert v2_get["require_fresh_7d"] is True
    assert v2_get["sanity_cap_ratio"] == 0.6


def test_scaler_v2_http_roundtrip_deep_merge_preserves_siblings(client):
    """Два последовательных POST: первый пишет cdp+scaler_v2, второй обновляет
    один под-ключ scaler_v2 → соседние блоки (cdp) и под-ключи не теряются."""
    r1 = client.post(
        "/api/settings",
        json={
            "autopilot": {
                "cdp": {"enabled": True, "payments_source": "shadow"},
                "scaler_v2": {"waster_min_spend_usd": 30.0, "engine_selfcheck_enabled": True},
            }
        },
        headers=HEADERS,
    )
    assert r1.status_code == 200, r1.text

    r2 = client.post(
        "/api/settings",
        json={"autopilot": {"scaler_v2": {"require_fresh_7d": True}}},
        headers=HEADERS,
    )
    assert r2.status_code == 200, r2.text

    body = client.get("/api/settings", headers=HEADERS).json()["autopilot"]
    # scaler_v2 — все три под-ключа доступны одновременно
    assert body["scaler_v2"]["waster_min_spend_usd"] == 30.0
    assert body["scaler_v2"]["engine_selfcheck_enabled"] is True
    assert body["scaler_v2"]["require_fresh_7d"] is True
    # соседний блок cdp не тронут
    assert body["cdp"]["enabled"] is True
    assert body["cdp"]["payments_source"] == "shadow"


def test_scaler_v2_http_unknown_key_400(client):
    """Неизвестный под-ключ scaler_v2 через HTTP → 400, ничего не сохраняется."""
    resp = client.post(
        "/api/settings",
        json={"autopilot": {"scaler_v2": {"disable_waster_veto": True}}},
        headers=HEADERS,
    )
    assert resp.status_code == 400


def test_scaler_v2_http_nan_400(client):
    """NaN во float-поле через HTTP → 400 (валидатор отклоняет неконечные числа).
    NaN сериализуется как literal в JSON body — FastAPI парсит его в float('nan')."""
    resp = client.post(
        "/api/settings",
        content='{"autopilot": {"scaler_v2": {"sanity_cap_ratio": NaN}}}',
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_get_autopilot_config_default_scaler_v2(tmp_path):
    """Без записи в settings.json scaler_v2 дефолтно = AUTOPILOT_DEFAULTS['scaler_v2']
    (safety-гейты ВКЛючены, require_fresh_7d выключен)."""
    settings_path = tmp_path / "settings_empty_v2.json"
    with patch("agent.scheduler.SETTINGS_FILE", settings_path):
        from services.autopilot import get_autopilot_config, AUTOPILOT_DEFAULTS
        cfg = get_autopilot_config()

    assert cfg["scaler_v2"] == AUTOPILOT_DEFAULTS["scaler_v2"]
    assert cfg["scaler_v2"]["require_fresh_7d"] is False
    assert cfg["scaler_v2"]["engine_selfcheck_enabled"] is True
    assert cfg["scaler_v2"]["sanity_cap_enabled"] is True
