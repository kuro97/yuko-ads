"""Production-adapters for :mod:`services.launch_checker`.

Этот модуль связывает чистый checker с Trello/Drive/Facebook. Импорты внешних
интеграций остаются внутри функций: checker можно импортировать в web, cron и
CLI без ранней инициализации токенов или сетевых клиентов.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import logging
import os
import shutil
import tempfile
from typing import Any, Mapping, Sequence

from services.launch_checker import (
    CheckerMode,
    LaunchCheckBlocked,
    LaunchChecker,
    LaunchCheckRequest,
    LaunchTarget,
    LiveAd,
    LiveAdsetInventory,
    PreparedLaunchMedia,
    ProviderLaunchAuthorization,
)


logger = logging.getLogger(__name__)


class LaunchLifecycleOutcome(StrEnum):
    """Terminal/durable итог единого lifecycle authorization."""

    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    BLOCKED_RECONCILE = "BLOCKED_RECONCILE"
    BLOCKED = "BLOCKED"
    RELEASED = "RELEASED"


@dataclass(frozen=True, slots=True)
class LaunchAuthorizationFinalization:
    """Несекретный итог finalizer, пригодный для API и daily cap."""

    auth_id: str
    outcome: LaunchLifecycleOutcome
    total_ads: int
    created_ads: int
    created_ad_ids: tuple[str, ...]
    needs_reconcile: bool
    consumes_daily_slot: bool


@dataclass(frozen=True, slots=True)
class LaunchAuthorizationReconciliation:
    """Итог exact fresh-live reconciliation; proof остаётся только в памяти."""

    auth_id: str
    outcome: LaunchLifecycleOutcome
    authorization: ProviderLaunchAuthorization | None
    created_ad_ids: tuple[str, ...]
    missing_names_by_city: Mapping[str, tuple[str, ...]]


def _auth_id(authorization_or_auth_id: object) -> str:
    value = (
        authorization_or_auth_id
        if isinstance(authorization_or_auth_id, str)
        else getattr(authorization_or_auth_id, "auth_id", None)
    )
    auth_id = str(value or "").strip()
    if not auth_id:
        raise LaunchCheckBlocked(
            "AUTHORIZATION_REQUIRED",
            ("Lifecycle finalizer не получил authorization",),
            None,
        )
    return auth_id


def _translate_repository_error(exc: Exception, auth_id: str) -> LaunchCheckBlocked:
    code = str(getattr(exc, "code", "LAUNCH_LIFECYCLE_FAILED") or "")
    reasons = getattr(exc, "reasons", None)
    if not isinstance(reasons, Sequence) or isinstance(reasons, (str, bytes)):
        reasons = ("Durable lifecycle operation не завершена",)
    return LaunchCheckBlocked(
        code,
        tuple(str(reason) for reason in reasons),
        str(getattr(exc, "check_id", None) or auth_id),
    )


def finalize_launch_authorization(
    authorization_or_auth_id: object,
    *,
    requested_outcome: str,
    now: datetime | None = None,
) -> LaunchAuthorizationFinalization:
    """Завершает любой launch path только по durable provider snapshot.

    ``requested_outcome`` влияет лишь на zero-CREATE результат. Подтверждённые
    ``ad_id`` и unresolved claims всегда сильнее локального exception/status.
    """
    from services import launch_repository

    auth_id = _auth_id(authorization_or_auth_id)
    normalized_request = str(requested_outcome or "").strip().upper()
    if normalized_request not in {
        "COMPLETED",
        "SUCCEEDED",
        "PARTIAL",
        "BLOCKED_RECONCILE",
        "BLOCKED",
        "FAILED",
        "RELEASED",
        "CANCELLED",
        "UNKNOWN",
    }:
        raise ValueError(f"Неподдерживаемый requested_outcome: {requested_outcome}")
    finalized_at = now or datetime.now(timezone.utc)
    try:
        snapshot = launch_repository.reconcile_authorization(auth_id, finalized_at)
        terminal_phases = {
            LaunchLifecycleOutcome.COMPLETED.value,
            LaunchLifecycleOutcome.RELEASED.value,
            LaunchLifecycleOutcome.BLOCKED.value,
            LaunchLifecycleOutcome.BLOCKED_RECONCILE.value,
        }
        if snapshot.phase in terminal_phases:
            target_outcome = LaunchLifecycleOutcome(snapshot.phase)
        elif snapshot.total_ads > 0 and snapshot.created_ads == snapshot.total_ads:
            target_outcome = LaunchLifecycleOutcome.COMPLETED
        elif snapshot.needs_reconcile or snapshot.create_started_ads > 0:
            target_outcome = LaunchLifecycleOutcome.BLOCKED_RECONCILE
        elif snapshot.created_ads > 0:
            target_outcome = LaunchLifecycleOutcome.PARTIAL
        elif normalized_request == "BLOCKED":
            target_outcome = LaunchLifecycleOutcome.BLOCKED
        else:
            target_outcome = LaunchLifecycleOutcome.RELEASED

        if snapshot.phase != target_outcome.value:
            launch_repository.finish_authorization(
                auth_id,
                target_outcome.value,
                finalized_at,
            )
            snapshot = launch_repository.reconcile_authorization(auth_id, finalized_at)
        actual_outcome = LaunchLifecycleOutcome(snapshot.phase)
        return LaunchAuthorizationFinalization(
            auth_id=auth_id,
            outcome=actual_outcome,
            total_ads=snapshot.total_ads,
            created_ads=snapshot.created_ads,
            created_ad_ids=snapshot.created_ad_ids,
            needs_reconcile=snapshot.needs_reconcile,
            consumes_daily_slot=(
                snapshot.created_ads > 0
                or actual_outcome is LaunchLifecycleOutcome.COMPLETED
            ),
        )
    except LaunchCheckBlocked:
        raise
    except Exception as exc:
        raise _translate_repository_error(exc, auth_id) from exc


def reconcile_checked_launch_authorization(
    checked_plan: object,
    *,
    now: datetime | None = None,
) -> LaunchAuthorizationReconciliation:
    """Строит all-target evidence из полного fresh account inventory.

    Blind retry отсутствует: repository ротирует proof только для exact
    missing/unclaimed names, а любой duplicate/drift становится typed block.
    """
    from integrations.facebook import fetch_complete_account_ad_inventory
    from services import launch_repository

    proof = getattr(checked_plan, "authorization", None)
    auth_id = _auth_id(proof)
    raw_targets = getattr(checked_plan, "targets", None)
    expected_by_city = getattr(checked_plan, "expected_names_by_city", None)
    if (
        not isinstance(raw_targets, Sequence)
        or isinstance(raw_targets, (str, bytes))
        or not raw_targets
        or not isinstance(expected_by_city, Mapping)
    ):
        raise LaunchCheckBlocked(
            "AUTHORIZATION_SCOPE_DRIFT",
            ("Checked plan не содержит exact target/name scopes",),
            auth_id,
        )

    inventories: dict[tuple[str, str], list[dict[str, object]]] = {}
    try:
        for target in raw_targets:
            account_kind = str(getattr(target, "account_kind", "") or "").strip()
            account_id = str(getattr(target, "account_id", "") or "").strip()
            account_key = (account_kind, account_id)
            if not all(account_key):
                raise LaunchCheckBlocked(
                    "AUTHORIZATION_SCOPE_DRIFT",
                    ("Target не содержит exact account scope",),
                    auth_id,
                )
            if account_key not in inventories:
                inventory = fetch_complete_account_ad_inventory(*account_key)
                if getattr(inventory, "inventory_complete", None) is not True:
                    raise LaunchCheckBlocked(
                        "LIVE_INVENTORY_INCOMPLETE",
                        ("Facebook inventory не доказан как полный",),
                        auth_id,
                    )
                inventories[account_key] = list(inventory)

        live_targets = []
        for target in raw_targets:
            city = str(getattr(target, "city", "") or "").strip()
            account_kind = str(getattr(target, "account_kind", "") or "").strip()
            account_id = str(getattr(target, "account_id", "") or "").strip()
            adset_id = str(getattr(target, "adset_id", "") or "").strip()
            raw_names = expected_by_city.get(city)
            if (
                not city
                or not adset_id
                or not isinstance(raw_names, Sequence)
                or isinstance(raw_names, (str, bytes))
                or not raw_names
            ):
                raise LaunchCheckBlocked(
                    "AUTHORIZATION_SCOPE_DRIFT",
                    (f"Target города {city or '<empty>'} не содержит exact names",),
                    auth_id,
                )
            names = tuple(str(name).strip() for name in raw_names)
            if any(not name for name in names) or len(names) != len(set(names)):
                raise LaunchCheckBlocked(
                    "AUTHORIZATION_SCOPE_DRIFT",
                    (f"Expected names города {city} не exact/unique",),
                    auth_id,
                )
            inventory = inventories[(account_kind, account_id)]
            exact_matches = {
                name: tuple(
                    sorted(
                        str(row["id"])
                        for row in inventory
                        if str(row.get("adset_id") or "") == adset_id
                        and str(row.get("name") or "") == name
                    )
                )
                for name in names
            }
            live_targets.append(
                launch_repository.TrustedLiveTarget(
                    city=city,
                    account_id=account_id.removeprefix("act_"),
                    adset_id=adset_id,
                    inventory_complete=True,
                    exact_matches=exact_matches,
                )
            )

        reconciled = launch_repository.reconcile_and_rotate_authorization(
            proof,
            tuple(live_targets),
            now or datetime.now(timezone.utc),
        )
        rotated = reconciled.authorization
        return LaunchAuthorizationReconciliation(
            auth_id=auth_id,
            outcome=LaunchLifecycleOutcome(reconciled.phase),
            authorization=(
                ProviderLaunchAuthorization(rotated.auth_id, rotated.secret)
                if rotated is not None
                else None
            ),
            created_ad_ids=reconciled.created_ad_ids,
            missing_names_by_city=dict(reconciled.missing_names_by_city),
        )
    except LaunchCheckBlocked:
        raise
    except Exception as exc:
        raise _translate_repository_error(exc, auth_id) from exc


def _copy_media(raw_media: object, *, as_carousel: bool) -> dict[str, Any]:
    """Копирует media payload и применяет тот же carousel contract, что launcher."""
    if not isinstance(raw_media, Mapping):
        raise ValueError("Drive вернул некорректный media payload")
    media = dict(raw_media)
    raw_paths = media.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("Drive не вернул media files")
    media["paths"] = [dict(item) if isinstance(item, Mapping) else item for item in raw_paths]
    raw_singles = media.get("singles")
    if raw_singles is not None:
        if not isinstance(raw_singles, list):
            raise ValueError("Drive вернул некорректные одиночные assets")
        media["singles"] = [
            dict(item) if isinstance(item, Mapping) else item for item in raw_singles
        ]
    if as_carousel and media.get("type") == "image" and len(media["paths"]) >= 2:
        media["paths"] = media["paths"][:10]
        media["type"] = "carousel"
    return media


def _prepare_media(
    card: Mapping[str, Any], request: LaunchCheckRequest
) -> PreparedLaunchMedia:
    from integrations.facebook import calculate_launch_media_sha256
    from integrations.gdrive import download_media
    from integrations.trello import get_card_drive_link

    card_id = str(card.get("id") or "").strip()
    drive_url = get_card_drive_link(card_id)
    if not drive_url:
        raise ValueError("Карточка не содержит Drive ссылку")
    media = _copy_media(download_media(drive_url), as_carousel=request.as_carousel)
    return PreparedLaunchMedia(
        media=media,
        media_sha256=calculate_launch_media_sha256(media),
    )


def _resolve_language(card: Mapping[str, Any]) -> str:
    from integrations.trello import detect_language

    return detect_language(str(card.get("name") or ""), str(card.get("desc") or ""))


def _resolve_product(card: Mapping[str, Any]) -> str:
    from services.product_tags import classify_product

    labels = card.get("labels")
    normalized_labels = labels if isinstance(labels, list) else []
    return classify_product(
        str(card.get("name") or ""),
        str(card.get("desc") or ""),
        normalized_labels,
    )


def _resolve_targets(
    card: Mapping[str, Any],
    request: LaunchCheckRequest,
    language: str,
    product: str,
    prepared: PreparedLaunchMedia,
) -> tuple[LaunchTarget, ...]:
    from integrations import facebook
    from services.fb_token_provider import fb_account, get_fb_account_id

    account_kind = (
        "online" if request.campaign_type in {"mql_online", "prodb_online"} else "offline"
    )
    account_context = fb_account("online") if account_kind == "online" else nullcontext()
    selected_cities = list(request.cities) if request.cities is not None else None
    with account_context:
        canonical_media = facebook._prepare_launch_media(dict(prepared.media))
        adsets = facebook._resolve_launch_adsets(
            request.campaign_type,
            language,
            selected_cities,
        )
        if account_kind == "online":
            # Онлайн — единый кабинет активного контекста, как раньше.
            online_account = str(get_fb_account_id()).removeprefix("act_").strip()
            if not online_account:
                raise ValueError("Не удалось определить exact Facebook account")
            accounts_by_city = {city: online_account for city, _adset_id in adsets}
            launch_route_type = None
        else:
            # Оффлайн — кабинет каждой ПАРЫ (город, тип) из discovery, который
            # уже отфильтрован картой маршрутизации (services/launch_routing.py):
            # L2 расщеплённых городов живёт в «ACME cabinet_b», их L1 и
            # MQL — в cabinet_a, CityF целиком в cabinet_b. Пара без кабинета
            # ниже даёт отказ, а не молчаливый дефолтный кабинет.
            from agent.adset_discovery import get_adset_accounts_dict
            from services.launch_routing import route_type

            # website-кампания ходит в MQL-инвентарь, даже если карточка на
            # втором языке (L2) — язык карточки не равен типу адсета.
            launch_route_type = route_type(request.campaign_type, language)
            accounts_by_city = {
                str(city): {
                    str(adset_type): str(account or "").removeprefix("act_").strip()
                    for adset_type, account in (types or {}).items()
                }
                for city, types in get_adset_accounts_dict().items()
            }
        targets = []
        for ordinal, (city, adset_id) in enumerate(adsets):
            if launch_route_type is None:
                account_id = accounts_by_city.get(city, "")
            else:
                account_id = (accounts_by_city.get(city) or {}).get(
                    launch_route_type, ""
                )
            if not account_id:
                # fail-closed: пара не маршрутизирована ни в один кабинет.
                raise ValueError(
                    f"Город {city} не привязан к FB-кабинету запуска "
                    "(карта маршрутизации (город, тип)→кабинет)"
                )
            if launch_route_type is not None:
                # Сверка двух независимых источников: живой инвентарь против
                # карты. Расхождение = отказ, а не запуск в спящий адсет.
                from services.launch_routing import (
                    LaunchRoutingError,
                    resolve_account,
                )

                try:
                    routed_account = resolve_account(city, launch_route_type)
                except LaunchRoutingError as exc:
                    raise ValueError(
                        f"Город {city} не привязан к FB-кабинету запуска "
                        f"({launch_route_type} вне карты маршрутизации)"
                    ) from exc
                if routed_account != account_id:
                    raise ValueError(
                        f"Кабинет {city}/{launch_route_type} расходится: "
                        f"инвентарь act_{account_id}, карта act_{routed_account}"
                    )
            targets.append(
                LaunchTarget(
                    city=city,
                    ordinal=ordinal,
                    account_kind=account_kind,
                    account_id=account_id,
                    adset_id=adset_id,
                    expected_names=facebook._planned_city_names(
                        city,
                        str(card.get("name") or ""),
                        product,
                        canonical_media,
                    ),
                )
            )
        return tuple(targets)


def _read_inventory(target: LaunchTarget) -> LiveAdsetInventory:
    from integrations.facebook import MAX_ADS_PER_ADSET, get_adset_info
    from services import launch_repository
    from services.adset_cleaner import _other_reserved_slots
    from services.fb_token_provider import fb_account, get_fb_account_id

    account_context = fb_account("online") if target.account_kind == "online" else nullcontext()
    with account_context:
        if target.account_kind == "online":
            # Онлайн — токен и кабинет берутся из контекста, они обязаны
            # совпадать с target (иначе inventory читался бы чужим токеном).
            account_id = str(get_fb_account_id()).removeprefix("act_").strip()
            if account_id != target.account_id:
                raise RuntimeError("Facebook account изменился до inventory check")
        # Оффлайн-цели живут в РАЗНЫХ кабинетах карты маршрутизации
        # (cabinet_a и cabinet_b), но читаются одним offline FB_TOKEN —
        # сверка с process-default кабинетом здесь ложно резала любые
        # цели вне cabinet_a (INVENTORY_UNVERIFIED по CityF).
        snapshot = get_adset_info(target.adset_id)

    # Живое доказательство маршрутизации: адсет обязан принадлежать кабинету
    # ЦЕЛИ своего города. Иначе план (город, кабинет, адсет) внутренне
    # противоречив — отказ, а не запуск «куда получится».
    live_account = str(snapshot.get("account_id") or "").removeprefix("act_").strip()
    if live_account != target.account_id:
        raise RuntimeError(
            f"Адсет {target.adset_id} города {target.city} живёт в кабинете "
            f"act_{live_account or '<unknown>'}, а не в кабинете цели "
            f"act_{target.account_id}"
        )

    raw_ads = snapshot.get("ads")
    if not isinstance(raw_ads, list):
        raise RuntimeError("Facebook вернул неполный список объявлений")
    ads = tuple(
        LiveAd(ad_id=str(ad.get("id") or ""), name=str(ad.get("name") or ""))
        for ad in raw_ads
        if isinstance(ad, Mapping)
    )
    ad_count = snapshot.get("ad_count")
    if type(ad_count) is not int:
        raise RuntimeError("Facebook вернул некорректный ad_count")

    # Оба durable reservation-контура уменьшают доступную ёмкость до reserve.
    other_reserved = _other_reserved_slots("", target.adset_id)
    other_reserved += launch_repository.get_reserved_slots(
        target.adset_id,
        datetime.now(timezone.utc),
    )
    max_ads = MAX_ADS_PER_ADSET
    return LiveAdsetInventory(
        adset_id=str(snapshot.get("adset_id") or ""),
        effective_status=str(snapshot.get("adset_effective_status") or ""),
        inventory_complete=snapshot.get("inventory_complete") is True,
        ads=ads,
        ad_count=ad_count,
        max_ads=max_ads,
        available=max_ads - ad_count,
        other_reserved_slots=other_reserved,
    )


def _adset_lock(adset_id: str):
    from services.adset_pause_guard import adset_mutation_lock

    return adset_mutation_lock(adset_id)


def build_production_launch_checker(
    mode: CheckerMode | str,
    *,
    prepared_media_by_card: Mapping[str, PreparedLaunchMedia] | None = None,
) -> LaunchChecker:
    """Возвращает единый production checker для cron, web, CLI и recovery.

    ``prepared_media_by_card`` позволяет active-пути повторно проверить fresh
    inventory и зарезервировать уже проверенные exact bytes без второго Drive
    download. Кэш принадлежит вызывающему коду и checker его не мутирует.
    """
    from services import launch_repository

    def prepare_media(
        card: Mapping[str, Any], request: LaunchCheckRequest
    ) -> PreparedLaunchMedia:
        card_id = str(card.get("id") or "").strip()
        cached = (
            prepared_media_by_card.get(card_id)
            if prepared_media_by_card is not None
            else None
        )
        if cached is not None:
            if not isinstance(cached, PreparedLaunchMedia):
                raise TypeError("prepared media cache содержит неверный тип")
            return cached
        return _prepare_media(card, request)

    return LaunchChecker(
        mode=CheckerMode(mode),
        repository=launch_repository,
        prepare_media=prepare_media,
        resolve_language=_resolve_language,
        resolve_product=_resolve_product,
        resolve_targets=_resolve_targets,
        read_inventory=_read_inventory,
        adset_lock=_adset_lock,
    )


def cleanup_prepared_media(media: Mapping[str, Any]) -> None:
    """Удаляет только exact локальные файлы, созданные Drive downloader-ом."""
    paths: list[str] = []
    if media.get("type") == "placement_pairs":
        raw_pairs = media.get("paths")
        if isinstance(raw_pairs, list):
            for pair in raw_pairs:
                if isinstance(pair, Mapping):
                    paths.extend(
                        str(pair[key])
                        for key in ("feed", "story")
                        if isinstance(pair.get(key), str)
                    )
        raw_singles = media.get("singles")
        if isinstance(raw_singles, list):
            paths.extend(
                str(single["path"])
                for single in raw_singles
                if isinstance(single, Mapping) and isinstance(single.get("path"), str)
            )
    else:
        raw_paths = media.get("paths")
        if isinstance(raw_paths, list):
            paths.extend(str(path) for path in raw_paths if isinstance(path, str))
    for path in dict.fromkeys(paths):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            # Cleanup не должен менять решение checker-а или результат CREATE.
            logger.warning(
                "Не удалось удалить временный launch media %s: %s",
                os.path.basename(path),
                type(exc).__name__,
            )
    _cleanup_download_directories(paths)


# Каталог, который создаёт download_media через mkdtemp под медиа Drive-папки.
_DOWNLOAD_DIR_PREFIX = "acme_"


def _cleanup_download_directories(paths: list[str]) -> None:
    """Убирает опустевший каталог загрузки: файлы удалены, а mkdtemp остаётся.

    Без этого /tmp копит `acme_*` со всеми скачанными байтами — за один
    залповый прогон это гигабайты, которые не отдаются до перезагрузки.
    """

    tmp_root = os.path.realpath(tempfile.gettempdir())
    for directory in dict.fromkeys(os.path.dirname(path) for path in paths):
        if not directory:
            continue
        try:
            # Удаляем только собственный mkdtemp: имя acme_*, родитель — /tmp.
            if not os.path.basename(directory).startswith(_DOWNLOAD_DIR_PREFIX):
                continue
            if os.path.realpath(os.path.dirname(directory)) != tmp_root:
                continue
            if os.path.islink(directory) or not os.path.isdir(directory):
                continue
            shutil.rmtree(directory)
        except OSError as exc:
            logger.warning(
                "Не удалось удалить каталог загрузки %s: %s",
                os.path.basename(directory),
                type(exc).__name__,
            )


__all__ = [
    "LaunchAuthorizationFinalization",
    "LaunchAuthorizationReconciliation",
    "LaunchLifecycleOutcome",
    "build_production_launch_checker",
    "cleanup_prepared_media",
    "finalize_launch_authorization",
    "reconcile_checked_launch_authorization",
]
