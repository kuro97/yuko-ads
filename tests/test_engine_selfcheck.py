"""Тесты services/engine_selfcheck.py — самопроверка прогноза CDP (Этап 4).

CDP замокан (services.cdp_client.get_budget_context). State изолирован в tmp_path.
Момент времени передаётся явно через now=... (детерминизм сезонной кривой).
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.engine_selfcheck as esc
from services.cdp_client import CdpError

_TZ = timezone(timedelta(hours=5))


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(esc, "_SELFCHECK_STATE_FILE", tmp_path / "engine_selfcheck_state.json")


def _now(day=16):
    return datetime(2026, 7, day, 13, 0, 0, tzinfo=_TZ)


def _ctx(forecast_eom, fact_mtd, month="2026-07"):
    return {
        "month": month,
        "cities": [
            {"city": "_total", "metrics": {
                "revenue_new": {"forecast_eom": forecast_eom, "fact_mtd": fact_mtd},
            }},
        ],
    }


def _seed(snapshots):
    esc._SELFCHECK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    esc._SELFCHECK_STATE_FILE.write_text(json.dumps({"snapshots": snapshots}), encoding="utf-8")


# --- record_snapshot ---

def test_record_snapshot_appends():
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(20_000_000, 4_000_000)):
        esc.record_snapshot(now=_now(16))
    state = json.loads(esc._SELFCHECK_STATE_FILE.read_text())
    assert len(state["snapshots"]) == 1
    snap = state["snapshots"][0]
    assert snap["date"] == "2026-07-16"
    assert snap["month"] == "2026-07"
    assert snap["forecast_eom"] == 20_000_000
    assert snap["fact_mtd"] == 4_000_000


def test_record_snapshot_once_per_day():
    """Второй вызов в тот же день → no-op (не дублируем снапшот)."""
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(20_000_000, 4_000_000)) as mock_ctx:
        esc.record_snapshot(now=_now(16))
        esc.record_snapshot(now=_now(16))
    state = json.loads(esc._SELFCHECK_STATE_FILE.read_text())
    assert len(state["snapshots"]) == 1
    # Второй раз даже не дёргает CDP (сначала проверяет наличие снапшота дня)
    assert mock_ctx.call_count == 1


def test_record_snapshot_cdp_error_noop():
    """Ошибка CDP → снапшот не пишется."""
    with patch("services.cdp_client.get_budget_context", side_effect=CdpError("down")):
        esc.record_snapshot(now=_now(16))
    assert not esc._SELFCHECK_STATE_FILE.exists()


def test_record_snapshot_retention_60d():
    """Снапшоты старше 60 дней вычищаются при записи нового."""
    _seed([{"date": "2026-04-01", "month": "2026-04",
            "forecast_eom": 1, "fact_mtd": 1}])
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(20_000_000, 4_000_000)):
        esc.record_snapshot(now=_now(16))
    state = json.loads(esc._SELFCHECK_STATE_FILE.read_text())
    dates = [s["date"] for s in state["snapshots"]]
    assert "2026-04-01" not in dates  # >60 дней назад
    assert "2026-07-16" in dates


# --- compute_forecast_selfcheck / evaluate ---

def test_selfcheck_forecast_overstated_distrust():
    """Прогноз завышал: ожидание ~¤7.8M, факт ¤5.4M (−31%) → недоверие (reason)."""
    _seed([
        {"date": "2026-07-09", "month": "2026-07", "forecast_eom": 20_000_000, "fact_mtd": 4_000_000},
        {"date": "2026-07-16", "month": "2026-07", "forecast_eom": 18_000_000, "fact_mtd": 5_400_000},
    ])
    result = esc.compute_forecast_selfcheck(now=_now(16), max_dev=0.25)
    assert result is not None
    assert result["dev"] < 0
    assert result["reason"] is not None
    assert "прогноз CDP завышает" in result["reason"]
    # evaluate — тонкая обёртка, отдаёт ту же строку
    assert esc.evaluate_forecast_selfcheck(now=_now(16), max_dev=0.25) == result["reason"]


def test_selfcheck_within_tolerance_neutral():
    """Факт ¤6.8M при ожидании ~¤7.8M (−13%) < порога 25% → нейтрально (reason None)."""
    _seed([
        {"date": "2026-07-09", "month": "2026-07", "forecast_eom": 20_000_000, "fact_mtd": 4_000_000},
        {"date": "2026-07-16", "month": "2026-07", "forecast_eom": 18_000_000, "fact_mtd": 6_800_000},
    ])
    result = esc.compute_forecast_selfcheck(now=_now(16), max_dev=0.25)
    assert result is not None
    assert result["reason"] is None
    assert esc.evaluate_forecast_selfcheck(now=_now(16), max_dev=0.25) is None


def test_selfcheck_different_month_none():
    """«Тогдашний» снапшот другого месяца → сверку не делаем (None)."""
    _seed([
        {"date": "2026-07-09", "month": "2026-06", "forecast_eom": 20_000_000, "fact_mtd": 4_000_000},
        {"date": "2026-07-16", "month": "2026-07", "forecast_eom": 18_000_000, "fact_mtd": 5_400_000},
    ])
    assert esc.compute_forecast_selfcheck(now=_now(16)) is None


def test_selfcheck_no_then_snapshot_none():
    """Есть только сегодняшний снапшот → сверку не с чем делать (None)."""
    _seed([
        {"date": "2026-07-16", "month": "2026-07", "forecast_eom": 18_000_000, "fact_mtd": 5_400_000},
    ])
    assert esc.compute_forecast_selfcheck(now=_now(16)) is None


def test_selfcheck_no_today_snapshot_none():
    """Нет сегодняшнего снапшота (CDP молчал) → None."""
    _seed([
        {"date": "2026-07-09", "month": "2026-07", "forecast_eom": 20_000_000, "fact_mtd": 4_000_000},
    ])
    assert esc.compute_forecast_selfcheck(now=_now(16)) is None


def test_selfcheck_empty_state_none():
    assert esc.compute_forecast_selfcheck(now=_now(16)) is None
    assert esc.evaluate_forecast_selfcheck(now=_now(16)) is None


# --- Wave 3C: строгая валидация снапшота + статусы record_snapshot ---

def test_record_snapshot_returns_recorded_status():
    """Валидный полный ответ CDP → status='recorded', снапшот сохранён."""
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(20_000_000, 4_000_000)):
        res = esc.record_snapshot(now=_now(16))
    assert res == {"status": "recorded", "saved": True}


def test_record_snapshot_idempotent_status_no_dupe():
    """Второй вызов в тот же день при ВАЛИДНОМ снапшоте → status='idempotent', без CDP."""
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(20_000_000, 4_000_000)) as mock_ctx:
        esc.record_snapshot(now=_now(16))
        res2 = esc.record_snapshot(now=_now(16))
    assert res2 == {"status": "idempotent", "saved": False}
    assert mock_ctx.call_count == 1


def test_record_snapshot_partial_then_valid_same_day_replaces():
    """Невалидный (partial) снапшот сегодня заменяется валидным ответом в тот же день."""
    _seed([
        {"date": "2026-07-16", "month": "2026-07", "forecast_eom": None, "fact_mtd": 4_000_000},
    ])
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(18_000_000, 5_400_000)) as mock_ctx:
        res = esc.record_snapshot(now=_now(16))
    assert res == {"status": "recorded", "saved": True}
    assert mock_ctx.call_count == 1  # partial снапшот НЕ идемпотентен — запрос был
    state = json.loads(esc._SELFCHECK_STATE_FILE.read_text())
    today = [s for s in state["snapshots"] if s["date"] == "2026-07-16"]
    assert len(today) == 1  # без дубля
    assert today[0]["forecast_eom"] == 18_000_000  # заменён валидным


def test_record_snapshot_invalid_response_not_saved():
    """Неполный ответ CDP (нет revenue_new._total) → status='invalid', не сохраняем."""
    with patch("services.cdp_client.get_budget_context", return_value={"month": "2026-07", "cities": []}):
        res = esc.record_snapshot(now=_now(16))
    assert res == {"status": "invalid", "saved": False}
    assert not esc._SELFCHECK_STATE_FILE.exists()


@pytest.mark.parametrize("bad_forecast", [float("nan"), float("inf"), -1.0])
def test_record_snapshot_rejects_non_finite_or_negative(bad_forecast):
    """NaN/Inf/отрицательные forecast_eom → снапшот невалиден, не сохраняем."""
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(bad_forecast, 4_000_000)):
        res = esc.record_snapshot(now=_now(16))
    assert res["status"] == "invalid"
    assert not esc._SELFCHECK_STATE_FILE.exists()


def test_record_snapshot_cdp_error_status_unavailable():
    """Ошибка CDP → status='unavailable', снапшот не пишется."""
    with patch("services.cdp_client.get_budget_context", side_effect=CdpError("down")):
        res = esc.record_snapshot(now=_now(16))
    assert res == {"status": "unavailable", "saved": False}


def test_record_snapshot_save_error_surfaced():
    """Ошибка _save_state ЯВНО возвращается как save_error (не скрыта за логом)."""
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(20_000_000, 4_000_000)), \
         patch.object(esc, "_save_state", side_effect=OSError("disk full")):
        res = esc.record_snapshot(now=_now(16))
    assert res == {"status": "save_error", "saved": False}


# --- Wave 3C: run_selfcheck — fail-closed оркестратор гейта ---

def test_run_selfcheck_ok_when_valid_history_normal_deviation():
    """Валидная история + отклонение в норме → status='ok', gate_ok=True."""
    _seed([
        {"date": "2026-07-09", "month": "2026-07", "forecast_eom": 20_000_000, "fact_mtd": 4_000_000},
        {"date": "2026-07-16", "month": "2026-07", "forecast_eom": 18_000_000, "fact_mtd": 6_800_000},
    ])
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(18_000_000, 6_800_000)):
        res = esc.run_selfcheck(now=_now(16), max_dev=0.25)
    assert res["status"] == "ok"
    assert res["gate_ok"] is True
    assert res["reason"] is None


def test_run_selfcheck_distrust_blocks_and_sets_reason():
    """Сильное отставание факта → status='distrust', gate_ok=False, reason задан
    (reason гасит engine-путь в scaler)."""
    _seed([
        {"date": "2026-07-09", "month": "2026-07", "forecast_eom": 20_000_000, "fact_mtd": 4_000_000},
        {"date": "2026-07-16", "month": "2026-07", "forecast_eom": 18_000_000, "fact_mtd": 5_400_000},
    ])
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(18_000_000, 5_400_000)):
        res = esc.run_selfcheck(now=_now(16), max_dev=0.25)
    assert res["status"] == "distrust"
    assert res["gate_ok"] is False
    assert res["reason"] is not None


def test_run_selfcheck_warming_when_no_history():
    """Есть только сегодняшний валидный снапшот, истории 6-9 дней нет → warming, блок."""
    _seed([
        {"date": "2026-07-16", "month": "2026-07", "forecast_eom": 18_000_000, "fact_mtd": 5_400_000},
    ])
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(18_000_000, 5_400_000)):
        res = esc.run_selfcheck(now=_now(16))
    assert res["status"] == "warming"
    assert res["gate_ok"] is False


def test_run_selfcheck_fetch_error_unavailable():
    """CDP лёг (нет свежего снапшота) → status='unavailable', блок."""
    with patch("services.cdp_client.get_budget_context", side_effect=CdpError("down")):
        res = esc.run_selfcheck(now=_now(16))
    assert res["status"] == "unavailable"
    assert res["gate_ok"] is False


def test_run_selfcheck_save_error_unavailable():
    """Ошибка сохранения снапшота → блок (не превращается в разрешение)."""
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(20_000_000, 4_000_000)), \
         patch.object(esc, "_save_state", side_effect=OSError("disk full")):
        res = esc.run_selfcheck(now=_now(16))
    assert res["gate_ok"] is False
    assert res["status"] == "unavailable"


def test_run_selfcheck_pacing_error_unavailable():
    """Ошибка сезонной кривой (pacing_curve) во время сверки → unavailable, блок."""
    _seed([
        {"date": "2026-07-09", "month": "2026-07", "forecast_eom": 20_000_000, "fact_mtd": 4_000_000},
        {"date": "2026-07-16", "month": "2026-07", "forecast_eom": 18_000_000, "fact_mtd": 6_800_000},
    ])
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(18_000_000, 6_800_000)), \
         patch("services.pacing_curve.expected_cumulative_share", side_effect=RuntimeError("boom")):
        res = esc.run_selfcheck(now=_now(16))
    assert res["status"] == "unavailable"
    assert res["gate_ok"] is False


def test_run_selfcheck_corrupt_state_invalid_and_recovers():
    """Битый state → status='invalid', блок; today-запись восстановлена (recovery)."""
    esc._SELFCHECK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    esc._SELFCHECK_STATE_FILE.write_text("{ это не json", encoding="utf-8")
    with patch("services.cdp_client.get_budget_context",
               return_value=_ctx(18_000_000, 5_400_000)):
        res = esc.run_selfcheck(now=_now(16))
    assert res["status"] == "invalid"
    assert res["gate_ok"] is False
    # Восстановление: файл перезаписан валидным сегодняшним снапшотом
    state = json.loads(esc._SELFCHECK_STATE_FILE.read_text())
    assert any(s["date"] == "2026-07-16" for s in state["snapshots"])


def test_run_selfcheck_never_raises_on_unexpected_error():
    """Неожиданная ошибка внутри (get_budget_context кидает не-CdpError) → блок, не падение."""
    with patch("services.cdp_client.get_budget_context", side_effect=ValueError("boom")):
        res = esc.run_selfcheck(now=_now(16))
    assert res["gate_ok"] is False
    assert res["status"] == "unavailable"
