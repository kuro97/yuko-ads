"""Тесты стража повторных заявок (services/repeat_lead_guard.py).

AMO изолирован моками, state — tmp_path. Проверяем: распознавание сброса
«менеджер → Биржа» интеграцией, отличие fblead-повторной от штатных потоков
(«НОВАЯ СДЕЛКА», SLA-казан), политику возврата (безусловный, задача только
живому менеджеру), dry-run без записи и идемпотентность по state.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

import services.repeat_lead_guard as guard

MANAGER = 9429023      # исходный менеджер (менеджер из лида-примера 34904254)
OTHER_MANAGER = 9156439
# сброс «10 минут назад»: внутри TTL стейта, иначе прунер сотрёт обработанность
FLIP_TS = int(time.time()) - 600


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """State во временный файл — боевой data/ не трогаем."""
    monkeypatch.setattr(guard, "STATE_FILE", tmp_path / "repeat_lead_guard_state.json")


def _event(lead_id=34904254, before=MANAGER, after=guard.BIRZHA_USER_ID, by=0, ts=FLIP_TS):
    return {
        "type": "entity_responsible_changed",
        "entity_id": lead_id,
        "created_by": by,
        "created_at": ts,
        "value_after": [{"responsible_user": {"id": after}}],
        "value_before": [{"responsible_user": {"id": before}}],
    }


def _repeat_note(ts=FLIP_TS, text="ПОВТОРНАЯ ЗАЯВКА В ЦЕЛЕВОЙ ВОРОНКЕ\nИсточник: fblead.com\nИмя: Тест"):
    return {"note_type": "common", "created_at": ts, "params": {"text": text}}


def _lead(lead_id=34904254, status=44520175, resp=guard.BIRZHA_USER_ID,
          pipeline=guard.PIPELINE_NOVYE_PRODAZHI):
    return {"id": lead_id, "status_id": status, "responsible_user_id": resp,
            "pipeline_id": pipeline}


def _flip(lead_id=34904254, prev=MANAGER, ts=FLIP_TS):
    return {"lead_id": lead_id, "flip_ts": ts, "prev_responsible_id": prev}


# --- разбор событий ---

def test_flip_from_event_valid_reset_parsed():
    flip = guard._flip_from_event(_event())
    assert flip == {"lead_id": 34904254, "flip_ts": FLIP_TS, "prev_responsible_id": MANAGER}


def test_flip_from_event_ignores_manual_and_non_birzha():
    assert guard._flip_from_event(_event(by=MANAGER)) is None          # ручной перевод
    assert guard._flip_from_event(_event(after=OTHER_MANAGER)) is None  # раздача, не сброс
    assert guard._flip_from_event(_event(before=guard.BIRZHA_USER_ID)) is None  # биржа → биржа
    assert guard._flip_from_event({"created_by": 0, "value_after": []}) is None  # битое событие


# --- распознавание заметки fblead ---

def test_has_repeat_note_matches_both_fblead_variants():
    assert guard.has_repeat_note([_repeat_note()], FLIP_TS)
    on_stage = _repeat_note(text="ПОВТОРНАЯ ЗАЯВКА НА ЦЕЛЕВОМ ЭТАПЕ\nИсточник: fblead.com")
    assert guard.has_repeat_note([on_stage], FLIP_TS)


def test_has_repeat_note_rejects_regular_flows_and_far_notes():
    new_deal = _repeat_note(text="НОВАЯ СДЕЛКА\nИсточник: fblead.com\nИмя: Тест")
    sla = _repeat_note(text="Возвращён в общий пул. | Причина: SLA статуса превышен")
    far = _repeat_note(ts=FLIP_TS + guard.NOTE_MATCH_WINDOW_SEC + 1)
    no_source = _repeat_note(text="ПОВТОРНАЯ ЗАЯВКА В ЦЕЛЕВОЙ ВОРОНКЕ\nИсточник: другое")
    assert not guard.has_repeat_note([new_deal, sla, far, no_source], FLIP_TS)
    assert not guard.has_repeat_note([], FLIP_TS)


# --- политика решения ---

def test_plan_restore_while_still_on_birzha_patches_and_tasks():
    plan = guard.plan_action(_flip(), _lead(), [_repeat_note()])
    assert plan["decision"] == "restore"
    assert plan["need_patch"] is True
    assert plan["task_user_id"] == MANAGER


def test_plan_restore_unconditional_after_redistribution():
    # распределитель успел отдать лид другому — возвращаем исходному всё равно
    plan = guard.plan_action(_flip(), _lead(resp=OTHER_MANAGER), [_repeat_note()])
    assert plan["decision"] == "restore"
    assert plan["need_patch"] is True


def test_plan_manager_already_took_back_needs_task_only():
    plan = guard.plan_action(_flip(), _lead(resp=MANAGER), [_repeat_note()])
    assert plan["decision"] == "restore"
    assert plan["need_patch"] is False
    assert plan["task_user_id"] == MANAGER


def test_plan_pseudo_user_restored_without_task():
    # онлайн-очередь возвращаем, но задачу псевдо-юзеру не ставим
    flip = _flip(prev=guard.ACME_ONLINE_USER_ID)
    plan = guard.plan_action(flip, _lead(), [_repeat_note()])
    assert plan["decision"] == "restore"
    assert plan["task_user_id"] is None


def test_plan_skips_closed_missing_and_unmarked_leads():
    assert guard.plan_action(_flip(), _lead(status=143), [_repeat_note()])["reason"] == "lead_closed"
    assert guard.plan_action(_flip(), None, [])["reason"] == "lead_missing"
    assert guard.plan_action(_flip(), _lead(), [])["reason"] == "no_repeat_note"


def test_plan_skips_foreign_pipeline():
    # сбросы на биржу случаются и в чужих воронках — fblead там ни при чём
    foreign = _lead(pipeline=5050320)
    assert guard.plan_action(_flip(), foreign, [_repeat_note()])["reason"] == "foreign_pipeline"


def test_fetch_lead_deleted_204_returns_none(monkeypatch):
    # удалённый лид: AMO отвечает 204 без тела, _amo_get отдаёт форму без id
    monkeypatch.setattr(guard, "_amo_get", lambda *a, **k: {"_embedded": {"leads": []}})
    assert guard._fetch_lead(47330191) is None


# --- прогон целиком ---

def _wire(monkeypatch, flips, leads, notes_by_lead):
    """Мокает AMO-границу. Возвращает (patch_mock, post_mock)."""
    monkeypatch.setattr(guard, "fetch_responsible_flips", lambda minutes: flips)
    monkeypatch.setattr(guard, "_fetch_lead", lambda lid: leads.get(lid))
    monkeypatch.setattr(guard, "_fetch_lead_notes", lambda lid: notes_by_lead.get(lid, []))
    patch = MagicMock()
    post = MagicMock()
    monkeypatch.setattr(guard, "_amo_patch", patch)
    monkeypatch.setattr(guard, "_amo_post", post)
    return patch, post


def test_run_dry_run_writes_nothing(monkeypatch):
    patch, post = _wire(
        monkeypatch,
        flips=[_flip()],
        leads={34904254: _lead()},
        notes_by_lead={34904254: [_repeat_note()]},
    )
    stats = guard.run(apply=False)

    assert stats["flips"] == 1
    patch.assert_not_called()
    post.assert_not_called()
    assert not guard.STATE_FILE.exists()


def test_run_apply_restores_manager_and_creates_task(monkeypatch):
    patch, post = _wire(
        monkeypatch,
        flips=[_flip()],
        leads={34904254: _lead()},
        notes_by_lead={34904254: [_repeat_note()]},
    )
    stats = guard.run(apply=True)

    assert stats["restored"] == 1 and stats["tasks"] == 1
    patch.assert_called_once_with("leads/34904254", {"responsible_user_id": MANAGER})
    task = post.call_args.args[1][0]
    assert task["entity_id"] == 34904254
    assert task["responsible_user_id"] == MANAGER
    assert task["entity_type"] == "leads"


def test_run_apply_is_idempotent_across_runs(monkeypatch):
    patch, post = _wire(
        monkeypatch,
        flips=[_flip()],
        leads={34904254: _lead()},
        notes_by_lead={34904254: [_repeat_note()]},
    )
    guard.run(apply=True)
    stats2 = guard.run(apply=True)

    assert patch.call_count == 1 and post.call_count == 1
    assert stats2["restored"] == 0 and stats2["tasks"] == 0


def test_run_two_flips_same_lead_acts_once_on_freshest(monkeypatch):
    # порядок подачи перемешан — страж сам сортирует и действует по свежему сбросу
    patch, post = _wire(
        monkeypatch,
        flips=[_flip(ts=FLIP_TS, prev=OTHER_MANAGER), _flip(ts=FLIP_TS + 600, prev=MANAGER)],
        leads={34904254: _lead()},
        notes_by_lead={34904254: [_repeat_note(ts=FLIP_TS + 600), _repeat_note()]},
    )
    stats = guard.run(apply=True)

    assert stats["restored"] == 1
    patch.assert_called_once_with("leads/34904254", {"responsible_user_id": MANAGER})
    # оба сброса помечены обработанными — второй прогон молчит
    stats2 = guard.run(apply=True)
    assert patch.call_count == 1 and stats2["restored"] == 0


def test_run_skips_sla_and_newborn_flows_without_touching(monkeypatch):
    sla_note = _repeat_note(text="Возвращён в общий пул. | Причина: SLA статуса превышен")
    patch, post = _wire(
        monkeypatch,
        flips=[_flip(lead_id=101), _flip(lead_id=102)],
        leads={101: _lead(lead_id=101), 102: _lead(lead_id=102, status=143)},
        notes_by_lead={101: [sla_note], 102: [_repeat_note()]},
    )
    stats = guard.run(apply=True)

    patch.assert_not_called()
    post.assert_not_called()
    assert stats["skipped"] == {"no_repeat_note": 1, "lead_closed": 1}


def test_run_limit_caps_actions_per_pass(monkeypatch):
    flips = [_flip(lead_id=200 + i) for i in range(5)]
    leads = {200 + i: _lead(lead_id=200 + i) for i in range(5)}
    notes = {200 + i: [_repeat_note()] for i in range(5)}
    patch, post = _wire(monkeypatch, flips=flips, leads=leads, notes_by_lead=notes)

    stats = guard.run(apply=True, limit=2)

    assert stats["restored"] == 2
    assert patch.call_count == 2


def test_run_task_failure_leaves_flip_unprocessed_for_retry(monkeypatch):
    patch, post = _wire(
        monkeypatch,
        flips=[_flip()],
        leads={34904254: _lead()},
        notes_by_lead={34904254: [_repeat_note()]},
    )
    post.side_effect = Exception("AMO POST ошибка (500)")
    stats = guard.run(apply=True)
    assert stats["errors"] == 1

    # следующий прогон: менеджер уже возвращён (патч прошёл) — дошлёт только задачу
    post.side_effect = None
    leads_after = {34904254: _lead(resp=MANAGER)}
    monkeypatch.setattr(guard, "_fetch_lead", lambda lid: leads_after.get(lid))
    stats2 = guard.run(apply=True)

    assert stats2["tasks"] == 1
    assert patch.call_count == 1  # повторного PATCH не было
