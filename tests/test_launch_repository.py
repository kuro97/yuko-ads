"""Изолированные SQLite-тесты launch authorization repository (migration 019)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from services import creative_intelligence as ci
from services import launch_repository as repository


NOW = datetime(2026, 7, 22, 6, 0, tzinfo=timezone.utc)
MEDIA_SHA = "a" * 64


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Каждый тест использует временную БД и не касается production data."""
    ci.DB_PATH = None
    db_path = str(tmp_path / "launch-checker.db")
    ci.init_kb(db_path)
    yield db_path
    ci.DB_PATH = None


def _proof(auth_id: str, secret: str = "provider-secret") -> SimpleNamespace:
    return SimpleNamespace(auth_id=auth_id, secret=secret)


def _plan(
    auth_id: str,
    *,
    card_id: str = "card-1",
    card_name: str = "Свежая карточка",
    cities: tuple[tuple[str, str], ...] = (
        ("CityA", "1001"),
        ("CityB", "1002"),
    ),
    names_by_city: dict[str, tuple[str, ...]] | None = None,
) -> SimpleNamespace:
    if names_by_city is None:
        names_by_city = {
            city: (f"{city} | {card_name} [PRODB]",) for city, _adset_id in cities
        }
    targets = tuple(
        SimpleNamespace(
            city=city,
            ordinal=ordinal,
            account_kind="offline",
            account_id="act-1",
            adset_id=adset_id,
            reserved_slots=len(names_by_city[city]),
        )
        for ordinal, (city, adset_id) in enumerate(cities)
    )
    return SimpleNamespace(
        check_id=f"check-{auth_id}",
        card_id=card_id,
        card_name=card_name,
        request=SimpleNamespace(
            source="MANUAL",
            campaign_type="L1",
            actor="api-key:sha256:actor",
            override_topic_veto=False,
            override_reason=None,
        ),
        media_sha256=MEDIA_SHA,
        targets=targets,
        expected_names_by_city=names_by_city,
        authorization=_proof(auth_id),
    )


def _reserve(plan: SimpleNamespace, now: datetime = NOW) -> None:
    repository.reserve_authorization(
        plan,
        hashlib.sha256(plan.authorization.secret.encode()).hexdigest(),
        now,
    )


def _scope(
    plan: SimpleNamespace,
    city: str = "CityA",
    *,
    media_sha256: str = MEDIA_SHA,
    ad_name: str | None = None,
) -> SimpleNamespace:
    target = next(target for target in plan.targets if target.city == city)
    return SimpleNamespace(
        account_kind=target.account_kind,
        account_id=target.account_id,
        city=city,
        adset_id=target.adset_id,
        ad_name=ad_name or plan.expected_names_by_city[city][0],
        media_sha256=media_sha256,
    )


def _scalar(db_path: str, sql: str, params: tuple = ()) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    finally:
        conn.close()


def test_migration_creates_all_tables_indexes_and_is_idempotent(isolated_db):
    conn = sqlite3.connect(isolated_db)
    try:
        ci._apply_launch_checker_migration(conn)
        ci._apply_launch_checker_migration(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    finally:
        conn.close()

    assert {
        "launch_authorizations",
        "launch_authorization_targets",
        "launch_authorization_ads",
        "launch_check_audit",
    } <= tables
    assert {
        "uq_launch_auth_open_card_campaign",
        "uq_launch_target_open_identity",
        "uq_launch_ads_open_exact_name",
        "idx_launch_target_reservations",
        "idx_launch_audit_card_time",
        "idx_launch_audit_auth",
    } <= indexes


@pytest.mark.parametrize("tag", ("[PRODB]", "[prodb]", "［ＰｒＯｄＢ］"))
def test_normalize_launch_name_ignores_product_tag_case_and_width(tag):
    assert repository.normalize_launch_name(f"  CityA\t|  Карточка  {tag}  ") == (
        "citya | карточка"
    )


def test_reserve_is_atomic_and_stores_only_secret_hash(isolated_db):
    plan = _plan("auth-1")
    _reserve(plan)

    conn = sqlite3.connect(isolated_db)
    conn.row_factory = sqlite3.Row
    try:
        auth = conn.execute(
            "SELECT * FROM launch_authorizations WHERE auth_id = 'auth-1'"
        ).fetchone()
        targets = conn.execute(
            "SELECT * FROM launch_authorization_targets WHERE auth_id = 'auth-1'"
        ).fetchall()
        ads = conn.execute(
            "SELECT * FROM launch_authorization_ads WHERE auth_id = 'auth-1'"
        ).fetchall()
    finally:
        conn.close()

    assert auth["phase"] == "RESERVED"
    assert auth["secret_sha256"] == hashlib.sha256(b"provider-secret").hexdigest()
    assert "provider-secret" not in json.dumps(dict(auth))
    assert len(targets) == 2
    assert len(ads) == 2
    assert {row["phase"] for row in targets + ads} == {"RESERVED"}


def test_identity_collision_is_typed_and_rolls_back_all_second_targets(isolated_db):
    first = _plan("auth-first", card_id="card-a", card_name="Карточка   [PRODB]")
    _reserve(first)
    second = _plan(
        "auth-second",
        card_id="card-b",
        card_name="Ｋарточка",
        cities=(("CityC", "2001"), ("CityA", "1001")),
        names_by_city={
            "CityC": ("CityC | Другое [PRODB]",),
            "CityA": ("CityA | Ｋарточка [PRODA]",),
        },
    )

    # NFKC применяется; используем тот же видимый root во втором target.
    second.card_name = "Карточка"
    with pytest.raises(repository.LaunchRepositoryBlocked) as captured:
        _reserve(second)

    assert captured.value.code == "DUPLICATE_RESERVED"
    assert captured.value.check_id == "check-auth-second"
    assert _scalar(
        isolated_db,
        "SELECT COUNT(*) FROM launch_authorizations WHERE auth_id = ?",
        ("auth-second",),
    ) == 0
    assert _scalar(
        isolated_db,
        "SELECT COUNT(*) FROM launch_authorization_targets WHERE auth_id = ?",
        ("auth-second",),
    ) == 0


def test_sibling_launch_ad_ids_are_only_same_launch_verified(isolated_db):
    """Соседние claim'ы одного запуска (gateway-check-<база>:<N>) — не дубль карточки."""
    conn = sqlite3.connect(isolated_db)
    now = NOW.isoformat()

    def audit(check_id, auth_id, n):
        conn.execute(
            "INSERT INTO launch_check_audit (event_id, check_id, auth_id, event_type, source, card_id, actor, "
            "reason_codes_json, evidence_json, created_at) VALUES (?, ?, ?, 'RESERVED', 'MANUAL', 'card-1', 'a', '[]', '{}', ?)",
            (f"e-{n}", check_id, auth_id, now),
        )

    def binding(claim, auth_id, ad_id, phase="VERIFIED"):
        conn.execute(
            "INSERT INTO launch_provider_ad_bindings (claim_id, auth_id, ad_name, adset_id, expected_fingerprint, "
            "expected_payload_json, phase, ad_id, created_at, updated_at) VALUES (?, ?, 'n', '1001', ?, '{}', ?, ?, ?, ?)",
            (claim, auth_id, "a" * 64, phase, ad_id, now, now),
        )

    audit("gateway-check-base1:0", "auth-0", 1)
    audit("gateway-check-base1:1", "auth-1", 2)
    audit("gateway-check-base1:2", "auth-2", 3)
    audit("gateway-check-base10:0", "auth-other", 4)  # другой запуск с похожим префиксом
    binding("c0", "auth-0", "ad-0")
    binding("c1", "auth-1", "ad-1", phase="CLAIMED")  # не подтверждён — не сосед
    binding("c9", "auth-other", "ad-9")
    conn.commit()
    conn.close()

    assert repository.sibling_launch_ad_ids("auth-2") == frozenset({"ad-0"})
    assert repository.sibling_launch_ad_ids("auth-unknown") == frozenset()


def test_concurrent_same_identity_has_exactly_one_winner(isolated_db):
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def reserve(auth_id: str, card_id: str) -> None:
        try:
            plan = _plan(auth_id, card_id=card_id)
            barrier.wait(timeout=5)
            _reserve(plan)
            outcomes.append("reserved")
        except repository.LaunchRepositoryBlocked as exc:
            outcomes.append(exc.code)

    threads = [
        threading.Thread(target=reserve, args=("auth-race-a", "card-race-a")),
        threading.Thread(target=reserve, args=("auth-race-b", "card-race-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert sorted(outcomes) == ["DUPLICATE_RESERVED", "reserved"]
    assert _scalar(isolated_db, "SELECT COUNT(*) FROM launch_authorizations") == 1
    assert _scalar(isolated_db, "SELECT COUNT(*) FROM launch_authorization_targets") == 2


def test_expired_unclaimed_reservation_releases_unique_keys(isolated_db):
    first = _plan("auth-expired", card_id="old-card")
    _reserve(first)

    result = repository.expire_stale_reservations(NOW + timedelta(minutes=31))

    assert result.released_auth_ids == ("auth-expired",)
    snapshot = repository.reconcile_authorization(
        "auth-expired", NOW + timedelta(minutes=31)
    )
    assert snapshot.phase == "RELEASED"
    assert snapshot.released_ads == snapshot.total_ads == 2

    replacement = _plan("auth-replacement", card_id="new-card")
    _reserve(replacement, NOW + timedelta(minutes=31))
    assert repository.reconcile_authorization(
        "auth-replacement", NOW + timedelta(minutes=31)
    ).phase == "RESERVED"


def test_expired_create_claim_becomes_blocked_reconcile(isolated_db):
    plan = _plan("auth-claimed")
    _reserve(plan)
    repository.claim_provider_create(
        plan.authorization,
        _scope(plan),
        NOW + timedelta(minutes=1),
    )

    result = repository.expire_stale_reservations(NOW + timedelta(minutes=31))
    snapshot = repository.reconcile_authorization(
        "auth-claimed", NOW + timedelta(minutes=31)
    )

    assert result.blocked_reconcile_auth_ids == ("auth-claimed",)
    assert snapshot.phase == "BLOCKED_RECONCILE"
    assert snapshot.needs_reconcile is True
    assert snapshot.blocked_reconcile_ads >= 1
    assert repository.get_reserved_slots("1001", NOW + timedelta(days=30)) == 1

    duplicate = _plan("auth-blocked-duplicate", card_id="other-card")
    with pytest.raises(repository.LaunchRepositoryBlocked) as captured:
        _reserve(duplicate, NOW + timedelta(days=30))
    assert captured.value.code == "DUPLICATE_RESERVED"


@pytest.mark.parametrize(
    ("scope_change", "expected_code"),
    [
        ({"media_sha256": "b" * 64}, "MEDIA_DRIFT"),
        ({"ad_name": "CityA | Чужое имя [PRODB]"}, "PROVIDER_SCOPE_DRIFT"),
        ({"adset_id": "wrong-adset"}, "PROVIDER_SCOPE_DRIFT"),
    ],
)
def test_provider_claim_rejects_drift_without_consuming_name(
    isolated_db, scope_change, expected_code
):
    plan = _plan("auth-drift")
    _reserve(plan)
    scope = _scope(plan)
    for key, value in scope_change.items():
        setattr(scope, key, value)

    with pytest.raises(repository.LaunchRepositoryBlocked) as captured:
        repository.claim_provider_create(plan.authorization, scope, NOW)

    assert captured.value.code == expected_code
    assert repository.reconcile_authorization("auth-drift", NOW).reserved_ads == 2


def test_read_only_media_validation_checks_secret_ttl_and_returns_metadata(isolated_db):
    plan = _plan("auth-validate")
    _reserve(plan)

    validated = repository.validate_authorization_media(
        plan.authorization, MEDIA_SHA, NOW
    )
    assert validated.auth_id == "auth-validate"
    assert validated.card_id == "card-1"
    assert validated.phase == "RESERVED"
    assert validated.plan_sha256 != validated.media_sha256
    assert tuple(target.city for target in validated.targets) == (
        "CityA",
        "CityB",
    )
    assert validated.targets[0].expected_names == (
        "CityA | Свежая карточка [PRODB]",
    )
    assert validated.targets[0].expected_name_keys == (
        "citya | свежая карточка",
    )

    def metadata_allows(scope: SimpleNamespace) -> bool:
        return any(
            target.city == scope.city
            and target.account_kind == scope.account_kind
            and target.account_id == scope.account_id
            and target.adset_id == scope.adset_id
            and repository.normalize_launch_name(scope.ad_name)
            in target.expected_name_keys
            for target in validated.targets
        )

    valid_scope = _scope(plan)
    assert metadata_allows(valid_scope) is True
    for field_name, wrong_value in (
        ("account_id", "wrong-account"),
        ("adset_id", "wrong-adset"),
        ("ad_name", "CityA | Чужое имя [PRODB]"),
    ):
        wrong_scope = _scope(plan)
        setattr(wrong_scope, field_name, wrong_value)
        assert metadata_allows(wrong_scope) is False
    assert _scalar(
        isolated_db,
        "SELECT COUNT(*) FROM launch_authorization_ads WHERE claim_id IS NOT NULL",
    ) == 0

    with pytest.raises(repository.LaunchRepositoryBlocked) as forged:
        repository.validate_authorization_media(
            _proof("auth-validate", "forged"), MEDIA_SHA, NOW
        )
    assert forged.value.code == "INVALID_AUTHORIZATION"

    with pytest.raises(repository.LaunchRepositoryBlocked) as expired:
        repository.validate_authorization_media(
            plan.authorization, MEDIA_SHA, NOW + timedelta(minutes=31)
        )
    assert expired.value.code == "AUTHORIZATION_EXPIRED"
    assert repository.reconcile_authorization("auth-validate", NOW).phase == "RESERVED"


def test_claim_and_confirm_persist_partial_then_complete(isolated_db):
    plan = _plan("auth-success")
    _reserve(plan)

    claim_a = repository.claim_provider_create(plan.authorization, _scope(plan), NOW)
    repository.record_provider_create_success(claim_a, "ad-1", NOW)
    partial = repository.reconcile_authorization("auth-success", NOW)
    assert partial.phase == "PARTIAL"
    assert partial.created_ad_ids == ("ad-1",)

    repository.renew_authorization("auth-success", timedelta(minutes=30), NOW)
    claim_b = repository.claim_provider_create(
        plan.authorization, _scope(plan, "CityB"), NOW
    )
    repository.record_provider_create_success(claim_b, "ad-2", NOW)
    complete = repository.reconcile_authorization("auth-success", NOW)
    assert complete.phase == "COMPLETED"
    assert complete.created_ads == complete.total_ads == 2
    assert complete.created_ad_ids == ("ad-1", "ad-2")


def test_authorization_has_create_claims_only_after_first_claim(isolated_db):
    """Резервация — не claim; первый claim_provider_create включает признак."""
    plan = _plan("auth-claims")
    _reserve(plan)
    assert repository.authorization_has_create_claims("auth-claims") is False

    repository.claim_provider_create(plan.authorization, _scope(plan), NOW)
    assert repository.authorization_has_create_claims("auth-claims") is True
    # Чужая авторизация не видит чужих claim.
    assert repository.authorization_has_create_claims("auth-other") is False


def test_target_renewal_checks_secret_scope_and_supports_create_started(isolated_db):
    plan = _plan("auth-renew-target")
    _reserve(plan)
    repository.claim_provider_create(plan.authorization, _scope(plan), NOW)

    renewed = repository.renew_authorization_target(
        plan.authorization,
        "CityA",
        "1001",
        timedelta(minutes=30),
        NOW + timedelta(minutes=5),
    )

    assert renewed.authorization_phase == "CREATE_STARTED"
    assert renewed.target_phase == "CREATE_STARTED"
    assert renewed.expires_at == NOW + timedelta(minutes=35)
    assert repository.get_reserved_slots(
        "1001",
        NOW + timedelta(minutes=6),
        exclude_auth_id="auth-renew-target",
    ) == 0
    assert repository.get_reserved_slots("1001", NOW + timedelta(minutes=6)) == 1

    with pytest.raises(repository.LaunchRepositoryBlocked) as forged:
        repository.renew_authorization_target(
            _proof("auth-renew-target", "forged"),
            "CityA",
            "1001",
            timedelta(minutes=30),
            NOW + timedelta(minutes=6),
        )
    assert forged.value.code == "INVALID_AUTHORIZATION"

    with pytest.raises(repository.LaunchRepositoryBlocked) as drift:
        repository.renew_authorization_target(
            plan.authorization,
            "CityA",
            "wrong-adset",
            timedelta(minutes=30),
            NOW + timedelta(minutes=6),
        )
    assert drift.value.code == "PROVIDER_SCOPE_DRIFT"


def test_expired_target_renewal_persists_release_before_typed_deny(isolated_db):
    expired = _plan("auth-expired-renew", card_id="expired-card")
    _reserve(expired)

    with pytest.raises(repository.LaunchRepositoryBlocked) as captured:
        repository.renew_authorization_target(
            expired.authorization,
            "CityA",
            "1001",
            timedelta(minutes=30),
            NOW + timedelta(minutes=31),
        )

    assert captured.value.code == "AUTHORIZATION_NOT_RENEWABLE"
    assert repository.reconcile_authorization(
        expired.authorization.auth_id,
        NOW + timedelta(minutes=31),
    ).phase == "RELEASED"
    replacement = _plan("auth-after-expired-renew", card_id="replacement-card")
    _reserve(replacement, NOW + timedelta(minutes=31))
    assert repository.reconcile_authorization(
        replacement.authorization.auth_id,
        NOW + timedelta(minutes=31),
    ).phase == "RESERVED"


def test_trusted_live_reconciliation_rotates_only_unclaimed_missing_names(isolated_db):
    names = (
        "CityA | Свежая карточка / 1 [PRODB]",
        "CityA | Свежая карточка / 2 [PRODB]",
    )
    plan = _plan(
        "auth-live-rotate",
        cities=(("CityA", "1001"),),
        names_by_city={"CityA": names},
    )
    _reserve(plan)
    repository.claim_provider_create(
        plan.authorization,
        _scope(plan, ad_name=names[0]),
        NOW,
    )
    repository.expire_stale_reservations(NOW + timedelta(minutes=31))

    result = repository.reconcile_and_rotate_authorization(
        plan.authorization,
        (
            repository.TrustedLiveTarget(
                city="CityA",
                account_id="act-1",
                adset_id="1001",
                inventory_complete=True,
                exact_matches={names[0]: ("ad-live-1",), names[1]: ()},
            ),
        ),
        NOW + timedelta(minutes=31),
    )

    assert result.phase == "PARTIAL"
    assert result.authorization is not None
    assert result.authorization.auth_id == plan.authorization.auth_id
    assert result.created_ad_ids == ("ad-live-1",)
    assert result.missing_names_by_city == {"CityA": (names[1],)}
    assert repository.get_reserved_slots("1001", NOW + timedelta(minutes=32)) == 1
    validated = repository.validate_authorization_media(
        result.authorization,
        MEDIA_SHA,
        NOW + timedelta(minutes=32),
    )
    assert validated.phase == "PARTIAL"

    with pytest.raises(repository.LaunchRepositoryBlocked) as old_proof:
        repository.validate_authorization_media(
            plan.authorization,
            MEDIA_SHA,
            NOW + timedelta(minutes=32),
        )
    assert old_proof.value.code == "INVALID_AUTHORIZATION"

    conn = sqlite3.connect(isolated_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT ad_name, phase, claim_id, created_ad_id
            FROM launch_authorization_ads
            WHERE auth_id = ? ORDER BY ordinal
            """,
            (plan.authorization.auth_id,),
        ).fetchall()
        auth = conn.execute(
            "SELECT secret_sha256 FROM launch_authorizations WHERE auth_id = ?",
            (plan.authorization.auth_id,),
        ).fetchone()
        audit = conn.execute(
            "SELECT event_type, evidence_json FROM launch_check_audit WHERE auth_id = ?",
            (plan.authorization.auth_id,),
        ).fetchall()
    finally:
        conn.close()
    assert tuple(row["phase"] for row in rows) == ("CREATED", "RESERVED")
    assert rows[0]["created_ad_id"] == "ad-live-1"
    assert rows[1]["claim_id"] is None
    assert auth["secret_sha256"] == hashlib.sha256(
        result.authorization.secret.encode()
    ).hexdigest()
    assert result.authorization.secret not in json.dumps(
        [dict(row) for row in audit], ensure_ascii=False
    )
    assert [row["event_type"] for row in audit][-2:] == [
        "CREATE_CONFIRMED",
        "RESERVED",
    ]


def test_trusted_live_reconciliation_never_rotates_ambiguous_claim(isolated_db):
    names = (
        "CityA | Свежая карточка / 1 [PRODB]",
        "CityA | Свежая карточка / 2 [PRODB]",
    )
    plan = _plan(
        "auth-live-ambiguous",
        cities=(("CityA", "1001"),),
        names_by_city={"CityA": names},
    )
    _reserve(plan)
    repository.claim_provider_create(
        plan.authorization,
        _scope(plan, ad_name=names[0]),
        NOW,
    )
    repository.expire_stale_reservations(NOW + timedelta(minutes=31))

    with pytest.raises(repository.LaunchRepositoryBlocked) as captured:
        repository.reconcile_and_rotate_authorization(
            plan.authorization,
            (
                repository.TrustedLiveTarget(
                    city="CityA",
                    account_id="act-1",
                    adset_id="1001",
                    inventory_complete=True,
                    exact_matches={names[0]: (), names[1]: ()},
                ),
            ),
            NOW + timedelta(minutes=31),
        )

    assert captured.value.code == "LIVE_RECONCILIATION_AMBIGUOUS"
    snapshot = repository.reconcile_authorization(
        plan.authorization.auth_id,
        NOW + timedelta(days=10),
    )
    assert snapshot.phase == "BLOCKED_RECONCILE"
    assert repository.get_reserved_slots("1001", NOW + timedelta(days=10)) == 2


def test_trusted_live_reconciliation_completes_without_rotating_proof(isolated_db):
    plan = _plan(
        "auth-live-complete",
        cities=(("CityA", "1001"),),
    )
    name = plan.expected_names_by_city["CityA"][0]
    _reserve(plan)
    repository.claim_provider_create(plan.authorization, _scope(plan), NOW)
    repository.expire_stale_reservations(NOW + timedelta(minutes=31))

    result = repository.reconcile_and_rotate_authorization(
        plan.authorization,
        (
            repository.TrustedLiveTarget(
                city="CityA",
                account_id="act-1",
                adset_id="1001",
                inventory_complete=True,
                exact_matches={name: ("ad-live-complete",)},
            ),
        ),
        NOW + timedelta(minutes=31),
    )

    assert result.phase == "COMPLETED"
    assert result.authorization is None
    assert result.created_ad_ids == ("ad-live-complete",)
    assert repository.get_reserved_slots("1001", NOW + timedelta(days=10)) == 0


def test_append_audit_is_concurrent_and_sanitized(isolated_db):
    barrier = threading.Barrier(20)
    errors: list[BaseException] = []

    def append(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            repository.append_launch_audit(
                repository.LaunchAuditEvent(
                    event_id=f"event-{index}",
                    check_id=f"check-{index}",
                    event_type="CANDIDATE",
                    source="CRON",
                    card_id=f"card-{index}",
                    actor="system",
                    evidence={
                        "secret": "do-not-store",
                        "url": "https://example.test/file?access_token=abc",
                    },
                    created_at=NOW,
                )
            )
        except BaseException as exc:  # pragma: no cover - диагностика threads
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == []
    conn = sqlite3.connect(isolated_db)
    try:
        rows = conn.execute(
            "SELECT evidence_json FROM launch_check_audit ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 20
    combined = " ".join(row[0] for row in rows)
    assert "do-not-store" not in combined
    assert "access_token=abc" not in combined
    assert "[REDACTED]" in combined
    assert "?<redacted>" in combined


def test_manual_release_does_not_keep_open_name_index(isolated_db):
    first = _plan("auth-release", card_id="card-release")
    _reserve(first)
    repository.finish_authorization("auth-release", "RELEASED", NOW)

    second = _plan("auth-after-release", card_id="card-other")
    _reserve(second)

    assert repository.reconcile_authorization("auth-release", NOW).phase == "RELEASED"
    assert repository.reconcile_authorization("auth-after-release", NOW).phase == "RESERVED"


@pytest.mark.parametrize(
    ("phase", "expected_slots"),
    [
        ("RESERVED", 1),
        ("CREATE_STARTED", 1),
        ("PARTIAL", 1),
        ("BLOCKED_RECONCILE", 1),
        ("COMPLETED", 0),
        ("RELEASED", 0),
    ],
)
def test_get_reserved_slots_counts_only_open_statuses_with_safe_ttl_semantics(
    isolated_db, phase, expected_slots
):
    plan = _plan("auth-slot-status")
    _reserve(plan)
    conn = sqlite3.connect(isolated_db)
    try:
        conn.execute(
            """
            UPDATE launch_authorization_targets SET phase = ?
            WHERE auth_id = ? AND adset_id = ?
            """,
            (phase, "auth-slot-status", "1001"),
        )
        conn.commit()
    finally:
        conn.close()

    assert repository.get_reserved_slots("1001", NOW) == expected_slots
    assert repository.get_reserved_slots("unknown-adset", NOW) == 0
    expected_after_ttl = 1 if phase == "BLOCKED_RECONCILE" else 0
    assert (
        repository.get_reserved_slots("1001", NOW + timedelta(minutes=31))
        == expected_after_ttl
    )


def test_get_reserved_slots_db_error_is_typed(isolated_db, tmp_path):
    del isolated_db
    ci.DB_PATH = str(tmp_path)

    with pytest.raises(repository.LaunchRepositoryBlocked) as captured:
        repository.get_reserved_slots("1001", NOW)

    assert captured.value.code == "RESERVATION_LOOKUP_FAILED"


RECOVERY_APPROVED_SHA = "b" * 64
RECOVERY_MEDIA_SHA = "c" * 64
RECOVERY_NAMES = (
    "CityA | Recovery / one [PRODB]",
    "CityA | Recovery / two [PRODB]",
)


def _insert_trusted_recovery_plan(
    db_path: str,
    *,
    plan_phase: str = "LAUNCHING",
    case_phase: str = "LAUNCHING",
    media_binding: str | None = RECOVERY_MEDIA_SHA,
    plan_error: str | None = None,
) -> None:
    created_at = (NOW - timedelta(days=1)).isoformat()
    approved_at = (NOW - timedelta(hours=1)).isoformat()
    evidence = {
        "classification": "MISSING",
        "inventory_complete": True,
    }
    if media_binding is not None:
        evidence["launch_media_manifest_sha256"] = media_binding
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO launch_recovery_cases (
                case_id, tenant_id, trello_action_id, card_id, card_name,
                source_completed_at, scan_since, campaign_type, account_kind,
                account_id, phase, target_cities_json, missing_cities_json,
                expected_names_json, media_manifest_sha256, approved_by,
                approved_at, created_at, updated_at
            ) VALUES (
                'case-trusted', 'default', 'action-trusted', 'card-trusted',
                'Recovery', ?, ?, 'leadgen', 'offline', 'act-recovery', ?,
                '["CityA"]', '["CityA"]', ?, ?, 'operator', ?, ?, ?
            )
            """,
            (
                (NOW - timedelta(days=2)).isoformat(),
                (NOW - timedelta(days=3)).isoformat(),
                case_phase,
                json.dumps(
                    {"CityA": list(RECOVERY_NAMES)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                RECOVERY_APPROVED_SHA,
                approved_at,
                created_at,
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO launch_recovery_city_plans (
                plan_id, case_id, city, account_kind, account_id, adset_id,
                expected_ad_names_json, expected_ad_count, reconcile_from,
                reconcile_until, phase, found_ad_ids_json,
                media_manifest_sha256, launch_attempt_key, capacity_available,
                evidence_json, approved_by, approved_at, last_rechecked_at,
                last_error, created_at, updated_at
            ) VALUES (
                'plan-trusted', 'case-trusted', 'CityA', 'offline',
                'act-recovery', 'adset-recovery', ?, 2, ?, ?, ?, '[]', ?,
                'attempt-trusted', 10, ?, 'operator', ?, ?, ?, ?, ?
            )
            """,
            (
                json.dumps(
                    list(RECOVERY_NAMES),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                (NOW - timedelta(days=2)).isoformat(),
                NOW.isoformat(),
                plan_phase,
                RECOVERY_APPROVED_SHA,
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                approved_at,
                NOW.isoformat(),
                plan_error,
                created_at,
                created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _issue_trusted_recovery(**overrides):
    values = {
        "plan_id": "plan-trusted",
        "approved_manifest_sha256": RECOVERY_APPROVED_SHA,
        "launch_attempt_key": "attempt-trusted",
        "card_id": "card-trusted",
        "card_name": "Recovery",
        "campaign_type": "leadgen",
        "account_kind": "offline",
        "account_id": "act-recovery",
        "city": "CityA",
        "adset_id": "adset-recovery",
        "expected_ad_names": RECOVERY_NAMES,
        "expected_ad_count": 2,
        "media_sha256": RECOVERY_MEDIA_SHA,
        "actor": "operator:sha256",
        "now": NOW,
    }
    values.update(overrides)
    return repository.reserve_trusted_recovery_authorization(**values)


def test_trusted_recovery_issuer_is_atomic_and_returns_ordinary_proof(isolated_db):
    _insert_trusted_recovery_plan(isolated_db)

    proof = _issue_trusted_recovery()
    validation = repository.validate_authorization_media(
        proof, RECOVERY_MEDIA_SHA, NOW
    )

    assert validation.source == "RECOVERY"
    assert validation.card_id == "card-trusted"
    assert validation.targets[0].expected_names == RECOVERY_NAMES
    assert validation.targets[0].adset_id == "adset-recovery"
    assert repository.get_reserved_slots("adset-recovery", NOW) == 2
    conn = sqlite3.connect(isolated_db)
    conn.row_factory = sqlite3.Row
    try:
        auth = conn.execute(
            "SELECT * FROM launch_authorizations WHERE auth_id = ?",
            (proof.auth_id,),
        ).fetchone()
        counts = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM launch_authorization_targets
                 WHERE auth_id = ?) AS targets,
                (SELECT COUNT(*) FROM launch_authorization_ads
                 WHERE auth_id = ?) AS ads,
                (SELECT COUNT(*) FROM launch_check_audit
                 WHERE auth_id = ?) AS audit
            """,
            (proof.auth_id, proof.auth_id, proof.auth_id),
        ).fetchone()
    finally:
        conn.close()
    assert auth["recovery_plan_id"] == "plan-trusted"
    assert auth["secret_sha256"] == hashlib.sha256(proof.secret.encode()).hexdigest()
    assert proof.secret not in json.dumps(dict(auth), ensure_ascii=False)
    assert tuple(counts) == (1, 2, 1)


@pytest.mark.parametrize(
    ("insert_overrides", "issue_overrides", "expected_code"),
    [
        ({"media_binding": None}, {}, "RECOVERY_LEGACY_MEDIA_BINDING"),
        ({"plan_phase": "APPROVED"}, {}, "RECOVERY_PLAN_NOT_LAUNCHING"),
        ({"plan_error": "provider uncertain"}, {}, "RECOVERY_PLAN_BLOCKED"),
        ({}, {"account_id": "wrong-account"}, "RECOVERY_PLAN_DRIFT"),
        ({}, {"launch_attempt_key": "wrong-attempt"}, "RECOVERY_PLAN_DRIFT"),
        ({}, {"media_sha256": "d" * 64}, "RECOVERY_MEDIA_DRIFT"),
    ],
)
def test_trusted_recovery_issuer_fails_closed_without_partial_rows(
    isolated_db, insert_overrides, issue_overrides, expected_code
):
    _insert_trusted_recovery_plan(isolated_db, **insert_overrides)

    with pytest.raises(repository.LaunchRepositoryBlocked) as captured:
        _issue_trusted_recovery(**issue_overrides)

    assert captured.value.code == expected_code
    assert _scalar(isolated_db, "SELECT COUNT(*) FROM launch_authorizations") == 0
    assert _scalar(isolated_db, "SELECT COUNT(*) FROM launch_authorization_targets") == 0
    assert _scalar(isolated_db, "SELECT COUNT(*) FROM launch_authorization_ads") == 0


def test_orphaned_upload_claim_reopens_after_ttl(tmp_path, monkeypatch):
    """Осиротевшая заливка (SIGKILL посреди upload) переоткрывается после TTL.

    bug9 п.1: auth_id детерминирован от manifest_id, и вечный
    UPLOAD_STARTED хоронил карточку навсегда.
    """
    from datetime import datetime, timedelta, timezone

    from services import launch_repository as repo

    db = tmp_path / "kb.db"
    monkeypatch.setattr(repo, "_DB_PATH_OVERRIDE", str(db), raising=False)
    import services.creative_intelligence as ci
    ci.init_kb(str(db))

    t0 = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
    # Первый клейм: записи нет → создаётся UPLOAD_STARTED, идём заливать.
    assert repo.claim_provider_asset_upload("auth-1", "asset-1", "a" * 64, t0) is None

    # Свежий повтор (заливка, возможно, идёт) — по-прежнему запрещён.
    with pytest.raises(repo.LaunchRepositoryBlocked):
        repo.claim_provider_asset_upload(
            "auth-1", "asset-1", "a" * 64, t0 + timedelta(minutes=5)
        )

    # Спустя TTL сирота переоткрывается: снова None = разрешена повторная заливка.
    assert (
        repo.claim_provider_asset_upload(
            "auth-1", "asset-1", "a" * 64, t0 + timedelta(hours=2)
        )
        is None
    )

    # Хеш-дрейф ловится и на сироте.
    with pytest.raises(repo.LaunchRepositoryBlocked):
        repo.claim_provider_asset_upload(
            "auth-1", "asset-1", "b" * 64, t0 + timedelta(hours=3)
        )
