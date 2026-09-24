"""Единая семантика Meta lead actions и регрессии всех потребителей."""

from unittest.mock import MagicMock, patch

import pytest

from agent.analyzer import _parse_insight_row
from agent.learner import parse_video_insight_row
from services.hourly_collector import _parse_hourly_rows
from services.meta_lead_actions import parse_meta_lead_actions
from services.metrics_snapshot import _parse_daily_row
from web.overview_routes import _fetch_trends


@pytest.mark.parametrize(
    ("actions", "canonical", "instant_form", "website", "status"),
    [
        (None, 0, None, None, "ok"),
        ([], 0, None, None, "ok"),
        ([{"action_type": "lead", "value": "7"}], 7, None, None, "ok"),
        ([{"action_type": "onsite_conversion.lead_grouped", "value": 4}], 4, 4, None, "ok"),
        ([{"action_type": "onsite_web_lead", "value": "3"}], 3, None, 3, "ok"),
        ([
            {"action_type": "onsite_conversion.lead_grouped", "value": 4},
            {"action_type": "offsite_conversion.fb_pixel_lead", "value": 3},
        ], 7, 4, 3, "ok"),
        ([
            {"action_type": "lead", "value": 7},
            {"action_type": "onsite_conversion.lead_grouped", "value": 4},
            {"action_type": "offsite_conversion.fb_pixel_lead", "value": 3},
        ], 7, 4, 3, "ok"),
        ([
            {"action_type": "onsite_web_lead", "value": 3},
            {"action_type": "offsite_conversion.fb_pixel_lead", "value": "3"},
            {"action_type": "offsite_lead_add_20_s_calls", "value": 3.0},
        ], 3, None, 3, "ok"),
    ],
)
def test_parse_meta_lead_actions_matrix(actions, canonical, instant_form, website, status):
    result = parse_meta_lead_actions(actions)

    assert result.canonical_total == canonical
    assert result.instant_form == instant_form
    assert result.website == website
    assert result.status == status


def test_aggregate_wins_but_component_mismatch_is_diagnostic():
    result = parse_meta_lead_actions([
        {"action_type": "lead", "value": 8},
        {"action_type": "onsite_conversion.lead_grouped", "value": 2},
        {"action_type": "offsite_conversion.fb_pixel_lead", "value": 3},
    ])

    assert result.canonical_total == 8
    assert result.status == "component_mismatch"
    assert result.problems == ("component_total_mismatch:lead=8,components=5",)


@pytest.mark.parametrize(
    "actions",
    [
        "not-a-list",
        [None],
        [{"value": 1}],
        [
            {"action_type": "lead", "value": 1},
            {"action_type": "lead", "value": 1},
        ],
        [
            {"action_type": "onsite_web_lead", "value": 1},
            {"action_type": "offsite_conversion.fb_pixel_lead", "value": 2},
        ],
        [{"action_type": "lead", "value": False}],
        [{"action_type": "lead", "value": -1}],
        [{"action_type": "lead", "value": 0.5}],
        [{"action_type": "lead", "value": "1.0"}],
    ],
)
def test_parse_meta_lead_actions_rejects_malformed_or_lossy_values(actions):
    result = parse_meta_lead_actions(actions)

    assert result.canonical_total is None
    assert result.status == "invalid"
    assert result.problems


def _canonical_actions():
    return [
        {"action_type": "lead", "value": "5"},
        {"action_type": "onsite_conversion.lead_grouped", "value": "5"},
        {"action_type": "video_view", "value": "20"},
    ]


def test_analyzer_uses_canonical_total_once():
    result = _parse_insight_row({"spend": "10", "actions": _canonical_actions()})

    assert result["leads"] == 5
    assert result["cpl"] == 2.0


def test_learner_uses_canonical_total_once():
    result = parse_video_insight_row({"spend": "10", "actions": _canonical_actions()})

    assert result["leads"] == 5
    assert result["video_views_3s"] == 20


def test_metrics_snapshot_uses_canonical_total_once():
    row = {"ad_id": "ad-1", "spend": "10", "actions": _canonical_actions()}

    result = _parse_daily_row(row, created_times={}, on_date="2026-07-22")

    assert result["leads"] == 5
    assert result["cpl"] == 2.0


def test_hourly_collector_uses_canonical_total_once():
    row = {
        "ad_id": "ad-1",
        "date_start": "2026-07-22",
        "hourly_stats_aggregated_by_advertiser_time_zone": "08:00:00 - 08:59:59",
        "actions": _canonical_actions(),
    }

    result = _parse_hourly_rows([row], ad_id="ad-1")

    assert result[0]["actions_lead"] == 5


def test_hourly_collector_raises_instead_of_writing_invalid_zero():
    row = {
        "date_start": "2026-07-22",
        "hourly_stats_aggregated_by_advertiser_time_zone": "08:00:00 - 08:59:59",
        "actions": [{"action_type": "lead", "value": "0.5"}],
    }

    with pytest.raises(ValueError, match="invalid Meta lead actions"):
        _parse_hourly_rows([row], ad_id="ad-1")


def test_overview_trends_keeps_instant_form_scope():
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "data": [{
            "date_start": "2026-07-22",
            "spend": "10",
            "actions": [
                {"action_type": "lead", "value": "8"},
                {"action_type": "onsite_conversion.lead_grouped", "value": "5"},
                {"action_type": "offsite_conversion.fb_pixel_lead", "value": "3"},
            ],
        }]
    }
    with patch("agent.fb_common._throttled_get", return_value=response), \
         patch("services.fb_token_provider.get_fb_token", return_value="token"), \
         patch("services.fb_token_provider.get_fb_account_id", return_value="account"), \
         patch("integrations.amo.get_leads", return_value=[]):
        result = _fetch_trends(7)

    assert result["leads"] == [5]


def test_overview_trends_raises_instead_of_publishing_invalid_zero():
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "data": [{
            "date_start": "2026-07-22",
            "spend": "10",
            "actions": [{"action_type": "lead", "value": "0.5"}],
        }]
    }
    with patch("agent.fb_common._throttled_get", return_value=response), \
         patch("services.fb_token_provider.get_fb_token", return_value="token"), \
         patch("services.fb_token_provider.get_fb_account_id", return_value="account"):
        with pytest.raises(ValueError, match="invalid Meta lead actions"):
            _fetch_trends(7)


@pytest.mark.parametrize(
    "parser,args",
    [
        (_parse_insight_row, ({"actions": [{"action_type": "lead", "value": "0.5"}]},)),
        (parse_video_insight_row, ({"actions": [{"action_type": "lead", "value": "0.5"}]},)),
        (_parse_daily_row, (
            {"ad_id": "ad-1", "actions": [{"action_type": "lead", "value": "0.5"}]},
            {},
            "2026-07-22",
        )),
    ],
)
def test_reporting_row_parsers_raise_on_invalid_lead_actions(parser, args):
    with pytest.raises(ValueError, match="invalid Meta lead actions"):
        parser(*args)
