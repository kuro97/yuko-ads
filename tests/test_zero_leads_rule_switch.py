"""Переключатель thresholds.zero_leads_rule_enabled: правило «N дней без лида» уступает раннему стопу.

Решение владельца: судим по расходу, а не по календарю. Флаг выключает
только action в обоих контурах (decision_policy и классическое дерево analyzer),
признак is_zero_leads_after_3d продолжает считаться для отчётов.
"""

from unittest.mock import patch

from agent.analyzer import apply_decision_tree
from services import autonomous_pause as autonomous
from services.decision_policy import score_and_decide


def _zero_lead_ad():
    # adset_id пустой: иначе страж «последняя реклама в адсете» переведёт единственный PAUSE в KEEP
    return {
        "ad_id": "1", "ad_name": "тест", "adset_id": "", "spend": 40.0, "leads": 0,
        "cpl": 0.0, "qual_pct": None, "payments": 0, "days_running": 4, "day_since_launch": 4,
        "outcomes_matched_at": None, "city": "CityA", "adset_type": "L2",
    }


def test_decision_policy_pauses_by_default_and_keeps_flag_when_disabled():
    on = score_and_decide([_zero_lead_ad()])[0]
    assert on["action"] == "PAUSE" and on["is_zero_leads_after_3d"] is True

    off = score_and_decide([_zero_lead_ad()], {"zero_leads_rule_enabled": False})[0]
    assert off["action"] != "PAUSE" and off["is_zero_leads_after_3d"] is True

    # только настоящий JSON false выключает правило
    weird = score_and_decide([_zero_lead_ad()], {"zero_leads_rule_enabled": "false"})[0]
    assert weird["action"] == "PAUSE"


def test_classic_tree_respects_switch():
    ad = {"spend": 60.0, "leads": 0, "days_running": 9, "cpl": 0, "romi": None, "qual_pct": None, "payments": 0}
    assert apply_decision_tree(ad)["action"] == "ОТКЛЮЧИТЬ"
    assert apply_decision_tree(ad, {"zero_leads_rule_enabled": False})["action"] != "ОТКЛЮЧИТЬ"


def test_approve_early_kill_uses_rule_specific_automation_rule():
    with patch("services.owner_action_repository.approve_by_system") as approve:
        assert autonomous.approve_early_kill("p1", rule="A", evidence={"business_reason": "x"}) is True
    kwargs = approve.call_args.kwargs
    assert kwargs["automation_rule"] == "EARLY_KILL_SPEND_CPL" and kwargs["actor"] == "early_kill"
    assert kwargs["reason_text"] == "x"
    assert autonomous.approve_early_kill("p1", rule="Z") is False
    with patch("services.owner_action_repository.approve_by_system", side_effect=RuntimeError("busy")):
        assert autonomous.approve_early_kill("p1", rule="A") is False
