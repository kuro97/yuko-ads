"""
Launcher — готовит точный launch-манифест и передаёт его Approval Gateway.

Алгоритм:
1. Читает колонку "Готово" в Trello
2. Находит незапущенные карточки (без зелёного чекбокса)
3. Фиксирует Trello snapshot, media bytes, текст и exact adset scope в staging
4. Передаёт immutable manifest в sealed gateway
5. Только после live CONFIRMED отмечает карточку и пишет историю
"""

import logging
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from integrations.trello import (
    get_done_list_id,
    get_unlaunched_cards,
)
from services.action_manifests import build_launch_manifest
from services.approval_checker_models import (
    ActionOrigin,
    ActionResult,
    LaunchManifest,
    LaunchSourceInput,
    canonical_json,
)
from services.launch_checker import LaunchCheckBlocked, ProviderLaunchAuthorization
from services.launch_staging import release_staging, stage_launch
from services.owner_action_models import (
    EvidenceRecord,
    ProposalKind,
    ProposalOrigin,
    ProposedActionPlan,
    ProposedTarget,
    canonical_sha256,
)
from services.action_producer_gateway import PROPOSAL_TTL
from services.owner_action_repository import propose_action

logger = logging.getLogger(__name__)

MEDIA_TYPE_LABELS = {
    "video": "Видео",
    "image": "Изображение",
    "carousel": "Карусель",
    "placement_pairs": "Пары (лента+сторис)",
}

_TOKEN_RE = re.compile(
    r"(?i)(access_token(?:=|%3D)|authorization:\s*bearer\s+)[^&\s]+"
)


def _safe_error(error: object) -> str:
    return _TOKEN_RE.sub(r"\1<redacted>", str(error))


def _validate_idempotency_key(value: str | None) -> str:
    if not isinstance(value, str):
        raise LaunchCheckBlocked(
            "INVALID_IDEMPOTENCY_KEY",
            ("Для запуска обязателен canonical UUID4 idempotency key",),
            None,
        )
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise LaunchCheckBlocked(
            "INVALID_IDEMPOTENCY_KEY",
            ("Для запуска обязателен canonical UUID4 idempotency key",),
            None,
        ) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise LaunchCheckBlocked(
            "INVALID_IDEMPOTENCY_KEY",
            ("Для запуска обязателен canonical UUID4 idempotency key",),
            None,
        )
    return value


def _release_staging_safe(manifest_id: str, terminal_result: ActionResult) -> None:
    try:
        release_staging(manifest_id, terminal_result=terminal_result)
    except Exception as exc:
        logger.warning("Не удалось освободить launch staging %s: %s", manifest_id, _safe_error(exc))


def _proposal_origin(origin: ActionOrigin) -> ProposalOrigin:
    return {
        ActionOrigin.WEB: ProposalOrigin.WEB,
        ActionOrigin.AUTO_LAUNCH: ProposalOrigin.CRON,
        ActionOrigin.TELEGRAM: ProposalOrigin.TELEGRAM_COMMAND,
        ActionOrigin.LAUNCH_RECOVERY: ProposalOrigin.RECOVERY,
        ActionOrigin.REPLACEMENT: ProposalOrigin.RECOVERY,
    }.get(origin, ProposalOrigin.WEB)


def _launch_auto_approve_enabled() -> bool:
    """settings.json → {"launch": {"auto_approve": true}}.

    Постоянная директива владельца: запуск рекламы — ежедневная
    рутина, ручное подтверждение каждой карточки не требуется. Решение
    записывается как decision_source='SYSTEM' (automation_rule ниже), а не как
    нажатие владельца — аудит различает их всегда. Выключается тем же
    settings.json без деплоя.
    """
    try:
        from agent.scheduler import load_settings

        block = load_settings().get("launch")
    except Exception:  # noqa: BLE001 — битые settings не меняют поведение
        return False
    return isinstance(block, dict) and block.get("auto_approve") is True


_LAUNCH_AUTO_APPROVE_RULE = "OWNER_STANDING_DIRECTIVE_LAUNCH"


def _try_auto_approve_launch(
    proposal_id: str,
    *,
    origin: ActionOrigin,
    now: datetime,
) -> bool:
    """Самоодобряет launch-предложение по постоянной директиве владельца.

    Любой отказ репозитория (уже уехало владельцу, TTL, второе решение)
    безопасен: предложение остаётся обычным, с кнопками в Telegram.
    """
    from services.owner_action_repository import approve_by_system

    try:
        approve_by_system(
            proposal_id=proposal_id,
            automation_rule=_LAUNCH_AUTO_APPROVE_RULE,
            actor=f"launcher:{origin.value}",
            reason_text=(
                "Постоянная директива владельца: запуски "
                "исполняются без ручного подтверждения"
            ),
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — отказ = обычный путь владельца
        logger.warning(
            "launch auto-approve %s отклонён (%s) — предложение уйдёт владельцу",
            proposal_id,
            type(exc).__name__,
        )
        return False
    return True


def _owner_launch_plan(
    manifest: LaunchManifest,
    *,
    card_name: str,
    now: datetime,
) -> ProposedActionPlan:
    """Разворачивает staged launch в один exact claim на каждый будущий ad."""

    targets: list[ProposedTarget] = []
    ordinal = 0
    for destination in manifest.destinations:
        for creative in sorted(
            destination.creatives,
            key=lambda item: item.order_index,
        ):
            exact_destination = replace(
                destination,
                creatives=(creative,),
            )
            exact_manifest = replace(
                manifest,
                manifest_id=f"{manifest.manifest_id}:{ordinal}",
                destinations=(exact_destination,),
            )
            payload = json.loads(canonical_json(exact_manifest).decode("utf-8"))
            claim_id = f"launch:{manifest.manifest_id}:{ordinal}"
            targets.append(
                ProposedTarget(
                    claim_id=claim_id,
                    ordinal=ordinal,
                    action_kind="CREATE_AD",
                    account_id=destination.account_id,
                    adset_id=destination.adset_id,
                    subject_id=manifest.trello.card_id,
                    city=destination.city,
                    language=(
                        "L2"
                        if destination.adset_type.upper().endswith("L2")
                        else "L1"
                    ),
                    intended_payload=payload,
                    intended_payload_sha256=canonical_sha256(payload),
                )
            )
            ordinal += 1
    evidence_payload = {
        "card_id": manifest.trello.card_id,
        "card_content_sha256": manifest.trello.card_content_sha256,
        "media_manifest_sha256": manifest.media_manifest_sha256,
        "config_version_sha256": manifest.config_version_sha256,
        "target_count": len(targets),
    }
    return ProposedActionPlan(
        proposal_kind=ProposalKind.LAUNCH,
        origin=_proposal_origin(manifest.origin),
        idempotency_key=manifest.idempotency_key,
        source_ref=f"trello:{manifest.trello.card_id}",
        actor=f"launcher:{manifest.origin.value}",
        summary=f"Запустить «{card_name}» в {len(targets)} объявлениях",
        targets=tuple(targets),
        evidence=(
            EvidenceRecord(
                evidence_kind="STAGED_LAUNCH",
                source_system="TRELLO_MEDIA_CONFIG",
                subject_id=manifest.trello.card_id,
                observed_at=now,
                complete=True,
                payload=evidence_payload,
                payload_sha256=canonical_sha256(evidence_payload),
            ),
        ),
        config_version_sha256=manifest.config_version_sha256,
        # Общий TTL предложений: карточка обязана переживать суточный цикл
        # дайджеста. Свои 24 часа здесь уже убивали одобренные запуски пачками
        # PROPOSAL_EXPIRED — владелец видел карточку, когда жить ей оставалось
        # меньше пары часов.
        valid_until=now + PROPOSAL_TTL,
        staged_media_root=Path(manifest.staging_directory),
    )


def launch_single(
    card_id: str,
    card_name: str,
    card_desc: str,
    status: dict,
    tenant_id: str | None = None,
    campaign_type: str = "leadgen",
    cities: list[str] | None = None,
    as_carousel: bool = False,
    trello_labels: list[str] | None = None,
    mark_done_on_complete: bool = True,
    prepare_city_cb=None,
    city_success_cb=None,
    authorization: ProviderLaunchAuthorization | None = None,
    prepared_media: Mapping[str, Any] | None = None,
    *,
    idempotency_key: str | None = None,
    origin: ActionOrigin = ActionOrigin.LEGACY_LAUNCHER,
    now: datetime | None = None,
    allow_checked: bool = False,
) -> dict:
    """Готовит exact launch и выполняет его только через sealed gateway.

    Старые provider proof/media/callback аргументы оставлены в сигнатуре лишь для
    понятного fail-closed ответа во время миграции callers. Они никогда не дают
    доступ к Facebook mutation из launcher.
    """

    del card_desc, trello_labels, mark_done_on_complete
    status["outcome"] = "running"
    status.setdefault("check_id", None)
    status["reason_codes"] = []
    status["reasons"] = []
    status.setdefault("log", [])
    status["step"] = "Проверяю точный план запуска…"
    status["step_pct"] = None
    prepared = None
    try:
        canonical_key = _validate_idempotency_key(idempotency_key)
        if not isinstance(origin, ActionOrigin):
            raise LaunchCheckBlocked(
                "INVALID_ACTION_ORIGIN",
                ("Источник запуска не входит в закрытый список",),
                None,
            )
        if any(
            value is not None
            for value in (
                prepare_city_cb,
                city_success_cb,
                authorization,
                prepared_media,
            )
        ):
            raise LaunchCheckBlocked(
                "LEGACY_LAUNCH_BYPASS_FORBIDDEN",
                ("Raw proof, media и callbacks больше не принимаются launcher",),
                None,
            )

        source = LaunchSourceInput(
            card_id=card_id,
            campaign_type=campaign_type,
            requested_cities=tuple(str(city) for city in (cities or ())),
            as_carousel=as_carousel,
            origin_reference=tenant_id or "legacy-launcher",
            allow_checked=bool(allow_checked and origin is ActionOrigin.AUTO_LAUNCH),
        )
        prepared = stage_launch(source, manifest_id=str(uuid.uuid4()))
        if origin is ActionOrigin.AUTO_LAUNCH:
            from services.auto_launch import prepare_staged_launch_for_gateway

            prepared = prepare_staged_launch_for_gateway(prepared, canonical_key)
        manifest_now = now or datetime.now(timezone.utc)
        manifest = build_launch_manifest(
            prepared,
            origin=origin,
            idempotency_key=canonical_key,
            now=manifest_now,
        )
        status["total"] = len(manifest.destinations)
        status["log"].append("План запуска зафиксирован; требуется решение владельца")
        # Владелец одобряет по summary, поэтому имя берём из проверенного
        # Trello-снапшота (prepared), а не из недоверенного аргумента caller'а.
        verified_card_name = getattr(prepared, "card_name", "") or card_name
        receipt = propose_action(
            _owner_launch_plan(
                manifest,
                card_name=verified_card_name,
                now=manifest_now,
            ),
            now=manifest_now,
        )
        status["proposal_id"] = receipt.proposal_id
        status["proposal_state"] = receipt.state
        if _launch_auto_approve_enabled() and _try_auto_approve_launch(
            receipt.proposal_id,
            origin=origin,
            now=manifest_now,
        ):
            status["outcome"] = "queued_auto"
            status["running"] = False
            status["log"].append(
                "Одобрено по постоянной директиве владельца — в очереди исполнения"
            )
        else:
            status["outcome"] = "pending_owner"
            status["running"] = False
            status["log"].append("⏳ Предложение отправлено владельцу")
        # Staging остаётся закреплён за immutable proposal до terminal lifecycle.
        return {"proposal_id": receipt.proposal_id}
    except LaunchCheckBlocked as exc:
        safe_reasons = [_safe_error(reason) for reason in exc.reasons]
        status["outcome"] = "blocked"
        status["check_id"] = exc.check_id
        status["reason_codes"] = [exc.code]
        status["reasons"] = safe_reasons
        status["log"].append(
            f"⛔ Запуск заблокирован: {exc.code}: {'; '.join(safe_reasons)}"
        )
        logger.warning("launch_single blocked for card %s: %s", card_id, exc.code)
        if prepared is not None:
            _release_staging_safe(prepared.manifest_id, ActionResult.FAILED)
        raise
    except Exception as exc:
        safe_error = _safe_error(exc)
        logger.error("launch_single failed for card %s: %s", card_id, safe_error)
        status["log"].append(f"❌ Ошибка: {safe_error}")
        status["outcome"] = "failed"
        # Пустой возврат сам по себе не объясняет причину, поэтому caller
        # получает её через status: иначе он рапортует «нет proposal_id»
        # вместо настоящей ошибки подготовки запуска.
        status["error"] = safe_error
        status["reconciliation_required"] = False
        if prepared is not None:
            _release_staging_safe(prepared.manifest_id, ActionResult.FAILED)
        return {}
    finally:
        status["step"] = ""
        status["step_pct"] = None
        status["running"] = False


def run(
    *,
    idempotency_key_factory: Callable[[], str] | None = None,
    plan_provider: Callable[[Mapping[str, Any]], object] | None = None,
) -> list[dict]:
    """Запускает Ready-карточки только через server-owned staging/gateway.

    ``plan_provider`` может сузить campaign/cities, но его raw media,
    callbacks и provider authorization никогда не передаются в execution.
    """

    print("🔍 Проверяю Trello колонку 'Готово'...")
    list_id = get_done_list_id()
    cards = get_unlaunched_cards(list_id)
    if not cards:
        print("✅ Нет новых карточек для запуска.")
        return []

    make_key = idempotency_key_factory or (lambda: str(uuid.uuid4()))
    results: list[dict] = []
    print(f"📋 Найдено незапущенных карточек: {len(cards)}")
    for card in cards:
        card_id = str(card["id"])
        card_name = str(card["name"])
        card_desc = str(card.get("desc") or "")
        print(f"\n{'=' * 50}")
        print(f"📌 Обрабатываю: {card_name}")
        status = {
            "running": True,
            "current": card_name,
            "progress": 0,
            "total": 0,
            "step": "",
            "step_pct": None,
            "log": [],
            "outcome": "running",
            "check_id": None,
            "reason_codes": [],
            "reasons": [],
        }
        try:
            from services.launch_checker import (
                LaunchCheckRequest,
                LaunchCheckStatus,
                LaunchSource,
                check_candidate,
            )

            quick_check = check_candidate(
                card,
                LaunchCheckRequest(
                    source=LaunchSource.AGENT_RUN,
                    campaign_type="leadgen",
                    cities=None,
                    as_carousel=False,
                    actor="system:agent-run",
                ),
                {},
            )
            if quick_check.status is LaunchCheckStatus.BLOCKED:
                raise LaunchCheckBlocked(
                    quick_check.reason_codes[0],
                    quick_check.reasons,
                    quick_check.check_id,
                )
            if plan_provider is None:
                ad_ids = launch_single(
                    card_id,
                    card_name,
                    card_desc,
                    status,
                    idempotency_key=make_key(),
                    origin=ActionOrigin.LEGACY_LAUNCHER,
                )
            else:
                checked_plan = plan_provider(card)
                request = getattr(checked_plan, "request", None)
                if (
                    str(getattr(checked_plan, "card_id", "") or "") != card_id
                    or request is None
                ):
                    raise LaunchCheckBlocked(
                        "AUTHORIZATION_SCOPE_DRIFT",
                        ("Checker plan не совпадает с Ready-карточкой",),
                        getattr(checked_plan, "check_id", None),
                    )
                requested_cities = getattr(request, "cities", None)
                ad_ids = launch_single(
                    card_id,
                    card_name,
                    card_desc,
                    status,
                    campaign_type=str(getattr(request, "campaign_type", "") or ""),
                    cities=(
                        list(requested_cities)
                        if requested_cities is not None
                        else None
                    ),
                    as_carousel=bool(getattr(request, "as_carousel", False)),
                    idempotency_key=make_key(),
                    origin=ActionOrigin.LEGACY_LAUNCHER,
                )
            result = {
                "card": card_name,
                "status": status["outcome"],
                "ads": ad_ids,
                "check_id": status.get("check_id"),
                "operation_id": status.get("operation_id"),
            }
        except LaunchCheckBlocked as exc:
            print(f"⛔ Запуск заблокирован: {exc.code}")
            result = {
                "card": card_name,
                "status": "blocked",
                "check_id": exc.check_id,
                "reason_codes": [exc.code],
                "reasons": list(exc.reasons),
            }
        except Exception as exc:
            safe_error = _safe_error(exc)
            print(f"❌ Ошибка при запуске: {safe_error}")
            result = {"card": card_name, "status": "failed", "reason": safe_error}
        results.append(result)
        if result["status"] != "succeeded":
            break
    return results


if __name__ == "__main__":
    results = run()
    print(f"\n{'='*50}")
    print(f"📊 Итог: {len([r for r in results if r['status'] == 'succeeded'])} запущено, "
          f"{len([r for r in results if r['status'] == 'partial'])} частично, "
          f"{len([r for r in results if r['status'] == 'failed'])} ошибок, "
          f"{len([r for r in results if r['status'] == 'blocked'])} заблокировано")
