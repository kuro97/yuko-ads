"""Закрытый LAUNCH adapter для sealed approval gateway."""

from __future__ import annotations

import logging

import hashlib
import json
import secrets
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from integrations.facebook import (
    _execute_launch_manifest_unchecked,
    _observe_launch_manifest_postcondition,
    _semantic_projection,
    fetch_complete_account_ad_inventory,
)
from services.action_gateway_core import (
    AdapterMutationResult,
    AdapterPostconditionResult,
)
from services.action_locks import adset_locks, launch_execution_lease
from services.action_manifests import manifest_from_operation_record
from services.approval_checker_models import (
    ActionBatchManifest,
    ActionManifest,
    ActionObservation,
    ActionOrigin,
    ActionResult,
    LaunchManifest,
    OperationItemRecord,
    PreparedLaunch,
    SourceSystem,
    canonical_json,
    manifest_sha256,
)
from services.owner_action_models import ActionAttemptAttestation
from services.approval_source_facebook import _load_exact_ads
from services.approval_sources import load_action_item_evidence
from services.launch_checker import LaunchCheckBlocked
from services.launch_checker import ProviderLaunchAuthorization
from services import launch_repository
from services.launch_staging import verify_staged_launch


_ACTION_SOURCES = frozenset(
    {
        SourceSystem.FACEBOOK,
        SourceSystem.TRELLO,
        SourceSystem.MEDIA_BYTES,
        SourceSystem.AMO,
        SourceSystem.CDP_ERP,
    }
)


def _require_launch(item: ActionManifest) -> LaunchManifest:
    if not isinstance(item, LaunchManifest):
        raise TypeError("LaunchActionAdapter принимает только LaunchManifest")
    return item


def _single_item_batch(item: LaunchManifest) -> ActionBatchManifest:
    """Строит внутренний batch только для повторного five-source read."""

    draft = ActionBatchManifest(
        batch_manifest_id=f"precondition:{item.manifest_id}",
        correlation_id=f"precondition:{item.manifest_id}",
        idempotency_key=item.idempotency_key,
        prepared_at=item.prepared_at,
        subject_ids=(f"card:{item.trello.card_id}",),
        actions=(item,),
        manifest_sha256="0" * 64,
    )
    return replace(draft, manifest_sha256=manifest_sha256(draft))


def _prepared(item: LaunchManifest) -> PreparedLaunch:
    return PreparedLaunch(
        manifest_id=item.manifest_id,
        staged_at=item.prepared_at,
        staging_root=Path(item.staging_root),
        staging_directory=Path(item.staging_directory),
        trello=item.trello,
        # Marker хранит только SHA-256 имени, raw name для execution не нужен.
        card_name="",
        card_name_sha256=item.card_name_sha256,
        campaign_type=item.campaign_type,
        config_version_sha256=item.config_version_sha256,
        media_assets=item.media_assets,
        destinations=item.destinations,
        media_manifest_sha256=item.media_manifest_sha256,
    )


def _manifest_card_root(item: LaunchManifest) -> str:
    """Восстанавливает stable card-root из exact server-owned ad names."""
    roots: set[str] = set()
    for destination in item.destinations:
        prefix = f"{destination.city} | "
        for creative in destination.creatives:
            if not creative.ad_name.startswith(prefix):
                raise LaunchCheckBlocked(
                    "INVALID_LAUNCH_MANIFEST",
                    ("Staged ad name не содержит exact city prefix",),
                    item.manifest_id,
                )
            remainder = creative.ad_name[len(prefix):]
            normalized = launch_repository.normalize_launch_name(remainder)
            # Отрезается только asset-суффикс (последний сегмент « / 1 блоггер»),
            # а не всё после первого « / »: имена карточек сами содержат « / »
            # («Блогер /PRODB оффлайн / Тема А / Подтема 1»), и
            # обрубок ловил чужие карточки того же блогера как DUPLICATE_LIVE.
            roots.add(normalized.rsplit(" / ", 1)[0])
    if len(roots) != 1:
        raise LaunchCheckBlocked(
            "INVALID_LAUNCH_MANIFEST",
            ("Staged ad names не образуют единый card-root",),
            item.manifest_id,
        )
    return next(iter(roots))


def _reserve_manifest_authorization(
    item: LaunchManifest,
    now: datetime,
) -> ProviderLaunchAuthorization:
    """Approval permit не bypass: выдаём обычный DB-backed proof."""
    secret = secrets.token_urlsafe(32)
    proof = ProviderLaunchAuthorization(
        auth_id=_gateway_auth_id(item.manifest_id),
        secret=secret,
    )
    from config import FB_ACCOUNT_ID_ONLINE

    online_account_id = str(FB_ACCOUNT_ID_ONLINE).removeprefix("act_")
    targets = tuple(
        SimpleNamespace(
            city=destination.city,
            ordinal=ordinal,
            account_kind=(
                "online"
                if destination.account_id.removeprefix("act_") == online_account_id
                else "offline"
            ),
            account_id=destination.account_id.removeprefix("act_"),
            adset_id=destination.adset_id,
            reserved_slots=len(destination.creatives),
        )
        for ordinal, destination in enumerate(item.destinations)
    )
    expected_names = {
        destination.city: tuple(
            creative.ad_name
            for creative in sorted(
                destination.creatives,
                key=lambda creative: creative.order_index,
            )
        )
        for destination in item.destinations
    }
    plan = SimpleNamespace(
        check_id=f"gateway-check-{item.manifest_id}",
        card_id=item.trello.card_id,
        card_name=_manifest_card_root(item),
        request=SimpleNamespace(
            source="MANUAL",
            campaign_type=item.campaign_type,
            actor=f"approval-gateway:{item.origin.value}",
            override_topic_veto=False,
            override_reason=None,
        ),
        media_sha256=item.media_manifest_sha256,
        targets=targets,
        expected_names_by_city=expected_names,
        authorization=proof,
    )
    launch_repository.reserve_authorization(
        plan,
        hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        now,
    )
    return proof


def _gateway_auth_id(manifest_id: str) -> str:
    return (
        "launch-auth-gateway-"
        + hashlib.sha256(manifest_id.encode("utf-8")).hexdigest()[:32]
    )


def _provably_no_create_claims(item: LaunchManifest, auth_id: str) -> bool:
    """True только если репозиторий подтвердил: ни один claim по авторизации не начат.

    Claim пишется непосредственно перед POST (integrations.facebook, claim_provider_create), поэтому
    «claim'ов нет» = «объявлений нет». Любая неясность (сбой чтения БД) → False → исход UNKNOWN.
    """
    try:
        return not launch_repository.authorization_has_create_claims(auth_id)
    except Exception:  # noqa: BLE001 — неизвестность = не доказано
        import logging

        logging.getLogger(__name__).exception(
            "action_adapter_launch: проверка claim по %s не удалась — исход считаем неизвестным",
            item.manifest_id,
        )
        return False


def _no_effect_result(
    item: LaunchManifest, exc: BaseException, *, stage: str, auto_launch_origin: bool
) -> AdapterMutationResult:
    """FAILED без эффекта у провайдера: следующий claim карточки исполняется своим чередом."""
    import logging

    from services.action_gateway_core import machine_reason

    reason = machine_reason(f"LAUNCH_NO_EFFECT_{stage}", exc)
    logging.getLogger(__name__).warning(
        "action_adapter_launch: %s упал до провайдера (%s) — провал без эффекта: %s",
        item.manifest_id,
        stage,
        reason,
    )
    if auto_launch_origin:
        try:
            from services.auto_launch import record_gateway_launch_no_effect

            record_gateway_launch_no_effect(item, reason)
        except Exception:  # noqa: BLE001 — журнал попытки не должен менять исход
            logging.getLogger(__name__).exception(
                "action_adapter_launch: провал без эффекта по %s не записан в попытку", item.manifest_id
            )
    return AdapterMutationResult(
        result=ActionResult.FAILED,
        created_ids=(),
        reason_code=reason,
        remote_may_have_changed=False,
    )


def _retract_create_marker_without_claims(item: LaunchManifest, auth_id: str) -> None:
    """Снимает CREATE-маркер auto_launch, если провайдер claim не делал.

    Fail-closed: любой сбой чтения БД или наличие хотя бы одного claim
    оставляет маркер — тогда сверка обязана блокировать авторетрай.
    """
    import logging

    log = logging.getLogger(__name__)
    try:
        has_claims = launch_repository.authorization_has_create_claims(auth_id)
    except Exception:  # noqa: BLE001 — неизвестность = маркер остаётся
        log.exception(
            "action_adapter_launch: проверка claim по %s не удалась — маркер оставлен",
            item.manifest_id,
        )
        return
    if has_claims:
        return
    try:
        from services.auto_launch import unmark_gateway_create_started

        unmark_gateway_create_started(item)
    except Exception:  # noqa: BLE001 — снятие маркера не должно скрыть исходную ошибку
        log.exception(
            "action_adapter_launch: снять CREATE-маркер %s не удалось",
            item.manifest_id,
        )


def _verify_binding_row(binding: dict, live_ad: dict[str, object]) -> tuple[str, str]:
    """Сверяет exact ad scope и creative semantics с durable pre-POST claim."""

    ad_id = str(live_ad.get("id") or "")
    if (
        not ad_id
        or str(live_ad.get("name") or "") != str(binding["ad_name"])
        or str(live_ad.get("adset_id") or "") != str(binding["adset_id"])
    ):
        raise ValueError("LAUNCH_BINDING_SCOPE_DRIFT")
    creative = live_ad.get("creative")
    if not isinstance(creative, dict):
        raise ValueError("LAUNCH_BINDING_CREATIVE_MISSING")
    creative_id = str(creative.get("id") or "")
    if not creative_id:
        raise ValueError("LAUNCH_BINDING_CREATIVE_ID_MISSING")
    expected_payload = json.loads(str(binding["expected_payload_json"]))
    if not isinstance(expected_payload, dict):
        raise ValueError("LAUNCH_BINDING_PAYLOAD_INVALID")
    if "creative_id" in expected_payload:
        if str(expected_payload["creative_id"]) != creative_id:
            raise ValueError("LAUNCH_BINDING_CREATIVE_ID_DRIFT")
    else:
        projection = _semantic_projection(creative, expected_payload)
        if canonical_json(projection) != canonical_json(expected_payload):
            raise ValueError("LAUNCH_BINDING_SEMANTIC_DRIFT")
    fingerprint = hashlib.sha256(canonical_json(expected_payload)).hexdigest()
    if fingerprint != str(binding["expected_fingerprint"]):
        raise ValueError("LAUNCH_BINDING_FINGERPRINT_DRIFT")
    return ad_id, creative_id


def _five_source_observation(
    item: LaunchManifest,
    now: datetime,
) -> ActionObservation:
    if not verify_staged_launch(_prepared(item)):
        raise RuntimeError("LAUNCH_STAGING_DRIFT")
    bundle = load_action_item_evidence(_single_item_batch(item), item, 0, now)
    sources = {source.source for source in bundle.sources}
    if sources != _ACTION_SOURCES or any(
        not source.complete or source.from_cache for source in bundle.sources
    ):
        raise RuntimeError("LAUNCH_FIVE_SOURCE_INCOMPLETE")
    if len(bundle.freshness) != len(_ACTION_SOURCES) or any(
        not freshness.fresh or not freshness.complete or freshness.from_cache
        for freshness in bundle.freshness
    ):
        raise RuntimeError("LAUNCH_FIVE_SOURCE_STALE")
    return ActionObservation(
        observed_at=now,
        digest=bundle.final_live_state_sha256,
        target_state="READY",
        subject_ids=(f"card:{item.trello.card_id}",),
        unrelated_state_digest=hashlib.sha256(
            canonical_json(
                (
                    bundle.facebook_sha256,
                    bundle.trello_sha256,
                    bundle.media_sha256,
                    bundle.amo_sha256,
                    bundle.cdp_sha256,
                )
            )
        ).hexdigest(),
    )


# Паузы перед повторными пост-чтениями после CREATE (секунды). Сумма держится в бюджете задания.
_POSTCONDITION_RETRY_DELAYS: tuple[float, ...] = (0.0, 10.0, 20.0, 30.0)


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


class LaunchActionAdapter:
    """Исполняет ровно immutable staged manifest без discovery/callbacks."""

    def execution_scope(
        self,
        item: ActionManifest,
        now: datetime,
    ) -> AbstractContextManager[None]:
        del now
        manifest = _require_launch(item)
        return self._locked_scope(manifest)

    @staticmethod
    @contextmanager
    def _locked_scope(item: LaunchManifest):
        adset_ids = tuple(destination.adset_id for destination in item.destinations)
        # Порядок фиксирован: neutral launch lease -> sorted adset locks.
        with launch_execution_lease(item.manifest_id):
            with adset_locks(adset_ids):
                yield

    def read_precondition(
        self,
        item: ActionManifest,
        now: datetime,
    ) -> ActionObservation:
        return _five_source_observation(_require_launch(item), now)

    def mutate(
        self,
        item: ActionManifest,
        now: datetime,
        *,
        attempt: ActionAttemptAttestation,
    ) -> AdapterMutationResult:
        manifest = _require_launch(item)
        # Порядок: резервация → CREATE-маркер auto_launch → исполнитель.
        # Резервация — только БД, объявлений не создаёт; её падение (capacity,
        # дубль) не должно оставлять улику CREATE. Исполнитель до первого
        # claim тоже объявлений не создаёт — на его падении маркер снимается,
        # если репозиторий подтверждает отсутствие claim.
        #
        # Сбой ДО провайдера — это «провал без эффекта», а не «исход неизвестен»:
        # раньше любое исключение здесь уходило в гейтвей как
        # PROVIDER_OUTCOME_UNKNOWN, задание — в RECONCILE_REQUIRED навсегда, а
        # остальные города карточки переставали исполняться.
        auto_launch_origin = manifest.origin is ActionOrigin.AUTO_LAUNCH
        try:
            proof = _reserve_manifest_authorization(manifest, now)
        except Exception as exc:  # noqa: BLE001 — резервация только БД, объявлений нет
            return _no_effect_result(manifest, exc, stage="RESERVE", auto_launch_origin=auto_launch_origin)
        try:
            if auto_launch_origin:
                from services.auto_launch import mark_gateway_create_started

                mark_gateway_create_started(manifest)
        except Exception as exc:  # noqa: BLE001 — маркер не поставлен, провайдера не трогали
            return _no_effect_result(manifest, exc, stage="MARKER", auto_launch_origin=auto_launch_origin)
        try:
            created_ids = tuple(
                _execute_launch_manifest_unchecked(
                    manifest,
                    attempt=attempt,
                    authorization=proof,
                )
            )
        except Exception as exc:
            if auto_launch_origin:
                _retract_create_marker_without_claims(manifest, proof.auth_id)
            if _provably_no_create_claims(manifest, proof.auth_id):
                return _no_effect_result(manifest, exc, stage="PRE_POST", auto_launch_origin=auto_launch_origin)
            raise
        expected_count = sum(
            len(destination.creatives) for destination in manifest.destinations
        )
        if len(created_ids) != expected_count or len(created_ids) != len(set(created_ids)):
            return AdapterMutationResult(
                result=ActionResult.PARTIAL,
                created_ids=created_ids,
                reason_code="LAUNCH_CREATE_PARTIAL",
                remote_may_have_changed=True,
            )
        if manifest.origin is ActionOrigin.AUTO_LAUNCH:
            from services.auto_launch import record_gateway_launch_result

            record_gateway_launch_result(manifest, created_ids)
        return AdapterMutationResult(result=None, created_ids=created_ids)

    def read_postcondition(
        self,
        item: ActionManifest,
        created_ids: tuple[str, ...],
        now: datetime,
    ) -> AdapterPostconditionResult:
        manifest = _require_launch(item)
        expected_count = sum(
            len(destination.creatives) for destination in manifest.destinations
        )
        bindings = launch_repository.get_provider_ad_bindings(
            _gateway_auth_id(manifest.manifest_id)
        )
        bound_ids = tuple(
            str(binding.get("ad_id") or "")
            for binding in bindings
            if binding.get("phase") == "VERIFIED"
        )
        if (
            len(bindings) != expected_count
            or len(bound_ids) != expected_count
            or set(bound_ids) != set(created_ids)
            or any(
                binding.get("verified_fingerprint") != binding.get("expected_fingerprint")
                for binding in bindings
            )
        ):
            return AdapterPostconditionResult(
                result=ActionResult.PARTIAL,
                observation=None,
                created_ids=created_ids,
                reason_code="LAUNCH_SEMANTIC_BINDINGS_REQUIRED",
                remote_may_have_changed=True,
            )
        observation = None
        last_block: LaunchCheckBlocked | None = None
        # Чтение сразу после CREATE ловит задержку Facebook (объявление ещё не видно в
        # инвентаре адсета или статус не устоялся): созданные и активные объявления
        # уходили в RECONCILE_REQUIRED, а та же проверка минутой позже проходила. Повторяем
        # пост-чтение с паузой; мутаций не делаем, только читаем.
        for attempt_index, delay in enumerate(_POSTCONDITION_RETRY_DELAYS):
            if delay:
                _sleep(delay)
            try:
                observation = _observe_launch_manifest_postcondition(
                    manifest,
                    created_ids,
                    now,
                )
                break
            except LaunchCheckBlocked as blocked:
                last_block = blocked
                logging.getLogger(__name__).warning(
                    "action_adapter_launch: post-read %s не сошёлся (попытка %d): %s %s",
                    manifest.manifest_id,
                    attempt_index + 1,
                    getattr(blocked, "code", ""),
                    "; ".join(getattr(blocked, "reasons", ()) or ()),
                )
            except Exception:
                # Немой except прятал настоящую причину «исход неизвестен»:
                # пост-проверка падала на потолке страниц cabinet_a, а в
                # аудите оставалось только LAUNCH_POSTCONDITION_UNAVAILABLE.
                logging.getLogger(__name__).exception(
                    "action_adapter_launch: post-read манифеста %s упал — "
                    "исход у провайдера неизвестен",
                    getattr(item, "manifest_id", "?"),
                )
                return AdapterPostconditionResult(
                    result=ActionResult.UNKNOWN,
                    observation=None,
                    created_ids=created_ids,
                    reason_code="LAUNCH_POSTCONDITION_UNAVAILABLE",
                    remote_may_have_changed=True,
                )
        if observation is None:
            from services.action_gateway_core import machine_reason

            return AdapterPostconditionResult(
                result=ActionResult.PARTIAL,
                observation=None,
                created_ids=created_ids,
                reason_code=machine_reason(
                    "LAUNCH_POSTCONDITION_MISMATCH",
                    last_block or LaunchCheckBlocked("UNKNOWN", (), None),
                ),
                remote_may_have_changed=True,
            )
        return AdapterPostconditionResult(
            result=ActionResult.CONFIRMED,
            observation=observation,
            created_ids=created_ids,
            reason_code="LAUNCH_EXACT_IDS_CONFIRMED",
            remote_may_have_changed=True,
        )

    def finalize_after_scope(
        self,
        item: ActionManifest,
        mutation: AdapterMutationResult,
        post: AdapterPostconditionResult,
        now: datetime,
    ) -> None:
        """Закрывает gateway-авторизацию — иначе она умирает только по TTL.

        Исправленный баг: адаптер резервировал авторизацию в
        _reserve_manifest_authorization и никогда не закрывал — finish звал
        только старый прямой путь. Полчаса TTL резервация ела capacity
        адсетов и блокировала чужие запуски (_find_collision →
        DUPLICATE_RESERVED). Закрываем на ЛЮБОМ исходе: успех — COMPLETED,
        провал/неизвестно — BLOCKED (слоты освобождаются, аудит остаётся).
        """
        manifest = _require_launch(item)
        confirmed = mutation.result is ActionResult.CONFIRMED or (
            post is not None and post.result is ActionResult.CONFIRMED
        )
        try:
            launch_repository.finish_authorization(
                _gateway_auth_id(manifest.manifest_id),
                "COMPLETED" if confirmed else "BLOCKED",
                now,
            )
        except Exception:  # noqa: BLE001 — закрытие не должно ронять исполнение
            import logging

            logging.getLogger(__name__).exception(
                "action_adapter_launch: finish_authorization(%s) не удалось",
                manifest.manifest_id,
            )

    def reconcile(
        self,
        item: OperationItemRecord,
        now: datetime,
    ) -> AdapterPostconditionResult:
        try:
            manifest = manifest_from_operation_record(item, LaunchManifest)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=None,
                created_ids=item.exact_created_ids,
                reason_code="WAL_MANIFEST_INVALID",
                remote_may_have_changed=True,
            )
        expected_count = sum(
            len(destination.creatives) for destination in manifest.destinations
        )
        auth_id = _gateway_auth_id(manifest.manifest_id)
        bindings = launch_repository.get_provider_ad_bindings(auth_id)
        if len(bindings) != expected_count:
            return AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=None,
                created_ids=item.exact_created_ids,
                reason_code="LAUNCH_BINDINGS_INCOMPLETE",
                remote_may_have_changed=True,
            )
        try:
            from config import FB_ACCOUNT_ID_ONLINE

            online_id = str(FB_ACCOUNT_ID_ONLINE).removeprefix("act_")
            for binding in bindings:
                phase = str(binding["phase"])
                if phase == "AMBIGUOUS":
                    return AdapterPostconditionResult(
                        result=ActionResult.PARTIAL,
                        observation=None,
                        created_ids=item.exact_created_ids,
                        reason_code="LAUNCH_SEMANTIC_AMBIGUOUS",
                        remote_may_have_changed=True,
                    )
                if phase == "VERIFIED":
                    continue
                destination = next(
                    target
                    for target in manifest.destinations
                    if target.adset_id == str(binding["adset_id"])
                )
                account_id = destination.account_id.removeprefix("act_")
                account_kind = "online" if account_id == online_id else "offline"
                inventory = fetch_complete_account_ad_inventory(account_kind, account_id)
                candidates = []
                for row in inventory:
                    if (
                        str(row.get("name") or "") != str(binding["ad_name"])
                        or str(row.get("adset_id") or "") != str(binding["adset_id"])
                    ):
                        continue
                    created_at = datetime.fromisoformat(
                        str(row.get("created_time") or "").replace("Z", "+00:00")
                    )
                    if item.attempted_at is None or created_at >= item.attempted_at:
                        candidates.append(str(row.get("id") or ""))
                if not candidates:
                    return AdapterPostconditionResult(
                        result=ActionResult.UNKNOWN,
                        observation=None,
                        created_ids=item.exact_created_ids,
                        reason_code="LAUNCH_CANDIDATE_NOT_FOUND",
                        remote_may_have_changed=True,
                    )
                if len(candidates) != 1:
                    return AdapterPostconditionResult(
                        result=ActionResult.PARTIAL,
                        observation=None,
                        created_ids=tuple(sorted(candidates)),
                        reason_code="LAUNCH_CANDIDATE_AMBIGUOUS",
                        remote_may_have_changed=True,
                    )
                live = _load_exact_ads((candidates[0],))[candidates[0]]
                ad_id, creative_id = _verify_binding_row(binding, live)
                launch_repository.record_provider_ad_verified(
                    str(binding["claim_id"]),
                    ad_id,
                    creative_id,
                    str(binding["expected_fingerprint"]),
                    now,
                )
                launch_repository.record_provider_create_success(
                    str(binding["claim_id"]),
                    ad_id,
                    now,
                )
            bindings = launch_repository.get_provider_ad_bindings(auth_id)
            created_ids = tuple(str(binding["ad_id"] or "") for binding in bindings)
            if (
                len(created_ids) != expected_count
                or any(not ad_id for ad_id in created_ids)
                or len(set(created_ids)) != expected_count
            ):
                raise ValueError("LAUNCH_VERIFIED_CARDINALITY_INVALID")
            if item.exact_created_ids and set(item.exact_created_ids) != set(created_ids):
                return AdapterPostconditionResult(
                    result=ActionResult.PARTIAL,
                    observation=None,
                    created_ids=created_ids,
                    reason_code="LAUNCH_CREATED_IDS_CONFLICT",
                    remote_may_have_changed=True,
                )
            rows = _load_exact_ads(created_ids)
            for binding in bindings:
                ad_id, creative_id = _verify_binding_row(
                    binding,
                    rows[str(binding["ad_id"])],
                )
                if (
                    creative_id != str(binding["creative_id"])
                    or str(binding["verified_fingerprint"])
                    != str(binding["expected_fingerprint"])
                ):
                    raise ValueError("LAUNCH_VERIFIED_BINDING_DRIFT")
        except Exception:
            return AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=None,
                created_ids=item.exact_created_ids,
                reason_code="LAUNCH_RECONCILIATION_UNAVAILABLE",
                remote_may_have_changed=True,
            )
        payload = tuple(
            {
                "ad_id": ad_id,
                "adset_id": str(rows[ad_id].get("adset_id") or ""),
                "configured_status": str(rows[ad_id].get("status") or ""),
                "effective_status": str(rows[ad_id].get("effective_status") or ""),
                "creative_id": str(
                    (rows[ad_id].get("creative") or {}).get("id") or ""
                ),
            }
            for ad_id in sorted(created_ids)
        )
        observation = ActionObservation(
            observed_at=now,
            digest=hashlib.sha256(canonical_json(payload)).hexdigest(),
            target_state="|".join(
                f"{row['configured_status']}:{row['effective_status']}"
                for row in payload
            ),
            subject_ids=item.subject_ids,
            unrelated_state_digest=hashlib.sha256(canonical_json(())).hexdigest(),
        )
        allowed_effective = {"ACTIVE", "PENDING_REVIEW", "IN_PROCESS"}
        live_shape_valid = all(
            row["configured_status"] == "ACTIVE"
            and row["effective_status"] in allowed_effective
            and row["adset_id"]
            and row["creative_id"]
            for row in payload
        )
        return AdapterPostconditionResult(
            result=ActionResult.CONFIRMED if live_shape_valid else ActionResult.PARTIAL,
            observation=observation,
            created_ids=created_ids,
            reason_code=(
                "LAUNCH_BINDINGS_CONFIRMED"
                if live_shape_valid
                else "LAUNCH_CREATED_IDS_CONFLICT"
            ),
            remote_may_have_changed=True,
        )
