"""Тесты стража тега лид-формы (services/form_tag_guard.py).

AMO изолирован моками. Проверяем: политику plan_tag (своя форма / чужая /
без поля / уже помеченная), сохранение существующих тегов при PATCH, гонку
«тег появился между выборкой и записью», dry-run без записи, лимит прогона
и дедуп кандидатов.
"""

from __future__ import annotations

import pytest

import services.form_tag_guard as guard

FORM_A = "2858269299251592"   # лид-форма A → form_a_tag
FORM_B = "1285256302102941"   # лид-форма B → form_b_tag


def _lead(lead_id=30254902, form_id=FORM_A, tags=("fb_lead", "On", "dir_a")):
    fields = []
    if form_id is not None:
        fields.append({
            "field_id": guard.FIELD_FB_FORM_ID,
            "field_name": "fb_form_id",
            "values": [{"value": form_id}],
        })
    return {
        "id": lead_id,
        "custom_fields_values": fields,
        "_embedded": {"tags": [{"name": t} for t in tags]},
    }


class FakeAmo:
    """Фейк AMO: страницы /leads по вызовам + одиночные лиды по id."""

    def __init__(self, pages: list[list[dict]], by_id: dict[int, dict] | None = None):
        self.pages = list(pages)
        self.by_id = by_id or {}
        self.patches: list[tuple[str, dict]] = []
        self.list_calls = 0

    def get(self, endpoint, params=None):
        if endpoint.startswith("leads/"):
            lead = self.by_id.get(int(endpoint.split("/")[1]))
            return dict(lead) if lead else {}
        self.list_calls += 1
        page = (params or {}).get("page", 1)
        batch = self.pages[page - 1] if page <= len(self.pages) else []
        return {"_embedded": {"leads": batch}}

    def patch(self, endpoint, body):
        self.patches.append((endpoint, body))
        return {}


@pytest.fixture
def amo(monkeypatch):
    def _install(pages, by_id=None):
        fake = FakeAmo(pages, by_id)
        monkeypatch.setattr(guard, "_amo_get", fake.get)
        monkeypatch.setattr(guard, "_amo_patch", fake.patch)
        monkeypatch.setattr(guard, "WRITE_PAUSE_SEC", 0)
        return fake
    return _install


# ─── plan_tag: политика ──────────────────────────────────────────────────────

def test_plan_tag_maps_form_to_tag():
    assert guard.plan_tag(_lead(form_id=FORM_A)) == {
        "decision": "tag", "reason": "form_matched", "tag": "form_a_tag",
    }
    assert guard.plan_tag(_lead(form_id=FORM_B, tags=("fb_lead",)))["tag"] == "form_b_tag"


def test_plan_tag_skips_already_tagged_ignoring_case():
    lead = _lead(tags=("fb_lead", "dir_a", "FORM_A_TAG"))
    assert guard.plan_tag(lead) == {"decision": "skip", "reason": "already_tagged"}


def test_plan_tag_skips_foreign_form():
    assert guard.plan_tag(_lead(form_id="953081145432003"))["reason"] == "foreign_form"


def test_plan_tag_skips_lead_without_form_field():
    assert guard.plan_tag(_lead(form_id=None))["reason"] == "no_form_field"


def test_plan_tag_skips_empty_form_value():
    lead = _lead()
    lead["custom_fields_values"][0]["values"] = [{"value": ""}]
    assert guard.plan_tag(lead)["reason"] == "no_form_field"


# ─── add_tag: запись ─────────────────────────────────────────────────────────

def test_add_tag_keeps_existing_tags(amo):
    lead = _lead(tags=("fb_lead", "On", "dir_a", "tg_group"))
    fake = amo([], {30254902: lead})

    assert guard.add_tag(30254902, "form_a_tag") is True

    endpoint, body = fake.patches[0]
    assert endpoint == "leads/30254902"
    assert [t["name"] for t in body["_embedded"]["tags"]] == [
        "fb_lead", "On", "dir_a", "tg_group", "form_a_tag",
    ]


def test_add_tag_rereads_lead_and_skips_if_tag_appeared(amo):
    """Между выборкой и записью тег поставила интеграция — писать нечего."""
    fresh = _lead(tags=("fb_lead", "dir_a", "form_a_tag"))
    fake = amo([], {30254902: fresh})

    assert guard.add_tag(30254902, "form_a_tag") is False
    assert fake.patches == []


def test_add_tag_skips_vanished_lead(amo):
    fake = amo([], {})
    assert guard.add_tag(30254902, "form_a_tag") is False
    assert fake.patches == []


# ─── run: прогон ─────────────────────────────────────────────────────────────

def test_run_dry_run_writes_nothing(amo):
    fake = amo([[_lead(1), _lead(2, form_id=FORM_B, tags=("fb_lead",))]])

    stats = guard.run(window_minutes=60, apply=False)

    assert stats["candidates"] == 2
    assert stats["tagged"] == 0
    assert fake.patches == []


def test_run_applies_tags_and_counts_skips(amo):
    leads = [
        _lead(1),                                                  # без тега → ставим
        _lead(2, tags=("fb_lead", "dir_a", "form_a_tag")),        # уже помечен
        _lead(3, form_id="953081145432003"),                       # чужая форма
        _lead(4, form_id=None),                                    # не FB-лид
    ]
    fake = amo([leads], {L["id"]: L for L in leads})

    stats = guard.run(window_minutes=60, apply=True)

    assert stats["scanned"] == 4
    assert stats["candidates"] == 1
    assert stats["tagged"] == 1
    assert stats["skipped"] == {"already_tagged": 1, "foreign_form": 1, "no_form_field": 1}
    assert [e for e, _ in fake.patches] == ["leads/1"]


def test_run_respects_limit_and_reports_full_candidate_count(amo):
    leads = [_lead(i) for i in range(1, 6)]
    fake = amo([leads], {L["id"]: L for L in leads})

    stats = guard.run(window_minutes=60, apply=True, limit=2)

    assert stats["candidates"] == 5      # усечение видно в статистике, а не молча
    assert stats["tagged"] == 2
    assert len(fake.patches) == 2


def test_run_backfill_searches_every_configured_form(amo):
    """Бэкфилл идёт по всем формам словаря, дубли сделок схлопываются."""
    lead_a, lead_b = _lead(1, form_id=FORM_A), _lead(2, form_id=FORM_B, tags=("fb_lead",))
    fake = amo([[lead_a, lead_b], [lead_a]], {1: lead_a, 2: lead_b})

    stats = guard.run(backfill=True, apply=True)

    assert stats["scanned"] == 2
    assert sorted(e for e, _ in fake.patches) == ["leads/1", "leads/2"]
    assert [t["name"] for _, b in fake.patches for t in b["_embedded"]["tags"]][-1] == "form_b_tag"


def test_fetch_window_stops_on_short_page(amo):
    """Неполная страница = выдача кончилась, за следующей не ходим."""
    fake = amo([[_lead(1)], [_lead(2)]])

    leads = guard.fetch_leads_window(60)

    assert [L["id"] for L in leads] == [1]
    assert fake.list_calls == 1


def test_run_counts_integration_overtaking_us_separately(amo):
    """Тег появился между выборкой и записью — это обгон, а не обычный пропуск."""
    stale = _lead(1, tags=("fb_lead", "dir_a"))            # в выдаче тега ещё нет
    fresh = _lead(1, tags=("fb_lead", "dir_a", "form_a_tag"))  # к записи он уже есть
    fake = amo([[stale]], {1: fresh})

    stats = guard.run(window_minutes=60, apply=True)

    assert stats["candidates"] == 1
    assert stats["tagged"] == 0
    assert stats["skipped"] == {"tagged_meanwhile": 1}
    assert fake.patches == []
