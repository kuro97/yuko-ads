"""План «запуск: город независим», волны 0 и I — сбои ДО провайдера.

Воспроизведённый сбой: у LAUNCH-заданий второй claim той же карточки падал ДО обращения к FB, гейтвей давал
PROVIDER_OUTCOME_UNKNOWN, задание уходило в RECONCILE_REQUIRED навсегда, а текст исключения жил только в
journald (40 часов). Волна 0: отпечаток исключения едет в reason_code. Волна I (п. A): исключение до
провайдера — это FAILED без эффекта (`remote_may_have_changed=False`), следующий claim карточки исполняется
своим чередом; попытка auto_launch получает ошибку города и снятый CREATE-маркер.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from services import action_adapter_launch
from services.action_gateway_core import ActionResult, unknown_outcome_reason
from services.approval_checker_models import (
    ActionKind,
    ActionOrigin,
    CreativeSpec,
    LaunchDestination,
    LaunchManifest,
    MediaAssetSpec,
    MediaType,
    PlacementRole,
    TrelloPrecondition,
)
from services.launch_checker import LaunchCheckBlocked
from services.launch_repository import LaunchRepositoryBlocked

NOW = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)


def _manifest(tmp_path) -> LaunchManifest:
    sha = "a" * 64
    media = MediaAssetSpec(
        asset_id="asset-1", order_index=0, media_type=MediaType.VIDEO, placement_group_id=None,
        placement_role=PlacementRole.DEFAULT, staged_relative_path="media/video.mp4",
        original_attachment_id="attachment-1", mime_type="video/mp4", size_bytes=10, content_sha256=sha,
    )
    creative = CreativeSpec(
        creative_id="creative-1", order_index=0, ad_name="CityB | Тест / карточка [PRODA]",
        media_asset_ids=(media.asset_id,), body_staged_relative_path="text/body.txt", body_sha256="b" * 64,
        product="PRODA", page_id="page-1", lead_form_id="form-1", call_to_action="LEARN_MORE",
        instagram_actor_id="instagram-1", title="title", link_url=None, expected_configured_status="ACTIVE",
    )
    destination = LaunchDestination(
        city="CityB", account_id="account-1", adset_id="adset-1", adset_type="L1",
        current_daily_budget=Decimal("10"), currency="USD", capacity_available=10, hard_reserve_slots=1,
        creatives=(creative,), duplicate_signature="c" * 64,
    )
    trello = TrelloPrecondition(
        card_id="card-1", board_id="board-1", ready_list_id="ready-1", expected_list_id="ready-1",
        expected_due_complete=False, expected_closed=False, date_last_activity=NOW,
        attachment_ids=("attachment-1",), attachment_manifest_sha256="d" * 64, labels_sha256="e" * 64,
        card_content_sha256="f" * 64,
    )
    manifest_id = "launch-manifest-wave0"
    return LaunchManifest(
        kind=ActionKind.LAUNCH, manifest_id=manifest_id, origin=ActionOrigin.AUTO_LAUNCH,
        idempotency_key=str(uuid.uuid4()), prepared_at=NOW, config_version_sha256=sha,
        staging_root=str(tmp_path), staging_directory=str(tmp_path / manifest_id), trello=trello,
        card_name_sha256=sha, media_manifest_sha256=sha, media_assets=(media,), campaign_type="PRODA",
        destinations=(destination,),
    )


@pytest.fixture
def harness(monkeypatch):
    """Адаптер с заглушённой резервацией и журналом попытки; до провайдера дело не доходит."""

    import services.auto_launch as auto_launch

    calls: dict[str, list] = {"retracted": [], "no_effect": [], "marker": []}
    monkeypatch.setattr(
        action_adapter_launch, "_reserve_manifest_authorization",
        lambda manifest, now: SimpleNamespace(auth_id="launch-auth-gateway-wave0", secret="s"),
    )
    monkeypatch.setattr(
        action_adapter_launch, "_retract_create_marker_without_claims",
        lambda manifest, auth_id: calls["retracted"].append(auth_id),
    )
    monkeypatch.setattr(auto_launch, "mark_gateway_create_started", lambda manifest: calls["marker"].append("x"))
    monkeypatch.setattr(
        auto_launch, "record_gateway_launch_no_effect",
        lambda manifest, reason: calls["no_effect"].append(reason),
    )
    return calls


def _mutate(tmp_path):
    return action_adapter_launch.LaunchActionAdapter().mutate(
        _manifest(tmp_path), NOW, attempt=SimpleNamespace(attempt_id="a1")
    )


def test_marker_refusal_before_provider_is_failed_without_effect(harness, monkeypatch, tmp_path):
    """Кандидат 1: маркер CREATE отвергает фазу попытки (RECONCILING/BLOCKED)."""

    import services.auto_launch as auto_launch

    def _refuse(manifest):
        raise RuntimeError("City CREATE marker требует LAUNCHING attempt")

    monkeypatch.setattr(auto_launch, "mark_gateway_create_started", _refuse)
    result = _mutate(tmp_path)
    assert result.result is ActionResult.FAILED and result.remote_may_have_changed is False
    assert result.reason_code == (
        "LAUNCH_NO_EFFECT_MARKER:RUNTIMEERROR:CITY_CREATE_MARKER_TREBUET_LAUNCHING_ATTEMPT"
    )
    assert harness["no_effect"] == [result.reason_code], "попытка получает ошибку города"
    assert harness["retracted"] == [], "маркер не ставился — снимать нечего"


def test_reservation_collision_before_provider_is_failed_without_effect(harness, monkeypatch, tmp_path):
    """Кандидат 2 (архитектор): липкая резервация карточки → DUPLICATE_RESERVED до FB."""

    def _collide(manifest, now):
        raise LaunchRepositoryBlocked(
            "DUPLICATE_RESERVED", ("Карточка или её имена уже зарезервированы другим запуском",), "check-1"
        )

    monkeypatch.setattr(action_adapter_launch, "_reserve_manifest_authorization", _collide)
    result = _mutate(tmp_path)
    assert result.result is ActionResult.FAILED and result.remote_may_have_changed is False
    assert result.reason_code.startswith("LAUNCH_NO_EFFECT_RESERVE:LAUNCHREPOSITORYBLOCKED:")
    assert harness["marker"] == [], "резервация упала — маркер CREATE не ставится"


def test_executor_failure_without_claims_is_failed_without_effect(harness, monkeypatch, tmp_path):
    """Исполнитель упал после маркера, но claim'ов у авторизации нет → объявлений нет → FAILED."""

    def _boom(manifest, *, attempt, authorization):
        raise RuntimeError("LAUNCH_PREFLIGHT_INVALID")

    monkeypatch.setattr(action_adapter_launch, "_execute_launch_manifest_unchecked", _boom)
    monkeypatch.setattr(action_adapter_launch.launch_repository, "authorization_has_create_claims", lambda auth_id: False)
    result = _mutate(tmp_path)
    assert result.result is ActionResult.FAILED and result.remote_may_have_changed is False
    assert result.reason_code.startswith("LAUNCH_NO_EFFECT_PRE_POST:RUNTIMEERROR:LAUNCH_PREFLIGHT_INVALID")
    assert harness["retracted"] == ["launch-auth-gateway-wave0"]


@pytest.mark.parametrize("has_claims", [True, "db_error"])
def test_executor_failure_with_claims_or_unknown_stays_unknown(harness, monkeypatch, tmp_path, has_claims):
    """Claim у авторизации есть (POST мог уйти) или БД недоступна → прежнее исключение → UNKNOWN в гейтвее."""

    def _boom(manifest, *, attempt, authorization):
        raise TimeoutError("provider timeout")

    def _claims(auth_id):
        if has_claims == "db_error":
            raise OSError("db locked")
        return True

    monkeypatch.setattr(action_adapter_launch, "_execute_launch_manifest_unchecked", _boom)
    monkeypatch.setattr(action_adapter_launch.launch_repository, "authorization_has_create_claims", _claims)
    with pytest.raises(TimeoutError) as info:
        _mutate(tmp_path)
    assert harness["no_effect"] == []
    assert unknown_outcome_reason(info.value) == "PROVIDER_OUTCOME_UNKNOWN:TIMEOUTERROR:PROVIDER_TIMEOUT"


def _post_harness(monkeypatch, outcomes):
    """read_postcondition с заглушками bindings; outcomes — последовательность исходов пост-чтения."""
    from services.approval_checker_models import ActionObservation

    monkeypatch.setattr(
        action_adapter_launch.launch_repository, "get_provider_ad_bindings",
        lambda auth_id: [{"ad_id": "ad-1", "phase": "VERIFIED", "verified_fingerprint": "x",
                          "expected_fingerprint": "x"}],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(action_adapter_launch, "_sleep", lambda seconds: sleeps.append(seconds))
    calls = iter(outcomes)

    def _observe(manifest, created_ids, now):
        if next(calls) == "blocked":
            raise LaunchCheckBlocked("LAUNCH_POSTCONDITION_INCOMPLETE", ("Не все exact created IDs найдены",), "m")
        return ActionObservation(observed_at=NOW, digest="1" * 64, target_state="ACTIVE|ACTIVE",
                                 subject_ids=("ad-1",), unrelated_state_digest="2" * 64)

    monkeypatch.setattr(action_adapter_launch, "_observe_launch_manifest_postcondition", _observe)
    return sleeps


def test_claim_manifest_with_second_creative_of_city_passes_scope_check(tmp_path):
    """Claim «база:1» несёт второй креатив города с order_index=1 — это законно.

    Проверка «индексы подряд с нуля» отвергала всё, кроме первого креатива города
    (INVALID_LAUNCH_MANIFEST «Creative order/capacity»), и карточка запускалась 1 объявлением на город.
    """
    from dataclasses import replace

    from integrations.facebook import _validate_launch_manifest_scope

    base = _manifest(tmp_path)
    destination = base.destinations[0]
    second = replace(destination.creatives[0], order_index=1, creative_id="creative-2")
    claim = replace(base, manifest_id=f"{base.manifest_id}:1", destinations=(replace(destination, creatives=(second,)),))
    _validate_launch_manifest_scope(claim)  # не бросает

    broken_full = replace(base, destinations=(replace(destination, creatives=(second,)),))
    with pytest.raises(LaunchCheckBlocked) as info:
        _validate_launch_manifest_scope(broken_full)  # полный манифест обязан начинаться с 0
    assert info.value.code == "INVALID_LAUNCH_MANIFEST"


def _live_target_check(monkeypatch, *, siblings, inventory_names):
    from integrations import facebook
    from services import launch_repository as repo

    card_root = repo.normalize_launch_name("CityE | Ирина / Тема А / PRODB")
    target = SimpleNamespace(
        account_kind="offline", account_id="1", adset_id="adset-1",
        expected_names=("CityE | Ирина / Тема А / PRODB / Ирина 2 [PRODB]",), identity_key=card_root,
    )
    inventory = [
        {"id": ad_id, "name": name, "adset_id": "adset-1", "effective_status": "ACTIVE"}
        for ad_id, name in inventory_names
    ]
    monkeypatch.setattr(repo, "sibling_launch_ad_ids", lambda auth_id: frozenset(siblings))
    monkeypatch.setattr(repo, "get_reserved_slots", lambda adset_id, now, exclude_auth_id=None: 0)
    monkeypatch.setattr(facebook, "_get_other_launch_reserved_slots", lambda workflow, adset_id: 0)
    monkeypatch.setattr(facebook, "_get_launch_hard_reserve_slots", lambda: 1)
    return lambda: facebook._assert_live_target_safe(
        target, proof=SimpleNamespace(auth_id="auth-1"), created_ad_ids={}, now=NOW, account_inventory=inventory,
    )


def test_sibling_ad_of_same_launch_is_not_card_duplicate(monkeypatch):
    """Второй креатив города падал DUPLICATE_LIVE об первый креатив той же карточки."""
    first = ("ad-first", "CityE | Ирина / Тема А / PRODB / Ирина 1 [PRODB]")
    _live_target_check(monkeypatch, siblings={"ad-first"}, inventory_names=[first])()  # не бросает

    with pytest.raises(LaunchCheckBlocked) as info:  # то же объявление от ЧУЖОГО запуска — дубль карточки
        _live_target_check(monkeypatch, siblings=set(), inventory_names=[first])()
    assert info.value.code == "DUPLICATE_LIVE"

    own = ("ad-own", "CityE | Ирина / Тема А / PRODB / Ирина 2 [PRODB]")
    with pytest.raises(LaunchCheckBlocked):  # точное имя этого claim уже живо — дубль всегда
        _live_target_check(monkeypatch, siblings={"ad-own"}, inventory_names=[own])()


def test_postcondition_read_after_write_lag_is_retried(monkeypatch, tmp_path):
    """Созданное объявление сразу после CREATE не видно — повтор пост-чтения, а не RECONCILE."""
    sleeps = _post_harness(monkeypatch, ["blocked", "blocked", "ok"])
    result = action_adapter_launch.LaunchActionAdapter().read_postcondition(_manifest(tmp_path), ("ad-1",), NOW)
    assert result.result is ActionResult.CONFIRMED
    assert sleeps == [10.0, 20.0]


def test_postcondition_persistent_mismatch_names_its_cause(monkeypatch, tmp_path):
    sleeps = _post_harness(monkeypatch, ["blocked"] * 4)
    result = action_adapter_launch.LaunchActionAdapter().read_postcondition(_manifest(tmp_path), ("ad-1",), NOW)
    assert result.result is ActionResult.PARTIAL
    assert result.reason_code.startswith(
        "LAUNCH_POSTCONDITION_MISMATCH:LAUNCHCHECKBLOCKED:LAUNCH_POSTCONDITION_INCOMPLETE"
    )
    assert sleeps == [10.0, 20.0, 30.0]


def test_unknown_reason_is_machine_code_and_bounded():
    reason = unknown_outcome_reason(ValueError("x" * 500 + " — ошибка\nвторая строка"))
    assert reason.startswith("PROVIDER_OUTCOME_UNKNOWN:VALUEERROR:")
    assert len(reason) <= 128
    assert reason.split(":", 1)[0] == "PROVIDER_OUTCOME_UNKNOWN"
    assert unknown_outcome_reason(RuntimeError("")) == "PROVIDER_OUTCOME_UNKNOWN:RUNTIMEERROR"
