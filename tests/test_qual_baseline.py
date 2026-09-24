"""Тесты services/qual_baseline.py — адаптивный порог квала.

AMO замокан на границе (integrations.amo.get_leads_window / classify_lead).
Кеш изолирован в tmp_path. Реальная сеть заблокирована conftest.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.qual_baseline as qb

_TZ = timezone(timedelta(hours=5))


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(qb, "_QUAL_STATE_FILE", tmp_path / "qual_baseline_state.json")


def _now():
    return datetime(2026, 7, 16, 13, 0, 0, tzinfo=_TZ)


def _leads(kinds):
    """kinds: список статусов ('квал'/'оплата'/'новый') — каждый лид несёт свой."""
    return [{"kind": k} for k in kinds]


def _classify(lead):
    return lead["kind"]


# --- compute_account_qual_baseline ---

def test_compute_qual_pct_mix():
    """5 лидов: 2 квал + 1 оплата + 2 новых → квал% = 60%."""
    leads = _leads(["квал", "квал", "оплата", "новый", "новый"])
    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("integrations.amo.classify_lead", side_effect=_classify):
        result = qb.compute_account_qual_baseline(days=30, now=_now())
    assert result is not None
    assert result["total_leads"] == 5
    assert result["qual_leads"] == 3
    assert result["qual_pct"] == pytest.approx(60.0)
    assert result["days"] == 30


def test_compute_empty_returns_none():
    with patch("integrations.amo.get_leads_window", return_value=[]), \
         patch("integrations.amo.classify_lead", side_effect=_classify):
        assert qb.compute_account_qual_baseline(now=_now()) is None


def test_compute_amo_error_returns_none():
    """Ошибка AMO не бросается наружу → None."""
    with patch("integrations.amo.get_leads_window", side_effect=RuntimeError("amo down")):
        assert qb.compute_account_qual_baseline(now=_now()) is None


# --- get_cached_qual_baseline ---

def _write_cache(payload):
    qb._QUAL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    qb._QUAL_STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")


def test_cached_fresh_no_recompute():
    """Свежий кеш (возраст < TTL) → отдаём как есть, пересчёт НЕ зовём."""
    _write_cache({
        "qual_pct": 15.4, "qual_leads": 77, "total_leads": 500, "days": 30,
        "computed_at": _now().isoformat(),
    })
    with patch.object(qb, "compute_account_qual_baseline") as mock_compute:
        result = qb.get_cached_qual_baseline(ttl_hours=24, now=_now() + timedelta(hours=5))
    mock_compute.assert_not_called()
    assert result["qual_pct"] == 15.4
    assert result["stale"] is False


def test_cached_stale_recompute_success():
    """Кеш протух → пересчёт удачен → отдаём свежий, кеш обновлён."""
    _write_cache({
        "qual_pct": 10.0, "qual_leads": 50, "total_leads": 500, "days": 30,
        "computed_at": (_now() - timedelta(hours=30)).isoformat(),
    })
    fresh = {"qual_pct": 20.0, "qual_leads": 100, "total_leads": 500, "days": 30,
             "computed_at": _now().isoformat()}
    with patch.object(qb, "compute_account_qual_baseline", return_value=fresh):
        result = qb.get_cached_qual_baseline(ttl_hours=24, now=_now())
    assert result["qual_pct"] == 20.0
    assert result["stale"] is False
    # Кеш перезаписан свежим значением
    saved = json.loads(qb._QUAL_STATE_FILE.read_text())
    assert saved["qual_pct"] == 20.0


def test_cached_stale_recompute_fail_returns_stale():
    """Кеш протух, пересчёт упал, кеш не старше 7 дней → отдаём stale=True."""
    _write_cache({
        "qual_pct": 12.0, "qual_leads": 60, "total_leads": 500, "days": 30,
        "computed_at": (_now() - timedelta(days=3)).isoformat(),
    })
    with patch.object(qb, "compute_account_qual_baseline", return_value=None):
        result = qb.get_cached_qual_baseline(ttl_hours=24, now=_now())
    assert result is not None
    assert result["qual_pct"] == 12.0
    assert result["stale"] is True


def test_cached_stale_too_old_recompute_fail_none():
    """Кеш старше 7 дней и пересчёт упал → None."""
    _write_cache({
        "qual_pct": 12.0, "qual_leads": 60, "total_leads": 500, "days": 30,
        "computed_at": (_now() - timedelta(days=10)).isoformat(),
    })
    with patch.object(qb, "compute_account_qual_baseline", return_value=None):
        assert qb.get_cached_qual_baseline(ttl_hours=24, now=_now()) is None


def test_cached_no_cache_recompute_fail_none():
    with patch.object(qb, "compute_account_qual_baseline", return_value=None):
        assert qb.get_cached_qual_baseline(now=_now()) is None


# --- effective_qual_threshold ---

def test_threshold_override_wins():
    thr, label = qb.effective_qual_threshold({"qual_override_pct": 18.0}, {"qual_pct": 15.4})
    assert thr == 18.0
    assert "вручную" in label


def test_threshold_adaptive_from_base():
    """0.8 × 15.4 = 12.32 → 12.3%, между полом 5 и потолком 30."""
    baseline = {"qual_pct": 15.4, "days": 30}
    cfg = {"qual_base_mult": 0.8, "qual_floor_pct": 5.0, "qual_cap_pct": 30.0}
    thr, label = qb.effective_qual_threshold(cfg, baseline)
    assert thr == pytest.approx(12.32)
    assert "0.8× базы 15.4% за 30 дн" in label


def test_threshold_clamped_to_floor():
    """0.8 × 3% = 2.4% → зажимается полом 5%."""
    thr, _ = qb.effective_qual_threshold(
        {"qual_base_mult": 0.8, "qual_floor_pct": 5.0, "qual_cap_pct": 30.0},
        {"qual_pct": 3.0, "days": 30},
    )
    assert thr == pytest.approx(5.0)


def test_threshold_clamped_to_cap():
    """0.8 × 50% = 40% → зажимается потолком 30%."""
    thr, _ = qb.effective_qual_threshold(
        {"qual_base_mult": 0.8, "qual_floor_pct": 5.0, "qual_cap_pct": 30.0},
        {"qual_pct": 50.0, "days": 30},
    )
    assert thr == pytest.approx(30.0)


def test_threshold_no_baseline_disabled():
    thr, label = qb.effective_qual_threshold({"qual_override_pct": 0.0}, None)
    assert thr is None
    assert "выключена" in label
