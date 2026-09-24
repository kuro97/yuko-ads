"""Crash-safe immutable staging для проверяемого запуска рекламы."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, ContextManager, Mapping

from config import (
    AD_BODY,
    AD_BODY_PRODB,
    AD_TITLE,
    FB_ACCOUNT_ID_ONLINE,
    FB_IG_ACME_PRODB,
    FB_IG_ONLINE,
    FB_PAGE_ID,
    LEAD_FORMS,
    REPORT_CHECKER_STAGING_DIR_MODE,
    REPORT_CHECKER_STAGING_FILE_MODE,
    REPORT_CHECKER_STAGING_ROOT,
    TRELLO_BOARD_ID,
    WEBSITE_LANDING_URL,
)
from integrations.trello import (
    TrelloCardSnapshot,
    detect_language,
    get_card_snapshot,
    get_done_list_id,
)
from services.approval_checker_models import (
    ActionResult,
    CreativeSpec,
    LaunchDestination,
    LaunchSourceInput,
    MediaAssetSpec,
    MediaType,
    PlacementRole,
    PreparedLaunch,
    TrelloPrecondition,
    canonical_json,
)
from services import media_cache
from services.approval_source_media import normalize_ad_text, rehash_staged_files
from services.approval_source_trello import _attachment_sha256, _labels_sha256
from services.product_tags import classify_product


_MANIFEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Брошенный temp-каталог stage_launch: tempfile.mkdtemp(prefix=f".{manifest_id}.").
_TEMP_DIRECTORY_RE = re.compile(r"^\.[A-Za-z0-9_-]{1,128}\.[A-Za-z0-9_]{1,32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COPY_CHUNK_BYTES = 1024 * 1024
_COMPLETE_MARKER = "manifest.complete"


class LaunchStagingError(RuntimeError):
    """Launch input нельзя безопасно зафиксировать либо повторно доказать."""


@dataclass(frozen=True, slots=True)
class _ResolvedLaunchSource:
    snapshot: TrelloCardSnapshot
    media: Mapping[str, Any]
    attachment_id: str
    body: str
    adset_type: str
    product: str


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _safe_root(root: Path) -> Path:
    try:
        info = root.lstat()
    except FileNotFoundError:
        root.mkdir(mode=REPORT_CHECKER_STAGING_DIR_MODE, parents=True, exist_ok=False)
        info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LaunchStagingError("STAGING_ROOT_UNSAFE")
    os.chmod(root, REPORT_CHECKER_STAGING_DIR_MODE)
    resolved = root.resolve(strict=True)
    if resolved != root.resolve():
        raise LaunchStagingError("STAGING_ROOT_UNSAFE")
    return resolved


def _safe_manifest_id(manifest_id: str) -> str:
    if not isinstance(manifest_id, str) or _MANIFEST_ID_RE.fullmatch(manifest_id) is None:
        raise ValueError("manifest_id должен быть безопасным идентификатором")
    return manifest_id


def _drive_attachment(snapshot: TrelloCardSnapshot) -> dict[str, Any]:
    matches = [
        attachment
        for attachment in snapshot.attachments
        if "drive.google.com" in str(attachment.get("url") or "").lower()
    ]
    if len(matches) != 1:
        raise LaunchStagingError("TRELLO_DRIVE_ATTACHMENT_NOT_EXACT")
    return matches[0]


def _resolve_launch_source(source: LaunchSourceInput) -> _ResolvedLaunchSource:
    """Собирает live Trello и actual Drive bytes без provider mutation."""

    if not isinstance(source, LaunchSourceInput):
        raise TypeError("source должен быть LaunchSourceInput")
    snapshot = get_card_snapshot(source.card_id)
    ready_list_id = get_done_list_id()
    if (
        snapshot.board_id != TRELLO_BOARD_ID
        or snapshot.list_id != ready_list_id
        or (snapshot.due_complete and not source.allow_checked)
        or snapshot.closed
    ):
        raise LaunchStagingError("TRELLO_CARD_NOT_READY")
    attachment = _drive_attachment(snapshot)
    from integrations.gdrive import download_media

    media = download_media(str(attachment["url"]))
    if source.as_carousel and media.get("type") == "image":
        paths = media.get("paths")
        if isinstance(paths, list) and len(paths) >= 2:
            media = dict(media)
            media["paths"] = paths[:10]
            media["type"] = "carousel"
    adset_type = detect_language(snapshot.name, snapshot.description)
    body = (
        AD_BODY_PRODB[adset_type]
        if source.campaign_type in {"leadgen_prodb", "prodb_online"}
        else AD_BODY[adset_type]
    )
    labels = tuple(
        str(label.get("name") or label.get("color") or "")
        for label in snapshot.labels
    )
    product = classify_product(snapshot.name, snapshot.description, list(labels))
    return _ResolvedLaunchSource(
        snapshot=snapshot,
        media=media,
        attachment_id=str(attachment["id"]),
        body=normalize_ad_text(body),
        adset_type=adset_type,
        product=product,
    )


def _source_bindings(media: Mapping[str, Any]) -> tuple[tuple[str, str, MediaType, str | None, PlacementRole], ...]:
    media_type = media.get("type")
    paths = media.get("paths")
    if not isinstance(paths, list) or not paths:
        raise LaunchStagingError("MEDIA_PATHS_INVALID")
    bindings: list[tuple[str, str, MediaType, str | None, PlacementRole]] = []
    if media_type == "placement_pairs":
        for pair in sorted(paths, key=lambda item: str(item.get("label")) if isinstance(item, dict) else ""):
            if not isinstance(pair, dict):
                raise LaunchStagingError("MEDIA_PAIR_INVALID")
            label = str(pair.get("label") or "").strip()
            feed = pair.get("feed")
            story = pair.get("story")
            if not label or not isinstance(feed, str) or not isinstance(story, str):
                raise LaunchStagingError("MEDIA_PAIR_INVALID")
            bindings.extend(
                (
                    (f"{label}.feed", feed, MediaType.PLACEMENT_PAIR, label, PlacementRole.FEED),
                    (f"{label}.story", story, MediaType.PLACEMENT_PAIR, label, PlacementRole.STORY),
                )
            )
        singles = media.get("singles", [])
        if not isinstance(singles, list):
            raise LaunchStagingError("MEDIA_SINGLES_INVALID")
        for single in sorted(
            singles,
            key=lambda item: str(item.get("label")) if isinstance(item, dict) else "",
        ):
            if not isinstance(single, dict):
                raise LaunchStagingError("MEDIA_SINGLE_INVALID")
            label = str(single.get("label") or "").strip()
            path = single.get("path")
            if not label or not isinstance(path, str):
                raise LaunchStagingError("MEDIA_SINGLE_INVALID")
            bindings.append(
                (f"{label}.single", path, MediaType.IMAGE, None, PlacementRole.DEFAULT)
            )
    else:
        type_map = {
            "video": MediaType.VIDEO,
            "image": MediaType.IMAGE,
            "carousel": MediaType.CAROUSEL_ITEM,
        }
        resolved_type = type_map.get(str(media_type))
        if resolved_type is None or any(not isinstance(path, str) for path in paths):
            raise LaunchStagingError("MEDIA_TYPE_INVALID")
        ordered = sorted(paths, key=lambda path: Path(path).name)
        if resolved_type is MediaType.CAROUSEL_ITEM:
            ordered = ordered[:10]
        bindings.extend(
            (Path(path).stem, path, resolved_type, None, PlacementRole.DEFAULT)
            for path in ordered
        )
    identities = [binding[0] for binding in bindings]
    if not identities or len(identities) != len(set(identities)):
        raise LaunchStagingError("MEDIA_ORDER_INVALID")
    return tuple(bindings)


def _copy_regular_file(source_path: str, destination: Path) -> tuple[int, str]:
    """Кладёт байты источника в staging: reflink из общего кэша либо копия.

    Кэш (`services/media_cache.py`) держит один экземпляр содержимого на все
    заявки, staging получает CoW-копию с `st_nlink == 1` — контракт
    immutable staging не меняется. Если ФС не умеет reflink, работает
    прежнее потоковое копирование.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(source_path, flags)
    except OSError as exc:
        raise LaunchStagingError("MEDIA_SOURCE_UNSAFE") from exc
    try:
        source_before = os.fstat(source_fd)
        if not stat.S_ISREG(source_before.st_mode) or source_before.st_nlink != 1:
            raise LaunchStagingError("MEDIA_SOURCE_UNSAFE")
        total, digest = _digest_source(source_fd, source_before)
        cached = media_cache.store(source_fd, digest, total)
        _assert_source_stable(source_fd, source_before, total)
        if cached is not None and media_cache.clone_into(
            cached, destination, file_mode=REPORT_CHECKER_STAGING_FILE_MODE
        ):
            return total, digest
        _copy_source_bytes(source_fd, destination, expected_size=total, expected_digest=digest)
        return total, digest
    finally:
        os.close(source_fd)


def _digest_source(source_fd: int, source_before: os.stat_result) -> tuple[int, str]:
    """Считает размер и sha256 источника одним проходом, проверяя неизменность."""

    digest = hashlib.sha256()
    total = 0
    os.lseek(source_fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    if total <= 0:
        raise LaunchStagingError("MEDIA_SOURCE_EMPTY")
    _assert_source_stable(source_fd, source_before, total)
    return total, digest.hexdigest()


def _assert_source_stable(
    source_fd: int, source_before: os.stat_result, total: int
) -> None:
    """Источник не должен подмениться между чтением, кэшем и материализацией."""

    source_after = os.fstat(source_fd)
    if (
        source_before.st_dev,
        source_before.st_ino,
        source_before.st_size,
        source_before.st_mtime_ns,
    ) != (
        source_after.st_dev,
        source_after.st_ino,
        source_after.st_size,
        source_after.st_mtime_ns,
    ) or total != source_after.st_size:
        raise LaunchStagingError("MEDIA_SOURCE_RACE")


def _copy_source_bytes(
    source_fd: int,
    destination: Path,
    *,
    expected_size: int,
    expected_digest: str,
) -> None:
    """Фолбэк без кэша: потоковое копирование с проверкой байт на выходе."""

    destination_fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        REPORT_CHECKER_STAGING_FILE_MODE,
    )
    digest = hashlib.sha256()
    total = 0
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            _write_all(destination_fd, chunk)
            digest.update(chunk)
            total += len(chunk)
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)
    if total != expected_size or digest.hexdigest() != expected_digest:
        raise LaunchStagingError("MEDIA_SOURCE_RACE")


def _write_file(path: Path, content: bytes) -> str:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        REPORT_CHECKER_STAGING_FILE_MODE,
    )
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return hashlib.sha256(content).hexdigest()


def _write_all(descriptor: int, content: bytes) -> None:
    """Пишет все bytes; короткая системная запись не считается успехом."""

    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise LaunchStagingError("STAGING_SHORT_WRITE")
        offset += written


def _account_context(campaign_type: str) -> ContextManager[Any]:
    if campaign_type in {"mql_online", "prodb_online"}:
        from services.fb_token_provider import fb_account

        return fb_account("online")
    return nullcontext()


def _config_sha256() -> str:
    from config import ADSETS, ADSETS_MQL, PRODB_ONLINE_ADSETS
    from services.launch_routing import routing_fingerprint

    return _sha256(
        {
            "adsets": ADSETS,
            "adsets_mql": ADSETS_MQL,
            "prodb_online_adsets": PRODB_ONLINE_ADSETS,
            # Карта роутинга — часть конфигурации запуска: манифест,
            # застейдженный до её правки, обязан перестать сходиться
            # (LAUNCH_STAGING_DRIFT), а не исполниться в кабинет, из которого
            # инвентарь уже уехал.
            "routing": routing_fingerprint(),
            "page_id": FB_PAGE_ID,
            "lead_forms": LEAD_FORMS,
            "title": AD_TITLE,
            "ig_prodb": FB_IG_ACME_PRODB,
            "ig_online": FB_IG_ONLINE,
            "landing": WEBSITE_LANDING_URL,
        }
    )


def _creative_asset_groups(
    assets: tuple[MediaAssetSpec, ...],
) -> tuple[tuple[str, ...], ...]:
    if all(asset.media_type is MediaType.CAROUSEL_ITEM for asset in assets):
        return (tuple(asset.asset_id for asset in assets),)
    groups: list[tuple[str, ...]] = []
    seen_pairs: set[str] = set()
    for asset in assets:
        group_id = asset.placement_group_id
        if group_id is not None:
            if group_id in seen_pairs:
                continue
            pair = tuple(
                item.asset_id
                for item in assets
                if item.placement_group_id == group_id
            )
            if len(pair) != 2:
                raise LaunchStagingError("MEDIA_PLACEMENT_PAIR_INCOMPLETE")
            seen_pairs.add(group_id)
            groups.append(pair)
        else:
            groups.append((asset.asset_id,))
    return tuple(groups)


def _creative_contract(
    campaign_type: str,
    adset_type: str,
    adset_id: str,
) -> tuple[str, str | None, str, str, str | None]:
    if campaign_type == "website":
        return FB_PAGE_ID, None, "LEARN_MORE", "NONE", WEBSITE_LANDING_URL
    if campaign_type in {"mql_online", "prodb_online"}:
        from integrations.facebook import get_adset_creative_template

        template = get_adset_creative_template(adset_id)
        if not isinstance(template, dict):
            raise LaunchStagingError("FB_TEMPLATE_UNAVAILABLE")
        page_id = str(template.get("page_id") or "")
        form_id = template.get("lead_form_id")
        cta = str(template.get("cta_type") or "")
        link_url = template.get("link")
        if not page_id or not cta or (form_id is not None and not isinstance(form_id, str)):
            raise LaunchStagingError("FB_TEMPLATE_INCOMPLETE")
        return page_id, form_id, cta, FB_IG_ONLINE or "NONE", link_url
    form = LEAD_FORMS[adset_type]
    instagram = FB_IG_ACME_PRODB if campaign_type == "leadgen_prodb" else "NONE"
    return FB_PAGE_ID, str(form["form_id"]), str(form["cta"]), instagram or "NONE", None


def _group_link_url(
    campaign_type: str,
    base_link_url: str | None,
    group_assets: tuple[MediaAssetSpec, ...],
) -> str | None:
    """Ссылка креатива группы: статикам lead gen — внешняя посадочная.

    Image-креатив (link_data) обязан нести link, а контракт leadgen ссылки не
    даёт — запуск статик падал INVALID_LAUNCH_MANIFEST. Ссылка обязана быть
    ВНЕШНЕЙ: facebook.com-ссылку FB отвергает с subcode 1815316 — как в
    create_image_ad (integrations/facebook.py). Видео не трогаем: их CTA
    исторически несёт только lead_gen_form_id.
    """
    if base_link_url is not None:
        return base_link_url
    if campaign_type not in {"leadgen", "leadgen_prodb"}:
        return None
    if all(asset.media_type is MediaType.VIDEO for asset in group_assets):
        return None
    return "https://example.com"


def _destinations(
    source: LaunchSourceInput,
    resolved: _ResolvedLaunchSource,
    media_assets: tuple[MediaAssetSpec, ...],
    body_path: str,
    body_sha256: str,
) -> tuple[LaunchDestination, ...]:
    from integrations import facebook

    with _account_context(source.campaign_type):
        adsets = facebook._resolve_launch_adsets(  # noqa: SLF001 - общий exact resolver
            source.campaign_type,
            resolved.adset_type,
            list(source.requested_cities) or None,
        )
        prepared_media = facebook._prepare_launch_media(dict(resolved.media))  # noqa: SLF001
        groups = _creative_asset_groups(media_assets)
        assets_by_id = {asset.asset_id: asset for asset in media_assets}
        if source.campaign_type in {"mql_online", "prodb_online"}:
            # Онлайн-кампании — единый онлайн-кабинет, как раньше.
            online_account = str(FB_ACCOUNT_ID_ONLINE).removeprefix("act_")
            accounts_by_city = None
            launch_route_type = None
        else:
            # Оффлайн: кабинет берётся из discovery, отфильтрованного картой
            # маршрутизации (services/launch_routing.py), а НЕ из константы
            # config.FB_ACCOUNT_ID. Решает пара (город, тип):
            # L2 расщеплённых городов живёт в «ACME cabinet_b», их L1 и MQL — в
            # cabinet_a, CityF целиком в cabinet_b.
            from agent.adset_discovery import get_adset_accounts_dict
            from services.launch_routing import route_type

            online_account = None
            # Язык карточки (L2/L1) — не то же самое, что тип адсета: у
            # website-кампании карточка тоже бывает на L2, а инвентарь там
            # MQL. Спрашивать карту про язык значило бы спросить не про тот
            # инвентарь.
            launch_route_type = route_type(source.campaign_type, resolved.adset_type)
            accounts_by_city = {
                str(city): {
                    str(adset_type): str(account or "").removeprefix("act_").strip()
                    for adset_type, account in (types or {}).items()
                }
                for city, types in get_adset_accounts_dict().items()
            }
        result: list[LaunchDestination] = []
        for city, adset_id in adsets:
            if accounts_by_city is None:
                account_id = online_account or ""
            else:
                account_id = (accounts_by_city.get(city) or {}).get(
                    launch_route_type or "", ""
                )
            if not account_id:
                # fail-closed: пара без кабинета в карте маршрутизации не
                # уходит молчаливо в дефолтный кабинет — запуск запрещён.
                raise LaunchStagingError("CITY_ACCOUNT_UNROUTED")
            if accounts_by_city is not None:
                # Кабинет обязан совпасть в ДВУХ независимых источниках: живом
                # инвентаре (discovery) и карте роутинга. Иначе устаревший кеш
                # или статический fallback дают внутренне согласованную, но
                # неверную пару — креатив уходит в спящий адсет молча.
                from services.launch_routing import (
                    LaunchRoutingError,
                    resolve_account,
                )

                try:
                    routed_account = resolve_account(city, launch_route_type or "")
                except LaunchRoutingError as exc:
                    raise LaunchStagingError("CITY_ACCOUNT_UNROUTED") from exc
                if routed_account != account_id:
                    raise LaunchStagingError("CITY_ACCOUNT_ROUTE_MISMATCH")
            capacity = facebook.get_adset_capacity(adset_id)
            try:
                daily_budget = Decimal(str(capacity["daily_budget"]))
                available = int(capacity["available"])
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise LaunchStagingError("FB_CAPACITY_INCOMPLETE") from exc
            if not daily_budget.is_finite() or daily_budget <= 0 or available < 0:
                raise LaunchStagingError("FB_CAPACITY_INCOMPLETE")
            names = facebook._planned_city_names(  # noqa: SLF001
                city,
                resolved.snapshot.name,
                resolved.product,
                prepared_media,
            )
            if len(names) != len(groups):
                raise LaunchStagingError("CREATIVE_ORDER_MISMATCH")
            page_id, form_id, cta, instagram, link_url = _creative_contract(
                source.campaign_type,
                resolved.adset_type,
                adset_id,
            )
            creatives = tuple(
                CreativeSpec(
                    creative_id=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"acme:{source.card_id}:{adset_id}:{order}:{name}",
                        )
                    ),
                    order_index=order,
                    ad_name=name,
                    media_asset_ids=asset_ids,
                    body_staged_relative_path=body_path,
                    body_sha256=body_sha256,
                    product=resolved.product,
                    page_id=page_id,
                    lead_form_id=form_id,
                    call_to_action=cta,
                    instagram_actor_id=instagram,
                    title=AD_TITLE,
                    link_url=_group_link_url(
                        source.campaign_type,
                        link_url,
                        tuple(assets_by_id[asset_id] for asset_id in asset_ids),
                    ),
                    expected_configured_status="ACTIVE",
                )
                for order, (name, asset_ids) in enumerate(zip(names, groups, strict=True))
            )
            signature = _sha256(
                {
                    "account_id": account_id,
                    "adset_id": adset_id,
                    "city": city,
                    "creatives": creatives,
                }
            )
            result.append(
                LaunchDestination(
                    city=city,
                    account_id=account_id,
                    adset_id=adset_id,
                    adset_type=resolved.adset_type,
                    current_daily_budget=daily_budget,
                    currency="USD",
                    capacity_available=available,
                    hard_reserve_slots=1,
                    creatives=creatives,
                    duplicate_signature=signature,
                )
            )
    return tuple(result)


def _marker_payload(prepared: PreparedLaunch) -> dict[str, object]:
    return {
        "schema_version": 1,
        "manifest_id": prepared.manifest_id,
        "trello": prepared.trello,
        "card_name_sha256": prepared.card_name_sha256,
        "campaign_type": prepared.campaign_type,
        "config_version_sha256": prepared.config_version_sha256,
        "media_assets": prepared.media_assets,
        "destinations": prepared.destinations,
        "media_manifest_sha256": prepared.media_manifest_sha256,
    }


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def stage_launch(source: LaunchSourceInput, *, manifest_id: str) -> PreparedLaunch:
    """Копирует actual bytes/text в stable root и публикует их атомарно."""

    safe_id = _safe_manifest_id(manifest_id)
    root = _safe_root(Path(REPORT_CHECKER_STAGING_ROOT))
    final_directory = root / safe_id
    if final_directory.exists() or final_directory.is_symlink():
        raise LaunchStagingError("STAGING_ALREADY_EXISTS")
    resolved = _resolve_launch_source(source)
    temporary = Path(tempfile.mkdtemp(prefix=f".{safe_id}.", dir=root))
    os.chmod(temporary, REPORT_CHECKER_STAGING_DIR_MODE)
    try:
        media_directory = temporary / "media"
        text_directory = temporary / "text"
        media_directory.mkdir(mode=REPORT_CHECKER_STAGING_DIR_MODE)
        text_directory.mkdir(mode=REPORT_CHECKER_STAGING_DIR_MODE)
        media_assets: list[MediaAssetSpec] = []
        for order, (identity, raw_path, media_type, group_id, role) in enumerate(
            _source_bindings(resolved.media)
        ):
            suffix = Path(raw_path).suffix.lower() or ".bin"
            relative = Path("media") / f"{order:03d}-{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex}{suffix}"
            size_bytes, content_sha256 = _copy_regular_file(
                raw_path,
                temporary / relative,
            )
            media_assets.append(
                MediaAssetSpec(
                    asset_id=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"acme:{safe_id}:{order}:{identity}:{role.value}",
                        )
                    ),
                    order_index=order,
                    media_type=media_type,
                    placement_group_id=group_id,
                    placement_role=role,
                    staged_relative_path=relative.as_posix(),
                    original_attachment_id=resolved.attachment_id,
                    mime_type=mimetypes.guess_type(raw_path)[0]
                    or "application/octet-stream",
                    size_bytes=size_bytes,
                    content_sha256=content_sha256,
                )
            )
        body_relative = Path("text") / "body.txt"
        body_bytes = normalize_ad_text(resolved.body).encode("utf-8")
        if not body_bytes:
            raise LaunchStagingError("AD_BODY_EMPTY")
        body_sha256 = _write_file(temporary / body_relative, body_bytes)
        assets = tuple(media_assets)
        media_manifest_sha256 = _sha256(
            {
                "assets": assets,
                "body_relative_path": body_relative.as_posix(),
                "body_sha256": body_sha256,
            }
        )
        destinations = _destinations(
            source,
            resolved,
            assets,
            body_relative.as_posix(),
            body_sha256,
        )
        snapshot = resolved.snapshot
        attachment_ids = tuple(str(item["id"]) for item in snapshot.attachments)
        trello = TrelloPrecondition(
            card_id=snapshot.card_id,
            board_id=snapshot.board_id,
            ready_list_id=snapshot.list_id,
            expected_list_id=snapshot.list_id,
            expected_due_complete=False,
            expected_closed=False,
            date_last_activity=snapshot.date_last_activity,
            attachment_ids=attachment_ids,
            attachment_manifest_sha256=_attachment_sha256(snapshot),
            labels_sha256=_labels_sha256(snapshot),
            card_content_sha256=snapshot.content_sha256,
        )
        prepared = PreparedLaunch(
            manifest_id=safe_id,
            staged_at=datetime.now(timezone.utc),
            staging_root=root,
            staging_directory=final_directory,
            trello=trello,
            card_name=resolved.snapshot.name,
            card_name_sha256=hashlib.sha256(
                resolved.snapshot.name.encode("utf-8")
            ).hexdigest(),
            campaign_type=source.campaign_type,
            config_version_sha256=_config_sha256(),
            media_assets=assets,
            destinations=destinations,
            media_manifest_sha256=media_manifest_sha256,
        )
        _fsync_directory(media_directory)
        _fsync_directory(text_directory)
        _fsync_directory(temporary)
        _write_file(temporary / _COMPLETE_MARKER, canonical_json(_marker_payload(prepared)))
        _fsync_directory(temporary)
        if final_directory.exists() or final_directory.is_symlink():
            raise LaunchStagingError("STAGING_ALREADY_EXISTS")
        os.replace(temporary, final_directory)
        _fsync_directory(root)
        return prepared
    except Exception:
        if temporary.exists() and temporary.parent == root:
            shutil.rmtree(temporary)
        raise


def refresh_staged_destinations(
    prepared: PreparedLaunch,
    destinations: tuple[LaunchDestination, ...],
) -> PreparedLaunch:
    """Атомарно публикует fresh capacity/replacement binding до manifest.

    Функция не меняет media/text и допускает ровно один
    server-owned refresh уже опубликованного staging перед gateway.
    """

    if not isinstance(prepared, PreparedLaunch):
        raise TypeError("prepared должен быть PreparedLaunch")
    if not destinations or tuple(item.city for item in destinations) != tuple(
        item.city for item in prepared.destinations
    ):
        raise LaunchStagingError("DESTINATION_REFRESH_SCOPE_DRIFT")
    directory = prepared.staging_directory.resolve(strict=True)
    if directory != prepared.staging_directory or directory.is_symlink():
        raise LaunchStagingError("STAGING_ROOT_UNSAFE")
    refreshed = replace(prepared, destinations=destinations)
    marker = directory / _COMPLETE_MARKER
    temporary = directory / f".{_COMPLETE_MARKER}.{uuid.uuid4().hex}.tmp"
    try:
        _write_file(temporary, canonical_json(_marker_payload(refreshed)))
        os.replace(temporary, marker)
        _fsync_directory(directory)
    finally:
        if temporary.exists():
            temporary.unlink()
    return refreshed


def base_manifest_id(manifest_id: str) -> str:
    """Базовый manifest_id: exact claim носит суффикс «:N» (_owner_launch_plan).

    Staging публикуется под базовым id (директория и marker), а на исполнение
    приходит per-claim манифест «база:N» с одним destination и одним creative.
    Все проверки staging обязаны сводить id к базовому — иначе они не могут
    сойтись никогда (регрессия approval-first: каждый запуск горел
    LAUNCH_STAGING_DRIFT).
    """
    return str(manifest_id).split(":", 1)[0]


def _canonical_dict(value: Any) -> Any:
    """Round-trip через canonical_json: dataclasses → простые dict/list."""
    return json.loads(canonical_json(value).decode("utf-8"))


def marker_matches_claim(marker_payload: Mapping[str, Any], expected_payload: Mapping[str, Any]) -> bool:
    """Сверка полного staging marker с per-claim манифестом «база:N».

    Marker несёт ВСЕ destinations/creatives запуска, claim — ровно один
    destination с одним creative. Претензии claim обязаны быть подмножеством
    marker: одинаковые инварианты (trello, campaign_type, config, media),
    его destination найден в marker по (account_id, adset_id) со всеми
    полями кроме creatives, и каждый его creative байт-в-байт есть в
    creatives этого destination.
    """
    try:
        marker = _canonical_dict(dict(marker_payload))
        expected = _canonical_dict(dict(expected_payload))
    except (TypeError, ValueError):
        return False
    if marker.get("manifest_id") != base_manifest_id(str(expected.get("manifest_id", ""))):
        return False
    for key in (marker.keys() | expected.keys()) - {"manifest_id", "destinations"}:
        if marker.get(key) != expected.get(key):
            return False
    marker_by_scope = {
        (dest.get("account_id"), dest.get("adset_id")): dest
        for dest in marker.get("destinations") or []
        if isinstance(dest, dict)
    }
    claim_destinations = expected.get("destinations") or []
    if not claim_destinations:
        return False
    for dest in claim_destinations:
        if not isinstance(dest, dict):
            return False
        full = marker_by_scope.get((dest.get("account_id"), dest.get("adset_id")))
        if full is None:
            return False
        if {k: v for k, v in full.items() if k != "creatives"} != {
            k: v for k, v in dest.items() if k != "creatives"
        }:
            return False
        full_creatives = {canonical_json(c) for c in full.get("creatives") or []}
        claim_creatives = dest.get("creatives") or []
        if not claim_creatives:
            return False
        if any(canonical_json(c) not in full_creatives for c in claim_creatives):
            return False
    return True


def verify_staged_launch(prepared: PreparedLaunch) -> bool:
    """Перечитывает marker, все media bytes и все creative body bytes."""

    if not isinstance(prepared, PreparedLaunch):
        return False
    try:
        root = _safe_root(Path(REPORT_CHECKER_STAGING_ROOT))
        base_id = _safe_manifest_id(base_manifest_id(prepared.manifest_id))
        expected_directory = root / base_id
        if (
            prepared.staging_root.resolve(strict=True) != root
            or prepared.staging_directory != expected_directory
            or expected_directory.is_symlink()
            or expected_directory.resolve(strict=True) != expected_directory
        ):
            return False
        marker = expected_directory / _COMPLETE_MARKER
        marker_info = marker.lstat()
        if (
            stat.S_ISLNK(marker_info.st_mode)
            or not stat.S_ISREG(marker_info.st_mode)
            or marker_info.st_nlink != 1
        ):
            return False
        marker_bytes = marker.read_bytes()
        if prepared.manifest_id == base_id:
            # Полный манифест: байт-в-байт, как раньше.
            if marker_bytes != canonical_json(_marker_payload(prepared)):
                return False
        else:
            # Exact claim «база:N»: подмножество полного marker.
            if not marker_matches_claim(
                json.loads(marker_bytes.decode("utf-8")),
                _marker_payload(prepared),
            ):
                return False
        paths = tuple(asset.staged_relative_path for asset in prepared.media_assets)
        digests = rehash_staged_files(paths, staging_root=expected_directory)
        if tuple(
            (item.order_index, item.size_bytes, item.content_sha256)
            for item in digests
        ) != tuple(
            (asset.order_index, asset.size_bytes, asset.content_sha256)
            for asset in prepared.media_assets
        ):
            return False
        body_paths = tuple(
            dict.fromkeys(
                creative.body_staged_relative_path
                for destination in prepared.destinations
                for creative in destination.creatives
            )
        )
        body_digests = rehash_staged_files(
            body_paths,
            staging_root=expected_directory,
        )
        expected_body = {
            creative.body_staged_relative_path: creative.body_sha256
            for destination in prepared.destinations
            for creative in destination.creatives
        }
        return all(
            digest.normalized_text_sha256 == expected_body[digest.relative_path]
            for digest in body_digests
        )
    except (OSError, ValueError, LaunchStagingError, KeyError):
        return False


def release_staging(manifest_id: str, *, terminal_result: ActionResult) -> None:
    """Удаляет staged bytes только после terminal CONFIRMED/FAILED."""

    if terminal_result in {ActionResult.PARTIAL, ActionResult.UNKNOWN}:
        return
    if terminal_result not in {ActionResult.CONFIRMED, ActionResult.FAILED}:
        raise ValueError("terminal_result должен быть terminal ActionResult")
    purge_staging_directory(_safe_manifest_id(manifest_id))


def purge_staging_directory(directory_name: str) -> bool:
    """Удаляет каталог внутри staging root теми же проверками, что и release.

    Принимает имя заявки (manifest_id) и имя брошенного временного каталога
    `.<manifest_id>.<random>`, который остаётся после жёсткого падения
    процесса в `stage_launch`. Возвращает False, если удалять уже нечего.
    """

    root = _safe_root(Path(REPORT_CHECKER_STAGING_ROOT))
    if not _is_safe_staging_entry(directory_name):
        raise LaunchStagingError("STAGING_RELEASE_PATH_UNSAFE")
    target = root / directory_name
    if target.is_symlink():
        raise LaunchStagingError("STAGING_RELEASE_PATH_UNSAFE")
    if not target.exists():
        return False
    if not target.is_dir() or target.resolve(strict=True).parent != root:
        raise LaunchStagingError("STAGING_RELEASE_PATH_UNSAFE")
    shutil.rmtree(target)
    _fsync_directory(root)
    return True


def _is_safe_staging_entry(name: str) -> bool:
    """Пропускает только собственные имена staging: заявку или её temp-каталог."""

    if name in {"", ".", ".."} or any(char in name for char in ("/", "\\", "\0")):
        return False
    return bool(_MANIFEST_ID_RE.match(name) or _TEMP_DIRECTORY_RE.match(name))
