"""Настоящие межпроцессные проверки SQLite launch repository."""

from __future__ import annotations

import hashlib
import multiprocessing
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import creative_intelligence as ci
from services import launch_repository as repository


NOW = datetime(2026, 7, 22, 6, 0, tzinfo=timezone.utc)
MEDIA_SHA = "a" * 64


def _plan(
    auth_id: str,
    card_id: str,
    card_name: str,
    cities: tuple[tuple[str, str], ...],
) -> SimpleNamespace:
    expected_names = {
        city: (f"{city} | {card_name} [PRODB]",) for city, _adset_id in cities
    }
    return SimpleNamespace(
        check_id=f"check-{auth_id}",
        card_id=card_id,
        card_name=card_name,
        request=SimpleNamespace(
            source="MANUAL",
            campaign_type="L1",
            actor="multiprocess-test",
            override_topic_veto=False,
            override_reason=None,
        ),
        media_sha256=MEDIA_SHA,
        targets=tuple(
            SimpleNamespace(
                city=city,
                ordinal=ordinal,
                account_kind="offline",
                account_id="act-multiprocess",
                adset_id=adset_id,
                reserved_slots=1,
            )
            for ordinal, (city, adset_id) in enumerate(cities)
        ),
        expected_names_by_city=expected_names,
        authorization=SimpleNamespace(auth_id=auth_id, secret="process-secret"),
    )


def _use_db(db_path: str) -> None:
    ci.DB_PATH = db_path


def _reserve(plan: SimpleNamespace) -> None:
    repository.reserve_authorization(
        plan,
        hashlib.sha256(plan.authorization.secret.encode()).hexdigest(),
        NOW,
    )


def _reserve_worker(db_path, start, queue, auth_id, card_id, card_name, cities):
    _use_db(db_path)
    start.wait(10)
    try:
        _reserve(_plan(auth_id, card_id, card_name, cities))
        queue.put((auth_id, "reserved"))
    except repository.LaunchRepositoryBlocked as exc:
        queue.put((auth_id, exc.code))
    except BaseException as exc:  # pragma: no cover - диагностика child process
        queue.put((auth_id, f"error:{type(exc).__name__}:{exc}"))


def _last_slots_worker(
    db_path,
    locks_path,
    start,
    queue,
    auth_id,
    card_id,
    card_name,
):
    _use_db(db_path)
    from services import adset_pause_guard

    adset_pause_guard._LOCKS_DIR = Path(locks_path)
    start.wait(10)
    try:
        with adset_pause_guard.adset_mutation_lock("91001"):
            other_reserved = repository.get_reserved_slots("91001", NOW)
            # В live уже 48/50; каждая карточка просит последние два слота.
            if 48 + other_reserved + 2 > 50:
                queue.put((auth_id, "CAPACITY_BLOCKED"))
                return
            plan = _plan(
                auth_id,
                card_id,
                card_name,
                (("CityA", "91001"),),
            )
            plan.expected_names_by_city["CityA"] = (
                f"CityA | {card_name} / 1 [PRODB]",
                f"CityA | {card_name} / 2 [PRODB]",
            )
            plan.targets[0].reserved_slots = 2
            _reserve(plan)
            queue.put((auth_id, "reserved"))
    except BaseException as exc:  # pragma: no cover - диагностика child process
        queue.put((auth_id, f"error:{type(exc).__name__}:{exc}"))


def _reverse_order_worker(
    db_path,
    locks_path,
    start,
    queue,
    auth_id,
    card_id,
    card_name,
    cities,
):
    _use_db(db_path)
    from services import adset_pause_guard

    adset_pause_guard._LOCKS_DIR = Path(locks_path)
    start.wait(10)
    try:
        with ExitStack() as stack:
            for adset_id in sorted({adset_id for _city, adset_id in cities}, key=int):
                stack.enter_context(adset_pause_guard.adset_mutation_lock(adset_id))
            _reserve(_plan(auth_id, card_id, card_name, cities))
        queue.put((auth_id, "reserved"))
    except BaseException as exc:  # pragma: no cover - диагностика child process
        queue.put((auth_id, f"error:{type(exc).__name__}:{exc}"))


def _audit_worker(db_path, start, queue, index):
    _use_db(db_path)
    start.wait(10)
    try:
        repository.append_launch_audit(
            repository.LaunchAuditEvent(
                event_id=f"multiprocess-audit-{index}",
                check_id=f"check-{index}",
                event_type="CANDIDATE",
                source="CRON",
                card_id=f"card-{index}",
                actor="system",
                created_at=NOW,
            )
        )
        queue.put((index, "appended"))
    except BaseException as exc:  # pragma: no cover - диагностика child process
        queue.put((index, f"error:{type(exc).__name__}:{exc}"))


def _claim_worker(db_path, start, queue, auth_id, city, adset_id, ad_name):
    _use_db(db_path)
    start.wait(10)
    proof = SimpleNamespace(auth_id=auth_id, secret="process-secret")
    scope = SimpleNamespace(
        account_kind="offline",
        account_id="act-multiprocess",
        city=city,
        adset_id=adset_id,
        ad_name=ad_name,
        media_sha256=MEDIA_SHA,
    )
    try:
        claim_id = repository.claim_provider_create(proof, scope, NOW)
        queue.put((claim_id, "claimed"))
    except repository.LaunchRepositoryBlocked as exc:
        queue.put((auth_id, exc.code))
    except BaseException as exc:  # pragma: no cover - диагностика child process
        queue.put((auth_id, f"error:{type(exc).__name__}:{exc}"))


@pytest.fixture
def process_db(tmp_path):
    db_path = str(tmp_path / "launch-multiprocess.db")
    ci.DB_PATH = None
    ci.init_kb(db_path)
    yield db_path, str(tmp_path / "locks")
    ci.DB_PATH = None


def _run_processes(target, arguments):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    processes = [
        context.Process(target=target, args=(start, queue, *args))
        for args in arguments
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=30) for _process in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
        assert not process.is_alive()
    return results


def test_multiprocess_same_identity_has_one_winner(process_db):
    db_path, _locks_path = process_db
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    common = (("CityA", "90001"), ("CityB", "90002"))
    processes = [
        context.Process(
            target=_reserve_worker,
            args=(
                db_path,
                start,
                queue,
                f"auth-identity-{index}",
                f"card-identity-{index}",
                "Одинаковый root",
                common,
            ),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=30)[1] for _process in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert sorted(results) == ["DUPLICATE_RESERVED", "reserved"]


def test_multiprocess_last_slots_allow_exactly_one_reservation(process_db):
    db_path, locks_path = process_db
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    processes = [
        context.Process(
            target=_last_slots_worker,
            args=(
                db_path,
                locks_path,
                start,
                queue,
                f"auth-capacity-{index}",
                f"card-capacity-{index}",
                f"Capacity {index}",
            ),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=30)[1] for _process in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert sorted(results) == ["CAPACITY_BLOCKED", "reserved"]
    assert repository.get_reserved_slots("91001", NOW) == 2


def test_multiprocess_reverse_city_order_uses_canonical_locks_without_deadlock(
    process_db,
):
    db_path, locks_path = process_db
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    city_orders = (
        (("CityA", "92001"), ("CityB", "92002")),
        (("CityB", "92002"), ("CityA", "92001")),
    )
    processes = [
        context.Process(
            target=_reverse_order_worker,
            args=(
                db_path,
                locks_path,
                start,
                queue,
                f"auth-order-{index}",
                f"card-order-{index}",
                f"Разный root {index}",
                city_orders[index],
            ),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=30)[1] for _process in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
        assert not process.is_alive()

    assert results == ["reserved", "reserved"]


def test_multiprocess_audit_rows_are_not_lost(process_db):
    db_path, _locks_path = process_db
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    processes = [
        context.Process(
            target=_audit_worker,
            args=(db_path, start, queue, index),
        )
        for index in range(12)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=30)[1] for _process in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert results == ["appended"] * 12
    connection = repository._get_connection()
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM launch_check_audit WHERE event_id LIKE ?",
            ("multiprocess-audit-%",),
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 12


def test_multiprocess_exact_claim_has_one_winner_and_one_audit(process_db):
    db_path, _locks_path = process_db
    plan = _plan(
        "auth-claim-process",
        "card-claim-process",
        "Claim process",
        (("CityA", "93001"),),
    )
    _reserve(plan)
    ad_name = plan.expected_names_by_city["CityA"][0]
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    queue = context.Queue()
    processes = [
        context.Process(
            target=_claim_worker,
            args=(
                db_path,
                start,
                queue,
                plan.authorization.auth_id,
                "CityA",
                "93001",
                ad_name,
            ),
        )
        for _index in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=30)[1] for _process in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert sorted(results) == ["CREATE_RECONCILE_REQUIRED", "claimed"]
    connection = repository._get_connection()
    try:
        claim_audits = connection.execute(
            """
            SELECT COUNT(*) FROM launch_check_audit
            WHERE auth_id = ? AND event_type = 'CREATE_CLAIMED'
            """,
            (plan.authorization.auth_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert claim_audits == 1
