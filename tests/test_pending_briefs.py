"""
Тесты services/pending_briefs.py — файловая очередь ТЗ на одобрение владельцем
(ARCH-brief-approval-flow.md).

Мокаем только файловую границу — PENDING_FILE переопределяется на tmp_path
в каждом тесте (monkeypatch). Никакой сети/Trello/Telegram здесь нет.
"""

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

_TZ_LOCAL = timezone(timedelta(hours=5))


@pytest.fixture(autouse=True)
def _tmp_pending_file(tmp_path, monkeypatch):
    """Каждый тест получает чистый временный PENDING_FILE."""
    import services.pending_briefs as pb
    monkeypatch.setattr(pb, "PENDING_FILE", tmp_path / "pending_briefs.json")
    yield


# ---------------------------------------------------------------------------
# add_pending
# ---------------------------------------------------------------------------

def test_add_pending_creates_record():
    """add_pending создаёт запись со status=pending, коротким id, created_at, signature."""
    import services.pending_briefs as pb

    record = pb.add_pending("CityA / Страх ошибки", "тело карточки", "PRODA",
                             "Страх ошибки::CityA::video_speaker")

    assert record["status"] == pb.STATUS_PENDING
    assert record["name"] == "CityA / Страх ошибки"
    assert record["desc"] == "тело карточки"
    assert record["product"] == "PRODA"
    assert record["signature"] == "Страх ошибки::CityA::video_speaker"
    assert record["id"] and len(record["id"]) == 6
    assert record["created_at"]
    assert record["decided_at"] is None
    assert record["card_id"] is None
    assert record["card_url"] is None


def test_add_pending_ids_unique():
    """Два add_pending подряд дают разные id."""
    import services.pending_briefs as pb

    r1 = pb.add_pending("A", "desc", None, "sig1")
    r2 = pb.add_pending("B", "desc", None, "sig2")

    assert r1["id"] != r2["id"]


def test_add_pending_preserves_existing_records():
    """mark_approved(X) затем add_pending(new) — X всё ещё approved, new присутствует."""
    import services.pending_briefs as pb

    old = pb.add_pending("Старое ТЗ", "desc", None, "sig-old")
    pb.mark_approved(old["id"], card_id="card1", card_url="https://trello.com/c/x")

    new = pb.add_pending("Новое ТЗ", "desc", None, "sig-new")

    data = pb._load()
    ids = {b["id"]: b for b in data["briefs"]}
    assert ids[old["id"]]["status"] == pb.STATUS_APPROVED
    assert ids[old["id"]]["card_id"] == "card1"
    assert new["id"] in ids


# ---------------------------------------------------------------------------
# callback_data <= 64 байта
# ---------------------------------------------------------------------------

def test_callback_data_within_64_bytes():
    """approve_brief:<id> не превышает лимит Telegram в 64 байта."""
    import services.pending_briefs as pb

    rec = pb.add_pending("Тема", "desc", None, "sig")
    cb = f"approve_brief:{rec['id']}".encode()

    assert len(cb) <= 64


def test_new_id_fallback_after_collisions_still_fits_64_bytes(monkeypatch):
    """Если 10 попыток token_hex(3) коллизируют с уже занятыми id, _new_id
    переходит на длинный fallback secrets.token_hex(_ID_LEN) без проверки —
    даже он укладывается в лимит callback_data <= 64 байта (approve_brief:<id>)."""
    import services.pending_briefs as pb

    calls = {"n": 0}

    def fake_token_hex(nbytes):
        calls["n"] += 1
        if nbytes == pb._ID_LEN:
            # fallback-ветка (после исчерпания 10 попыток) — длинный id
            return "b" * (pb._ID_LEN * 2)
        # короткая ветка (nbytes=3) — всегда "коллизия" с уже занятым id
        return "aaaaaa"

    monkeypatch.setattr(pb.secrets, "token_hex", fake_token_hex)

    new_id = pb._new_id({"aaaaaa"})

    assert new_id == "b" * (pb._ID_LEN * 2)
    cb = f"approve_brief:{new_id}".encode()
    assert len(cb) <= 64
    # Цикл действительно исчерпал все 10 попыток перед fallback (10 коллизий + 1 fallback-вызов)
    assert calls["n"] == 11


# ---------------------------------------------------------------------------
# get_brief
# ---------------------------------------------------------------------------

def test_get_brief_found_and_missing():
    """get_brief находит существующую запись и возвращает None для несуществующей."""
    import services.pending_briefs as pb

    rec = pb.add_pending("Тема", "desc", None, "sig")

    assert pb.get_brief(rec["id"])["name"] == "Тема"
    assert pb.get_brief("ffffff") is None


# ---------------------------------------------------------------------------
# active_signatures
# ---------------------------------------------------------------------------

def test_active_signatures_pending_and_approved_only():
    """active_signatures содержит только сигнатуры pending и approved."""
    import services.pending_briefs as pb

    r_pending = pb.add_pending("A", "desc", None, "sig-pending")
    r_approved = pb.add_pending("B", "desc", None, "sig-approved")
    r_rejected = pb.add_pending("C", "desc", None, "sig-rejected")
    r_expired = pb.add_pending("D", "desc", None, "sig-expired")

    pb.mark_approved(r_approved["id"], card_id="c1", card_url="u1")
    pb.mark_rejected(r_rejected["id"])
    # Помечаем "expired" напрямую через файл (expire_old тестируется отдельно)
    data = pb._load()
    for b in data["briefs"]:
        if b["id"] == r_expired["id"]:
            b["status"] = pb.STATUS_EXPIRED
    pb._save(data)

    sigs = pb.active_signatures()

    assert sigs == {"sig-pending", "sig-approved"}
    assert "sig-rejected" not in sigs
    assert "sig-expired" not in sigs
    assert r_pending["id"]  # использован, чтобы не ругался линтер на неиспользуемую переменную


# ---------------------------------------------------------------------------
# mark_approved / mark_rejected
# ---------------------------------------------------------------------------

def test_mark_approved_sets_fields():
    """mark_approved ставит status=approved, card_url, card_id, decided_at."""
    import services.pending_briefs as pb

    rec = pb.add_pending("Тема", "desc", None, "sig")

    ok = pb.mark_approved(rec["id"], card_id="card123", card_url="https://trello.com/c/card123")

    assert ok is True
    updated = pb.get_brief(rec["id"])
    assert updated["status"] == pb.STATUS_APPROVED
    assert updated["card_id"] == "card123"
    assert updated["card_url"] == "https://trello.com/c/card123"
    assert updated["decided_at"] is not None


def test_mark_rejected():
    """mark_rejected ставит status=rejected и возвращает True."""
    import services.pending_briefs as pb

    rec = pb.add_pending("Тема", "desc", None, "sig")

    ok = pb.mark_rejected(rec["id"])

    assert ok is True
    updated = pb.get_brief(rec["id"])
    assert updated["status"] == pb.STATUS_REJECTED
    assert updated["decided_at"] is not None


def test_mark_missing_returns_false():
    """mark_approved/mark_rejected по несуществующему id возвращают False."""
    import services.pending_briefs as pb

    assert pb.mark_approved("ffffff", card_id="x", card_url="y") is False
    assert pb.mark_rejected("ffffff") is False


# ---------------------------------------------------------------------------
# expire_old
# ---------------------------------------------------------------------------

def test_expire_old_flips_stale_pending():
    """pending старше RETENTION_DAYS -> expired, свежий остаётся pending."""
    import services.pending_briefs as pb

    now = datetime.now(_TZ_LOCAL)
    old_time = now - timedelta(days=pb.RETENTION_DAYS + 1)

    old_rec = pb.add_pending("Старая тема", "desc", None, "sig-old", now=old_time)
    fresh_rec = pb.add_pending("Свежая тема", "desc", None, "sig-fresh", now=now)

    count = pb.expire_old(now=now)

    assert count == 1
    assert pb.get_brief(old_rec["id"])["status"] == pb.STATUS_EXPIRED
    assert pb.get_brief(fresh_rec["id"])["status"] == pb.STATUS_PENDING


def test_expire_old_ignores_non_pending():
    """approved 100 дней назад остаётся approved — ретеншн только для pending."""
    import services.pending_briefs as pb

    now = datetime.now(_TZ_LOCAL)
    old_time = now - timedelta(days=100)

    rec = pb.add_pending("Старая одобренная", "desc", None, "sig-approved-old", now=old_time)
    pb.mark_approved(rec["id"], card_id="c1", card_url="u1")

    count = pb.expire_old(now=now)

    assert count == 0
    assert pb.get_brief(rec["id"])["status"] == pb.STATUS_APPROVED


def test_expire_old_ignores_rejected():
    """rejected 100 дней назад остаётся rejected — ретеншн только для pending
    (AC9: expire не трогает решённые записи, не только approved)."""
    import services.pending_briefs as pb

    now = datetime.now(_TZ_LOCAL)
    old_time = now - timedelta(days=100)

    rec = pb.add_pending("Старая отклонённая", "desc", None, "sig-rejected-old", now=old_time)
    pb.mark_rejected(rec["id"])

    count = pb.expire_old(now=now)

    assert count == 0
    assert pb.get_brief(rec["id"])["status"] == pb.STATUS_REJECTED


def test_expire_old_trims_to_max_records():
    """После expire_old очередь не превышает MAX_RECORDS (остаются последние)."""
    import services.pending_briefs as pb

    now = datetime.now(_TZ_LOCAL)
    for i in range(pb.MAX_RECORDS + 50):
        pb.add_pending(f"Тема {i}", "desc", None, f"sig-{i}", now=now)

    pb.expire_old(now=now)

    data = pb._load()
    assert len(data["briefs"]) == pb.MAX_RECORDS
    # Остались последние по порядку добавления
    assert data["briefs"][-1]["name"] == f"Тема {pb.MAX_RECORDS + 49}"


def test_expire_old_bad_created_at_skipped_not_crashed():
    """Битый created_at не роняет прогон — запись просто пропускается."""
    import services.pending_briefs as pb

    now = datetime.now(_TZ_LOCAL)
    rec = pb.add_pending("Тема", "desc", None, "sig", now=now)
    data = pb._load()
    for b in data["briefs"]:
        if b["id"] == rec["id"]:
            b["created_at"] = "не-дата"
    pb._save(data)

    count = pb.expire_old(now=now)

    assert count == 0
    assert pb.get_brief(rec["id"])["status"] == pb.STATUS_PENDING


# ---------------------------------------------------------------------------
# _load / _save — edge cases файловой границы
# ---------------------------------------------------------------------------

def test_load_missing_file_returns_empty():
    """Файла нет -> {"briefs": []}."""
    import services.pending_briefs as pb

    assert pb._load() == {"briefs": []}


def test_load_corrupt_json_returns_empty(tmp_path):
    """Битый JSON -> {"briefs": []}, не падает."""
    import services.pending_briefs as pb

    pb.PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    pb.PENDING_FILE.write_text("{не json", encoding="utf-8")

    assert pb._load() == {"briefs": []}


def test_save_is_atomic_and_reloadable():
    """_save затем _load — данные совпадают."""
    import services.pending_briefs as pb

    data = {"briefs": [{"id": "abc123", "name": "Тест", "status": "pending"}]}
    pb._save(data)

    loaded = pb._load()

    assert loaded == data


# ---------------------------------------------------------------------------
# Потокобезопасность
# ---------------------------------------------------------------------------

def test_concurrent_add_pending_no_lost_updates():
    """20 потоков, каждый делает add_pending -> join -> ровно 20 записей.

    Lock не даёт потерять апдейты (lost-update race). Без sleep — join
    детерминирован (ждём завершения всех потоков перед проверкой).
    """
    import services.pending_briefs as pb

    threads = []
    for i in range(20):
        t = threading.Thread(target=pb.add_pending, args=(f"Тема {i}", "desc", None, f"sig-{i}"))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    data = pb._load()
    assert len(data["briefs"]) == 20
    # Все id уникальны — не было гонки при генерации id
    ids = {b["id"] for b in data["briefs"]}
    assert len(ids) == 20
