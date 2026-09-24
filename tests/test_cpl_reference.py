"""Тесты services/cpl_reference.py — скользящая цена лида кабинета.

FB замокан (_throttled_get + провайдер токена), кэш — во временном файле.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from services import cpl_reference
from services.cpl_reference import (
    CplReference,
    _window,
    fetch_account_cpl_reference,
    load_cpl_references,
)

_NOW = datetime(2026, 9, 15, 6, 30, tzinfo=timezone.utc)  # 11:30 CityA


def _resp(status=200, data=None, text=""):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.json.return_value = {"data": data if data is not None else []}
    return resp


def _patch_fb(get_side_effect):
    return (
        patch("agent.fb_common._throttled_get", side_effect=get_side_effect),
        patch("services.fb_token_provider.get_fb_account_id", return_value="111"),
        patch("services.fb_token_provider.get_fb_token", return_value="tok"),
        patch("services.fb_token_provider.offline_account_context", return_value=None),
    )


def test_window_is_full_days_before_today_in_citya():
    since, until = _window(_NOW, 14)
    assert until == "2026-09-14"
    assert since == "2026-09-01"


def test_fetch_computes_cpl_from_spend_and_lead_actions():
    captured = {}

    def _get(url, params=None, **kw):
        captured["url"] = url
        captured["params"] = params
        return _resp(data=[{"spend": "1000.0", "actions": [{"action_type": "lead", "value": "200"}]}])

    patches = _patch_fb(_get)
    with patches[0], patches[1], patches[2], patches[3]:
        ref = fetch_account_cpl_reference("act_111", now=_NOW)

    assert ref.source == "fb_account_window"
    assert ref.cpl == Decimal("5.00")
    assert ref.leads == 200 and ref.spend_usd == 1000.0
    assert ref.account_id == "111"
    assert captured["params"]["level"] == "account"
    assert json.loads(captured["params"]["time_range"]) == {"since": "2026-09-01", "until": "2026-09-14"}
    assert "act_111/insights" in captured["url"]


def test_fetch_insufficient_leads_gives_no_cpl():
    patches = _patch_fb(lambda *a, **k: _resp(data=[{"spend": "40", "actions": [{"action_type": "lead", "value": "3"}]}]))
    with patches[0], patches[1], patches[2], patches[3]:
        ref = fetch_account_cpl_reference("111", now=_NOW, min_leads=10)
    assert ref.cpl is None
    assert ref.source == "insufficient_leads"
    assert ref.leads == 3


def test_fetch_http_error_is_error_source_not_exception():
    patches = _patch_fb(lambda *a, **k: _resp(status=500, text="boom"))
    with patches[0], patches[1], patches[2], patches[3]:
        ref = fetch_account_cpl_reference("111", now=_NOW)
    assert ref.cpl is None and ref.source == "error"


def test_fetch_exception_is_error_source_not_exception():
    def _boom(*a, **k):
        raise RuntimeError("network down")

    patches = _patch_fb(_boom)
    with patches[0], patches[1], patches[2], patches[3]:
        ref = fetch_account_cpl_reference("111", now=_NOW)
    assert ref.source == "error"


def test_load_uses_fresh_cache_without_fb_call(tmp_path):
    cache = tmp_path / "cpl.json"
    entry = CplReference(
        account_id="111", cpl_usd="10.00", spend_usd=1000.0, leads=100, window_days=14,
        since="2026-09-01", until="2026-09-14",
        computed_at=(_NOW - timedelta(hours=3)).isoformat(), source="fb_account_window",
    )
    cache.write_text(json.dumps({"111": entry.__dict__}), encoding="utf-8")

    with patch.object(cpl_reference, "fetch_account_cpl_reference") as fetch:
        refs = load_cpl_references(["act_111"], now=_NOW, cache_path=cache)

    fetch.assert_not_called()
    assert refs["111"].cpl == Decimal("10.00")


def test_load_refetches_when_cache_stale_or_window_differs(tmp_path):
    cache = tmp_path / "cpl.json"
    stale = CplReference(
        account_id="111", cpl_usd="10.00", spend_usd=1000.0, leads=100, window_days=14,
        since="2026-08-30", until="2026-09-12",
        computed_at=(_NOW - timedelta(hours=40)).isoformat(), source="fb_account_window",
    )
    cache.write_text(json.dumps({"111": stale.__dict__}), encoding="utf-8")
    fresh = CplReference(
        account_id="111", cpl_usd="7.50", spend_usd=750.0, leads=100, window_days=14,
        since="2026-09-01", until="2026-09-14", computed_at=_NOW.isoformat(), source="fb_account_window",
    )
    with patch.object(cpl_reference, "fetch_account_cpl_reference", return_value=fresh) as fetch:
        refs = load_cpl_references(["111"], now=_NOW, cache_path=cache)
    fetch.assert_called_once()
    assert refs["111"].cpl == Decimal("7.50")
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["111"]["cpl_usd"] == "7.50"

    # другое окно → кэш не годится даже свежий
    with patch.object(cpl_reference, "fetch_account_cpl_reference", return_value=fresh) as fetch2:
        load_cpl_references(["111"], now=_NOW, cache_path=cache, window_days=7)
    fetch2.assert_called_once()


def test_load_does_not_cache_errors(tmp_path):
    cache = tmp_path / "cpl.json"
    failed = CplReference(
        account_id="111", cpl_usd=None, spend_usd=0.0, leads=0, window_days=14,
        since="2026-09-01", until="2026-09-14", computed_at=_NOW.isoformat(), source="error",
    )
    with patch.object(cpl_reference, "fetch_account_cpl_reference", return_value=failed):
        refs = load_cpl_references(["111"], now=_NOW, cache_path=cache)
    assert refs["111"].source == "error"
    assert not cache.exists()
