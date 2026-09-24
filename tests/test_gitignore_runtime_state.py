"""Тест .gitignore (Wave 4): runtime state исключён из Git, но нужные
tracked fixtures/reference data и docs/specs — НЕ скрыты.

Использует локальный `git check-ignore` (без сети).
"""

import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


def _is_ignored(relpath: str) -> bool:
    """git check-ignore: exit 0 = игнорируется, 1 = нет."""
    res = subprocess.run(
        ["git", "check-ignore", "-q", relpath],
        cwd=str(PROJECT_ROOT), capture_output=True,
    )
    return res.returncode == 0


# Runtime state / кеши / секреты — ДОЛЖНЫ игнорироваться
_MUST_IGNORE = [
    "data/creative_kb.db",
    "data/settings.json",
    "data/auto_actions.json",
    "data/autopilot_state.json",
    "data/autopilot_live_daily.json",
    "data/budget_scaler_state.json",
    "data/budget_scaler_v2_state.json",
    "data/budget_daily_cap_state.json",
    "data/budget_scaler_cron_state.json",
    "data/budget_daily_cap_state.json.lock",
    "data/cdp_payments_state.json",
    "data/cdp_payments_lead_cache.json",
    "data/cron_heartbeats.json",
    "data/doubt_log.json",
    "data/engine_selfcheck_state.json",
    "data/guardian_state.json",
    "data/expired_offer_guard_state.json",
    "data/metrics_backfill_state.json",
    "data/ad_created_times.json",
    "data/qual_baseline_state.json",
    "data/llm_credit_alert_state.json",
    "data/online_report_state.json",
    "data/adset_spend_guard_state.json",
    "data/fb_credentials.json",
    "data/history.json",
    "exports/one_off_export_ABC.json",
    "data/one_off_manifest_20260101_000000.json",
]

# Нужные tracked спеки — НЕ должны игнорироваться
# (в публичной версии data/ игнорируется целиком: данных компании там нет)
_MUST_NOT_IGNORE = [
    "docs/specs/ARCH-phase2-budget-pilot.md",
]


@pytest.mark.parametrize("relpath", _MUST_IGNORE)
def test_runtime_state_ignored(relpath):
    assert _is_ignored(relpath), f"{relpath} должен игнорироваться .gitignore"


@pytest.mark.parametrize("relpath", _MUST_NOT_IGNORE)
def test_tracked_data_and_specs_not_ignored(relpath):
    assert not _is_ignored(relpath), f"{relpath} НЕ должен скрываться .gitignore"


def test_broad_data_json_glob_absent():
    """Убеждаемся, что не завёлся слишком широкий data/*.json (скрыл бы fixtures)."""
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in gitignore.splitlines()]
    assert "data/*.json" not in lines
