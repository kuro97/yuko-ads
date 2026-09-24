"""Живой квал-страж: KB-ноль перепроверяется по AMO, живой квал снимает паузу."""

from unittest.mock import patch

from services.live_qual_guard import filter_stale_qual_candidates, kb_sees_zero_quals


def _decision(ad_id: str) -> dict:
    return {"ad_id": ad_id, "action": "PAUSE", "score": 1, "reasons": ["qual 0"]}


def test_live_qual_removes_stale_candidate():
    ads = {"a1": {"leads": 20, "qual_pct": 0.0}}
    with patch("services.live_qual_guard.live_qual_count", return_value=5):
        kept, dropped = filter_stale_qual_candidates([_decision("a1")], ads)
    assert kept == [] and dropped == ["a1"]


def test_zero_live_quals_keeps_candidate():
    ads = {"a1": {"leads": 20, "qual_pct": 0.0}}
    with patch("services.live_qual_guard.live_qual_count", return_value=0):
        kept, dropped = filter_stale_qual_candidates([_decision("a1")], ads)
    assert len(kept) == 1 and dropped == []


def test_amo_unavailable_fails_open_for_pause():
    """Сеть упала — страж молчит и паузу НЕ блокирует (защита от сливов важнее)."""
    ads = {"a1": {"leads": 20, "qual_pct": 0.0}}
    with patch("services.live_qual_guard.live_qual_count", return_value=None):
        kept, dropped = filter_stale_qual_candidates([_decision("a1")], ads)
    assert len(kept) == 1 and dropped == []


def test_no_leads_candidate_not_checked():
    """Реклама без лидов квалов иметь не может — живой запрос не тратится."""
    ads = {"a1": {"leads": 0, "qual_pct": None}}
    with patch("services.live_qual_guard.live_qual_count", side_effect=AssertionError) as m:
        kept, dropped = filter_stale_qual_candidates([_decision("a1")], ads)
    assert len(kept) == 1 and dropped == [] and not m.called


def test_kb_with_known_quals_not_checked():
    ads = {"a1": {"leads": 20, "qual_pct": 25.0}}
    with patch("services.live_qual_guard.live_qual_count", side_effect=AssertionError) as m:
        kept, dropped = filter_stale_qual_candidates([_decision("a1")], ads)
    assert len(kept) == 1 and not m.called


def test_kb_sees_zero_quals_semantics():
    assert kb_sees_zero_quals(None)
    assert kb_sees_zero_quals({"qual_pct": None})
    assert kb_sees_zero_quals({"qual_pct": 0})
    assert not kb_sees_zero_quals({"qual_pct": 12.5})
