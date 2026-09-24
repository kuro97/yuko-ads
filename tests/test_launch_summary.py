"""Сводка-воронка запусков: счётчики окна и текст сообщения."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import services.launch_summary as launch_summary
from services.database_migrations import apply_runtime_migrations

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=1)

IN_WINDOW = (NOW - timedelta(hours=5)).isoformat()
BEFORE_WINDOW = (SINCE - timedelta(hours=5)).isoformat()


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    apply_runtime_migrations(str(path))
    conn = sqlite3.connect(path)
    monkeypatch.setattr(launch_summary, "_DB_PATH", path)
    yield conn
    conn.close()


def _insert_proposal(
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    kind: str = "LAUNCH",
    created_at: str = IN_WINDOW,
) -> None:
    conn.execute(
        """
        INSERT INTO owner_action_proposals (
            proposal_id, proposal_kind, origin, idempotency_key, source_ref,
            requested_by_actor, summary, plan_json, plan_sha256, targets_sha256,
            evidence_sha256, config_version_sha256, proposal_sha256,
            created_at, valid_until
        ) VALUES (?, ?, 'CRON', ?, 'test', 'test', 'test', '{}', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            proposal_id,
            kind,
            f"idem-{proposal_id}",
            _sha("plan"),
            _sha("targets"),
            _sha("evidence"),
            _sha("config"),
            _sha(f"proposal-{proposal_id}"),
            created_at,
            (NOW + timedelta(days=2)).isoformat(),
        ),
    )
    conn.commit()


def _insert_event(
    conn: sqlite3.Connection,
    proposal_id: str,
    event_type: str,
    *,
    reason_code: str | None = None,
    created_at: str = IN_WINDOW,
    seq: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO owner_action_events (
            event_id, proposal_id, event_seq, event_type, actor, reason_code,
            payload_json, payload_sha256, created_at
        ) VALUES (?, ?, ?, ?, 'test', ?, '{}', ?, ?)
        """,
        (
            f"evt-{proposal_id}-{event_type}-{seq}",
            proposal_id,
            seq,
            event_type,
            reason_code,
            _sha("payload"),
            created_at,
        ),
    )
    conn.commit()


def _insert_system_approve(
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    recorded_at: str = IN_WINDOW,
) -> None:
    conn.execute(
        """
        INSERT INTO owner_action_decisions (
            decision_id, proposal_id, proposal_sha256, decision_kind,
            reason_text, recorded_at, decision_source, automation_rule
        ) VALUES (?, ?, ?, 'APPROVE', 'test', ?, 'SYSTEM', 'TEST_RULE')
        """,
        (f"dec-{proposal_id}", proposal_id, _sha(f"proposal-{proposal_id}"), recorded_at),
    )
    conn.commit()


def _insert_lifecycle(
    conn: sqlite3.Connection,
    proposal_id: str,
    state: str,
    *,
    updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO owner_action_lifecycle (proposal_id, state, updated_at)
        VALUES (?, ?, ?)
        """,
        (proposal_id, state, updated_at),
    )
    conn.commit()


def test_funnel_counts_window(db):
    # P1: предложен в окне, самоодобрен, отклонён live-review, висит в
    # RECONCILE_REQUIRED дольше порога.
    _insert_proposal(db, "p1")
    _insert_system_approve(db, "p1")
    _insert_event(db, "p1", "LIFECYCLE_BLOCKED_STALE", reason_code="LIVE_REVIEW_DENIED")
    _insert_lifecycle(
        db, "p1", "RECONCILE_REQUIRED",
        updated_at=(NOW - timedelta(hours=8)).isoformat(),
    )

    # P0: создан до окна, истёк в окне без одобрения.
    _insert_proposal(db, "p0", created_at=BEFORE_WINDOW)
    _insert_event(db, "p0", "LIFECYCLE_EXPIRED", reason_code="PROPOSAL_EXPIRED")

    # P2: одобрен до окна, истёк в окне — «одобрено, но не исполнено».
    _insert_proposal(db, "p2", created_at=BEFORE_WINDOW)
    _insert_system_approve(db, "p2", recorded_at=BEFORE_WINDOW)
    _insert_event(db, "p2", "LIFECYCLE_EXPIRED", reason_code="PROPOSAL_EXPIRED")

    # P3: подтверждённый запуск в окне.
    _insert_proposal(db, "p3")
    _insert_event(db, "p3", "LIFECYCLE_VERIFIED")

    # PAUSE-предложение не должно протекать в воронку запусков.
    _insert_proposal(db, "pause1", kind="PAUSE")
    _insert_event(
        db, "pause1", "LIFECYCLE_BLOCKED_STALE", reason_code="LIVE_REVIEW_DENIED"
    )

    # Созданные объявления: два в окне, одно до окна.
    for index, created_at in enumerate((IN_WINDOW, IN_WINDOW, BEFORE_WINDOW)):
        db.execute(
            """
            INSERT INTO launch_check_audit (
                event_id, check_id, event_type, source, card_id, actor, created_at
            ) VALUES (?, 'check-1', 'CREATE_CONFIRMED', 'MANUAL', 'card-1', 'test', ?)
            """,
            (f"audit-{index}", created_at),
        )
    db.commit()

    funnel = launch_summary.collect_launch_funnel(SINCE, NOW)

    assert funnel["proposed"] == 2  # p1 и p3; pause не считается, p0/p2 до окна
    assert funnel["approved_system"] == 1  # p1; p2 одобрен до окна
    assert funnel["approved_owner"] == 0
    assert funnel["review_denied"] == 1  # p1; pause1 не считается
    assert funnel["review_failed"] == 0
    assert funnel["expired_approved"] == 1  # p2
    assert funnel["expired_unapproved"] == 1  # p0
    assert funnel["created_ads"] == 2
    assert funnel["verified"] == 1  # p3
    assert funnel["hanging"] == 1  # p1


def test_hanging_ignores_fresh_executions(db):
    _insert_proposal(db, "fresh")
    _insert_lifecycle(
        db, "fresh", "ATTEMPT_STARTED",
        updated_at=(NOW - timedelta(hours=1)).isoformat(),
    )
    funnel = launch_summary.collect_launch_funnel(SINCE, NOW)
    assert funnel["hanging"] == 0


def test_hanging_counts_stale_approved(db):
    # «Одобрено, но диспетчер так и не взял» — висяк, а не норма.
    _insert_proposal(db, "stale-approved")
    _insert_lifecycle(
        db, "stale-approved", "APPROVED",
        updated_at=(NOW - timedelta(hours=8)).isoformat(),
    )
    funnel = launch_summary.collect_launch_funnel(SINCE, NOW)
    assert funnel["hanging"] == 1


def test_send_alerts_when_collect_fails(db, monkeypatch):
    sent: list[str] = []

    import services.notifications

    monkeypatch.setattr(
        services.notifications, "send_telegram", lambda text: sent.append(text) or True
    )
    monkeypatch.setattr(
        launch_summary,
        "collect_launch_funnel",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert launch_summary.send_launch_summary(NOW) is False
    assert len(sent) == 1
    assert "не собралась" in sent[0]


def test_format_zero_funnel_says_it_loudly(db):
    funnel = launch_summary.collect_launch_funnel(SINCE, NOW)
    text = launch_summary.format_launch_summary(funnel)
    assert "Предложено запусков: 0" in text
    assert "не сделал ничего" in text


def test_format_full_funnel(db):
    funnel = {
        "proposed": 5,
        "approved_system": 4,
        "approved_owner": 1,
        "review_denied": 3,
        "review_failed": 1,
        "expired_approved": 2,
        "expired_unapproved": 1,
        "created_ads": 6,
        "verified": 1,
        "hanging": 2,
        "hanging_oldest": "2026-08-31T05:35:43+00:00",
    }
    text = launch_summary.format_launch_summary(funnel)
    assert "Предложено запусков: 5" in text
    assert "Одобрено: 5 (из них кнопкой: 1)" in text
    assert "Создано объявлений: 6" in text
    assert "отклонено проверкой перед исполнением: 3" in text
    assert "одобрено, но не исполнено до истечения: 2" in text
    assert "Висит без подтверждения: 2" in text
    assert "31.08" in text
    assert "не сделал ничего" not in text


def test_send_sends_even_when_funnel_is_empty(db, monkeypatch):
    sent: list[str] = []

    def fake_send(text: str) -> bool:
        sent.append(text)
        return True

    import services.notifications

    monkeypatch.setattr(services.notifications, "send_telegram", fake_send)
    assert launch_summary.send_launch_summary(NOW) is True
    assert len(sent) == 1
    assert "Запуски за сутки" in sent[0]


# --- Этап 3b: алерт застоя и стоп-кнопки -------------------------------------


def test_stall_alert_when_proposals_but_no_creations(db):
    _insert_proposal(db, "stall-1", created_at=(NOW - timedelta(hours=30)).isoformat())
    _insert_proposal(db, "stall-2", created_at=IN_WINDOW)
    funnel = launch_summary.collect_launch_funnel(SINCE, NOW)
    assert funnel["stall_proposed"] == 2
    assert funnel["stall_created"] == 0
    text = launch_summary.format_launch_summary(funnel)
    assert "конвейер запусков стоит" in text


def test_no_stall_alert_when_ads_created(db):
    _insert_proposal(db, "ok-1", created_at=IN_WINDOW)
    db.execute(
        """
        INSERT INTO launch_check_audit (
            event_id, check_id, event_type, source, card_id, actor, created_at
        ) VALUES ('audit-ok', 'check-1', 'CREATE_CONFIRMED', 'MANUAL', 'card-1', 'test', ?)
        """,
        (IN_WINDOW,),
    )
    db.commit()
    text = launch_summary.format_launch_summary(
        launch_summary.collect_launch_funnel(SINCE, NOW)
    )
    assert "конвейер запусков стоит" not in text


def test_collect_stop_buttons_only_fresh_unstopped(monkeypatch):
    import services.auto_launch as auto_launch

    stop_map = {
        "card-fresh": {
            "name": "Свежая карточка про мотивацию",
            "ad_ids": ["ad-1"],
            "stopped": False,
            "at": (NOW - timedelta(hours=2)).isoformat(),
        },
        "card-stopped": {
            "name": "Уже остановленная",
            "ad_ids": ["ad-2"],
            "stopped": True,
            "at": (NOW - timedelta(hours=3)).isoformat(),
        },
        "card-old": {
            "name": "Старый запуск",
            "ad_ids": ["ad-3"],
            "stopped": False,
            "at": (NOW - timedelta(days=5)).isoformat(),
        },
    }
    monkeypatch.setattr(
        auto_launch, "_load_auto_launch_state", lambda: {"stop_map": stop_map}
    )
    buttons = launch_summary.collect_stop_buttons(SINCE, NOW)
    assert len(buttons) == 1
    label, callback = buttons[0][0]
    assert callback == "stop_launch:card-fresh"
    assert "Остановить" in label


def test_send_uses_buttons_when_present(db, monkeypatch):
    sent: dict[str, object] = {}

    import services.telegram_bot

    monkeypatch.setattr(
        services.telegram_bot,
        "send_with_buttons",
        lambda text, buttons: sent.update(text=text, buttons=buttons) or True,
    )
    monkeypatch.setattr(
        launch_summary,
        "collect_stop_buttons",
        lambda since, until: [[("⏸ Остановить «Тест»", "stop_launch:card-x")]],
    )
    assert launch_summary.send_launch_summary(NOW) is True
    assert sent["buttons"] == [[("⏸ Остановить «Тест»", "stop_launch:card-x")]]
    assert "Запуски за сутки" in sent["text"]
