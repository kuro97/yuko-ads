import json
import hashlib
import inspect
import logging
import math
import subprocess
import os
import re
import threading
import time
import tempfile
import weakref
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from config import FB_PAGE_ID, LEAD_FORMS, AD_TITLE
from services.fb_token_provider import get_fb_token, get_fb_account_id
from services.cleanup_authorization import CleanupDeleteAuthorization
from services.cleanup_repository import (
    prepare_cleanup_delete_boundary,
)
from services.product_tags import format_ad_name
from services import launch_repository
from services.approval_checker_models import (
    ActionObservation,
    AssetRecoveryManifest,
    CreativeSpec,
    LaunchManifest,
    MediaAssetSpec,
    MediaType,
    PlacementRole,
    canonical_json,
)
from services.owner_action_models import ActionAttemptAttestation
from services.launch_checker import (
    LaunchCheckBlocked,
    ProviderCreateScope,
    ProviderLaunchAuthorization,
    bind_provider_authorization,
    get_bound_provider_authorization,
    validate_authorization_media,
)

# API URL и session — из общего модуля, чтобы не дублировать
from agent.fb_common import API, _throttled_get

logger = logging.getLogger(__name__)

VIDEO_API = "https://graph-video.facebook.com/v21.0"
MAX_ADS_PER_ADSET = 50
COST_PER_CREATIVE = 15  # $ — бюджет делится на это число
_MAX_ADSET_INVENTORY_PAGES = 10
_MAX_ACCOUNT_INVENTORY_PAGES = 100
_SAFE_CLEANUP_EFFECTIVE_STATUSES = frozenset({
    "PAUSED",
    "ADSET_PAUSED",
    "CAMPAIGN_PAUSED",
})
_RETIRED_AD_EFFECTIVE_STATUSES = frozenset({"DELETED", "ARCHIVED"})
_KNOWN_AD_EFFECTIVE_STATUSES = frozenset({
    "ACTIVE",
    "PAUSED",
    "PENDING_REVIEW",
    "DISAPPROVED",
    "PREAPPROVED",
    "PENDING_BILLING_INFO",
    "CAMPAIGN_PAUSED",
    "ADSET_PAUSED",
    "IN_PROCESS",
    "WITH_ISSUES",
    *_RETIRED_AD_EFFECTIVE_STATUSES,
})
_CLEANUP_GUARD_REGISTRY_LOCK = threading.Lock()
_CLEANUP_GUARD_REGISTRY: dict[int, "_CleanupGuardRegistryRecord"] = {}
_CLEANUP_GUARD_TTL_SECONDS = 120.0
_TOKEN_RE = re.compile(
    r"(?i)(access_token(?:=|%3D)|authorization:\s*bearer\s+)[^&\s]+"
)


def _safe_error(error: object) -> str:
    return _TOKEN_RE.sub(r"\1<redacted>", str(error))


def _prepare_current_cleanup_delete_boundary(
    *,
    claim_id: str,
    workflow_id: str,
    adset_id: str,
    ad_id: str,
) -> tuple[dict, str, dict[str, object]]:
    """Перечитывает current config/local evidence у самой HTTP-границы."""
    from services.adset_cleaner import (
        cleanup_runtime_config_identity,
        get_cleaner_config,
        load_local_zero_evidence,
    )

    current_config = get_cleaner_config()
    config_identity = cleanup_runtime_config_identity(current_config)
    fresh_local = load_local_zero_evidence(ad_id)
    local_hash = prepare_cleanup_delete_boundary(
        claim_id=claim_id,
        workflow_id=workflow_id,
        adset_id=adset_id,
        ad_id=ad_id,
        runtime_config_hash=config_identity["config_hash"],
        runtime_config_generation=config_identity["config_generation"],
        local_zero_evidence=fresh_local,
    )
    return dict(fresh_local), local_hash, dict(config_identity)


def _get_launch_hard_reserve_slots() -> int:
    """Возвращает обязательный запас object-slots после любого CREATE."""
    from services.adset_cleaner import get_cleaner_config

    value = get_cleaner_config().get("hard_reserve_slots", 1)
    if type(value) is not int or not 1 <= value <= 5:
        raise ValueError("hard_reserve_slots должен быть int от 1 до 5")
    return value


def _get_other_launch_reserved_slots(workflow_id: str | None, adset_id: str) -> int:
    """Считает durable reservations, исключая текущий replacement workflow."""
    from services.adset_cleaner import _other_reserved_slots

    reserved = _other_reserved_slots(workflow_id or "", adset_id)
    if type(reserved) is not int or not 0 <= reserved <= MAX_ADS_PER_ADSET:
        raise ValueError("other_reserved_slots должен быть int от 0 до 50")
    return reserved


class AdsetInventoryError(RuntimeError):
    """Безопасная ошибка неполного live inventory Facebook."""


class ExistingCreativeSourceError(RuntimeError):
    """Source ad нельзя безопасно использовать как exact creative."""


class AssetRecoveryCreateNotStarted(RuntimeError):
    """Gateway доказал, что до Graph POST выполнение не дошло."""


class LaunchMediaBindingError(RuntimeError):
    """Локальный media batch нельзя безопасно связать с CREATE."""


@dataclass(frozen=True, slots=True)
class LaunchMediaFileEvidence:
    """Безопасная identity и content evidence одного локального asset."""

    relative_name: str
    size: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class LaunchMediaManifest:
    """Immutable city manifest exact файлов, scope и ordered ad names."""

    manifest_sha256: str
    media_type: str
    card_name_sha256: str
    city: str
    account_kind: str
    account_id: str
    adset_id: str
    expected_ad_names: tuple[str, ...]
    files: tuple[LaunchMediaFileEvidence, ...]


@dataclass(slots=True)
class _ProviderLaunchContext:
    """Проверенный media/account контекст текущего server-issued proof."""

    proof: ProviderLaunchAuthorization
    attempt: ActionAttemptAttestation
    media_sha256: str
    account_kind: str
    account_id: str
    created_ad_ids: dict[str, str]
    replacement_workflow_ids: dict[str, str]


_BOUND_PROVIDER_LAUNCH: ContextVar[_ProviderLaunchContext | None] = ContextVar(
    "facebook_provider_launch_context",
    default=None,
)


class CleanupGuardError(RuntimeError):
    """Стабильный fail-closed код блокировки DELETE."""


class CleanupDeleteReconcileRequired(CleanupGuardError):
    """Результат DELETE неоднозначен; автоматический retry запрещён."""

    outcome = "RECONCILE_REQUIRED"


class CompleteAccountAdInventory(list[dict[str, object]]):
    """List-compatible результат только завершённого exact account scan."""

    __slots__ = ("account_id", "account_kind", "inventory_complete")

    def __init__(self, account_kind: str, account_id: str) -> None:
        super().__init__()
        self.account_kind = account_kind
        self.account_id = account_id
        self.inventory_complete = True


@dataclass(frozen=True, slots=True, weakref_slot=True)
class CleanupGuardEvidence:
    """Одноразовая привязка DELETE к одному exact candidate."""

    adset_id: str
    stale_days: int
    candidate_id: str
    total_ids: frozenset[str]
    effective_active_ids: frozenset[str]
    issued_at_monotonic: float


@dataclass(frozen=True, slots=True)
class _CleanupGuardRegistryRecord:
    """Неизменяемая эталонная копия полей выданной evidence."""

    reference: weakref.ReferenceType[CleanupGuardEvidence]
    adset_id: str
    stale_days: int
    candidate_id: str
    total_ids: frozenset[str]
    effective_active_ids: frozenset[str]
    issued_at_monotonic: float


def _mint_cleanup_guard_evidence(
    *,
    adset_id: str,
    stale_days: int,
    candidate_id: str,
    total_ids: frozenset[str],
    effective_active_ids: frozenset[str],
) -> CleanupGuardEvidence:
    """Регистрирует evidence, созданный только проверенным кодом этого модуля."""
    issued_at_monotonic = time.monotonic()
    evidence = CleanupGuardEvidence(
        adset_id=adset_id,
        stale_days=stale_days,
        candidate_id=candidate_id,
        total_ids=total_ids,
        effective_active_ids=effective_active_ids,
        issued_at_monotonic=issued_at_monotonic,
    )
    evidence_id = id(evidence)

    def unregister(reference: weakref.ReferenceType[CleanupGuardEvidence]) -> None:
        with _CLEANUP_GUARD_REGISTRY_LOCK:
            registered = _CLEANUP_GUARD_REGISTRY.get(evidence_id)
            if registered is not None and registered.reference is reference:
                _CLEANUP_GUARD_REGISTRY.pop(evidence_id, None)

    reference = weakref.ref(evidence, unregister)
    record = _CleanupGuardRegistryRecord(
        reference=reference,
        adset_id=adset_id,
        stale_days=stale_days,
        candidate_id=candidate_id,
        total_ids=total_ids,
        effective_active_ids=effective_active_ids,
        issued_at_monotonic=issued_at_monotonic,
    )
    with _CLEANUP_GUARD_REGISTRY_LOCK:
        _CLEANUP_GUARD_REGISTRY[evidence_id] = record
    return evidence


def _consume_cleanup_guard_evidence(
    evidence: object,
    candidate_id: str,
) -> _CleanupGuardRegistryRecord:
    """Атомарно потребляет module-minted evidence; replay всегда запрещён."""
    if not isinstance(evidence, CleanupGuardEvidence):
        raise CleanupGuardError("cleanup_guard_evidence_required")
    with _CLEANUP_GUARD_REGISTRY_LOCK:
        registered = _CLEANUP_GUARD_REGISTRY.pop(id(evidence), None)
    if registered is None or registered.reference() is not evidence:
        raise CleanupGuardError("cleanup_guard_evidence_consumed_or_forged")
    if time.monotonic() - registered.issued_at_monotonic > _CLEANUP_GUARD_TTL_SECONDS:
        raise CleanupGuardError("cleanup_guard_evidence_expired")
    evidence_state = (
        evidence.adset_id,
        evidence.stale_days,
        evidence.candidate_id,
        evidence.total_ids,
        evidence.effective_active_ids,
        evidence.issued_at_monotonic,
    )
    registered_state = (
        registered.adset_id,
        registered.stale_days,
        registered.candidate_id,
        registered.total_ids,
        registered.effective_active_ids,
        registered.issued_at_monotonic,
    )
    if evidence_state != registered_state:
        raise CleanupGuardError("cleanup_guard_evidence_tampered")
    if registered.candidate_id != candidate_id:
        raise CleanupGuardError("cleanup_guard_candidate_binding_mismatch")
    return registered


def upload_video(video_path: str, name: str, progress_cb=None) -> str:
    """Загружает видео в FB, возвращает video_id.
    Файлы >50MB загружаются через chunked upload.
    Всегда ждёт пока видео обработается.

    progress_cb — необязательный callable(step: str, step_pct: int|None).
    Вызывается в ключевых точках загрузки и ожидания обработки.
    """
    provider_context = _BOUND_PROVIDER_LAUNCH.get()
    if provider_context is None or get_bound_provider_authorization() != provider_context.proof:
        raise LaunchCheckBlocked(
            "AUTHORIZATION_REQUIRED",
            ("Provider upload вызван вне проверенного launch context",),
            None,
        )
    actual_account_id = str(get_fb_account_id()).removeprefix("act_").strip()
    if actual_account_id != provider_context.account_id:
        raise LaunchCheckBlocked(
            "PROVIDER_SCOPE_DRIFT",
            ("Facebook account upload изменился после проверки",),
            provider_context.proof.auth_id,
        )
    import os
    file_size = os.path.getsize(video_path)
    if progress_cb:
        try:
            progress_cb("Заливаю видео в Facebook…", None)
        except Exception:
            pass
    if file_size > 15 * 1024 * 1024:  # >15MB -> chunked (надёжнее на медленном канале сервера)
        return _upload_video_chunked(video_path, name, file_size, progress_cb=progress_cb)
    from integrations.facebook_ads_mutation_transport import _create_launch_asset

    with open(video_path, "rb") as f:
        resp = _create_launch_asset(
            provider_context.attempt,
            account_id=provider_context.attempt.account_id,
            url=f"{VIDEO_API}/act_{get_fb_account_id()}/advideos",
            data={"name": name},
            files={"source": f},
            timeout=(10, 300),  # видео большое — даём до 5 мин на заливку
        )
    resp.raise_for_status()
    video_id = resp.json()["id"]
    # Ждём обработки видео
    _wait_video_ready(video_id, progress_cb=progress_cb)
    return video_id


def _upload_video_chunked(video_path: str, name: str, file_size: int, progress_cb=None) -> str:
    """Chunked upload для больших видео (FB API 3-step).

    progress_cb — необязательный callable(step: str, step_pct: int|None).
    Вызывается после каждого залитого чанка с прогрессом в МБ.
    """
    account_id = get_fb_account_id()
    provider_context = _BOUND_PROVIDER_LAUNCH.get()
    if provider_context is None:
        raise LaunchCheckBlocked(
            "AUTHORIZATION_REQUIRED",
            ("Chunked upload вызван вне проверенного launch context",),
            None,
        )
    from integrations.facebook_ads_mutation_transport import _create_launch_asset

    url = f"{VIDEO_API}/act_{account_id}/advideos"
    chunk_size = 8 * 1024 * 1024  # 8MB чанки — на медленном/нестабильном канале надёжнее
    total_mb = file_size // (1024 * 1024) or 1  # защита от деления на 0 для крошечных файлов

    # Шаг 1: Start upload session
    resp = _create_launch_asset(
        provider_context.attempt,
        account_id=provider_context.attempt.account_id,
        url=url,
        data={"upload_phase": "start", "file_size": file_size},
    )
    resp.raise_for_status()
    start_data = resp.json()
    upload_session_id = start_data["upload_session_id"]
    video_id = start_data.get("video_id") or start_data.get("id")
    logger.info("Chunked upload started: session=%s, video_id=%s, size=%dMB", upload_session_id, video_id, file_size // (1024*1024))

    # Шаг 2: Transfer chunks
    with open(video_path, "rb") as f:
        offset = 0
        while offset < file_size:
            chunk = f.read(chunk_size)
            # Повтор при обрыве/таймауте — канал сервера медленный и нестабильный
            transfer_data = None
            for attempt in range(4):
                try:
                    resp = _create_launch_asset(
                        provider_context.attempt,
                        account_id=provider_context.attempt.account_id,
                        url=url,
                        data={
                            "upload_phase": "transfer",
                            "upload_session_id": upload_session_id,
                            "start_offset": offset,
                        },
                        files={"video_file_chunk": chunk},
                        timeout=(10, 300),
                    )
                    resp.raise_for_status()
                    transfer_data = resp.json()
                    break
                except Exception as exc:
                    if attempt == 3:
                        raise
                    logger.warning("Чанк offset=%d не залился (попытка %d/4): %s — повтор", offset, attempt + 1, exc)
                    time.sleep(3 * (attempt + 1))
            new_offset = int(transfer_data.get("start_offset", offset + len(chunk)))
            done_mb = new_offset // (1024 * 1024)
            logger.info("Chunk uploaded: %dMB / %dMB", done_mb, total_mb)
            # Обновляем под-статус после каждого чанка
            if progress_cb:
                try:
                    progress_cb(
                        f"Заливаю видео… {done_mb}/{total_mb} МБ",
                        int(done_mb / total_mb * 100),
                    )
                except Exception:
                    pass
            offset = new_offset

    # Шаг 3: Finish
    resp = _create_launch_asset(
        provider_context.attempt,
        account_id=provider_context.attempt.account_id,
        url=url,
        data={
            "upload_phase": "finish",
            "upload_session_id": upload_session_id,
            "title": name,
        },
    )
    resp.raise_for_status()
    finish_data = resp.json()
    logger.info("Chunked upload finish response: %s", finish_data)
    # video_id приходит из start response, finish возвращает только {success: True}
    video_id = video_id or finish_data.get("video_id") or finish_data.get("id")
    if not video_id:
        raise RuntimeError(f"FB chunked upload: no video_id. start={start_data}, finish={finish_data}")
    logger.info("Chunked upload finished: video_id=%s", video_id)

    # Ждём обработки видео
    _wait_video_ready(video_id, progress_cb=progress_cb)
    return video_id


def _wait_video_ready(video_id: str, max_wait: int = 300, progress_cb=None) -> None:
    """Ждём пока FB обработает видео (макс max_wait сек).

    progress_cb — необязательный callable(step: str, step_pct: int|None).
    Вызывается на КАЖДОМ опросе (раз в 5с) — чтобы UI не выглядел зависшим.
    """
    if not video_id:
        logger.error("_wait_video_ready called with video_id=None, skipping")
        return
    token = get_fb_token()
    waited = 0
    interval = 5
    while waited < max_wait:
        time.sleep(interval)
        waited += interval
        # Уведомляем UI о каждом опросе — главная точка «антизависания»
        if progress_cb:
            try:
                progress_cb(
                    f"⏳ Facebook обрабатывает видео… {waited}с",
                    int(waited / max_wait * 100),
                )
            except Exception:
                pass
        try:
            resp = _throttled_get(
                f"https://graph.facebook.com/v21.0/{video_id}",
                params={"access_token": token, "fields": "status"}
            )
            if resp.ok:
                st = resp.json().get("status", {})
                vs = st.get("video_status", "")
                logger.info("Video %s status: %s (%ds)", video_id, vs, waited)
                if vs == "ready":
                    return
                if vs == "error":
                    raise RuntimeError(f"FB video processing error: {st}")
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning("Video status check error: %s", e)
    logger.warning("Video %s not ready after %ds, proceeding anyway", video_id, max_wait)


def extract_thumbnail(video_path: str) -> str:
    """Извлекает кадр на 1 секунде, возвращает путь к jpg."""
    # Убираем любое расширение и добавляем _thumb.jpg
    base, _ = os.path.splitext(video_path)
    thumb_path = base + "_thumb.jpg"
    try:
        subprocess.run(
            ["ffmpeg", "-i", video_path, "-ss", "00:00:01", "-vframes", "1", thumb_path, "-y"],
            capture_output=True, check=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg таймаут: {video_path}")
    return thumb_path


def _prepare_image_for_upload(image_path: str) -> tuple[str, bool]:
    """Ужимает слишком большую картинку под лимиты FB.

    FB режет картинки с большими размерами/весом (error_subcode 1885355).
    Если длинная сторона > 1920px или файл > 8 МБ — уменьшаем (сторона ≤1920,
    пересохраняем JPEG q88). Без Pillow или при ошибке — отдаём оригинал.

    Возвращает (path, is_temp): is_temp=True если создан временный файл (удалить после).
    """
    try:
        from PIL import Image
    except ImportError:
        return image_path, False
    try:
        import os as _os
        import tempfile
        MAX_SIDE = 1920
        MAX_BYTES = 8 * 1024 * 1024
        too_big_file = _os.path.getsize(image_path) > MAX_BYTES
        with Image.open(image_path) as im:
            w, h = im.size
            if max(w, h) <= MAX_SIDE and not too_big_file:
                return image_path, False  # всё в норме
            ratio = MAX_SIDE / max(w, h) if max(w, h) > MAX_SIDE else 1.0
            out_im = im.convert("RGB")
            if ratio < 1.0:
                out_im = out_im.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            fd, out_path = tempfile.mkstemp(suffix=".jpg")
            _os.close(fd)
            out_im.save(out_path, "JPEG", quality=88, optimize=True)
            print(f"   🖼 Картинка ужата для FB: {w}x{h} → {out_im.size[0]}x{out_im.size[1]}")
            return out_path, True
    except Exception as e:
        print(f"   ⚠️ Не удалось ужать картинку ({e}) — загружаю оригинал")
        return image_path, False


def upload_image(image_path: str) -> str:
    """Загружает картинку в FB, возвращает image_hash.

    Большие картинки автоматически ужимаются под лимиты FB.
    Использует attested transport с retry при rate limit FB.
    """
    provider_context = _BOUND_PROVIDER_LAUNCH.get()
    if provider_context is None or get_bound_provider_authorization() != provider_context.proof:
        raise LaunchCheckBlocked(
            "AUTHORIZATION_REQUIRED",
            ("Provider upload вызван вне проверенного launch context",),
            None,
        )
    actual_account_id = str(get_fb_account_id()).removeprefix("act_").strip()
    if actual_account_id != provider_context.account_id:
        raise LaunchCheckBlocked(
            "PROVIDER_SCOPE_DRIFT",
            ("Facebook account upload изменился после проверки",),
            provider_context.proof.auth_id,
        )
    import os as _os
    from integrations.facebook_ads_mutation_transport import _create_launch_asset
    upload_path, is_temp = _prepare_image_for_upload(image_path)
    try:
        with open(upload_path, "rb") as f:
            resp = _create_launch_asset(
                provider_context.attempt,
                account_id=provider_context.attempt.account_id,
                url=f"{API}/act_{get_fb_account_id()}/adimages",
                data={},
                files={"filename": f},
            )
    finally:
        if is_temp:
            try:
                _os.remove(upload_path)
            except OSError:
                pass
    if resp.status_code != 200:
        raise RuntimeError(
            f"FB upload_image ошибка {resp.status_code}: {resp.text[:300]}"
        )
    images = resp.json()["images"]
    return list(images.values())[0]["hash"]


def _provider_now() -> datetime:
    return datetime.now(timezone.utc)


def _repository_block(exc: Exception, proof: ProviderLaunchAuthorization) -> LaunchCheckBlocked:
    """Переводит durable provider deny без утечки DB/provider деталей."""
    if isinstance(exc, LaunchCheckBlocked):
        return exc
    code = str(getattr(exc, "code", "PROVIDER_AUTHORIZATION_UNAVAILABLE"))
    reasons = tuple(
        str(reason)
        for reason in getattr(
            exc,
            "reasons",
            ("Durable provider authorization недоступна",),
        )
    )
    check_id = getattr(exc, "check_id", None)
    return LaunchCheckBlocked(code, reasons, check_id or proof.auth_id)


def _provider_city_from_name(name: str) -> str:
    parts = str(name).split(" | ", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise LaunchCheckBlocked(
            "PROVIDER_SCOPE_DRIFT",
            ("Ad name не содержит exact city identity",),
            None,
        )
    return parts[0].strip()


def _authorized_target(validation, city: str, adset_id: str):
    """Находит exact target только в DB-проверенном scope."""
    matches = tuple(
        target
        for target in getattr(validation, "targets", ())
        if str(getattr(target, "city", "")) == str(city)
        and str(getattr(target, "adset_id", "")) == str(adset_id)
    )
    if len(matches) != 1:
        raise LaunchCheckBlocked(
            "PROVIDER_SCOPE_DRIFT",
            ("City/adset не входит в exact authorization",),
            str(getattr(validation, "auth_id", "")) or None,
        )
    return matches[0]


def _assert_live_target_safe(
    target,
    *,
    proof: ProviderLaunchAuthorization,
    created_ad_ids: dict[str, str],
    now: datetime,
    account_inventory: list[dict[str, object]] | None = None,
    replacement_workflow_id: str | None = None,
) -> None:
    """Fresh full-account duplicate/capacity gate у самой provider-границы."""
    account_kind = str(getattr(target, "account_kind", ""))
    account_id = str(getattr(target, "account_id", "")).removeprefix("act_")
    adset_id = str(getattr(target, "adset_id", ""))
    expected_names = tuple(str(name) for name in getattr(target, "expected_names", ()))
    identity_key = str(getattr(target, "identity_key", ""))
    if not account_id or not adset_id or not expected_names or not identity_key:
        raise LaunchCheckBlocked(
            "AUTHORIZATION_SCOPE_INVALID",
            ("Authorization target не содержит complete exact scope",),
            proof.auth_id,
        )
    inventory = (
        fetch_complete_account_ad_inventory(account_kind, account_id)
        if account_inventory is None
        else account_inventory
    )
    live_rows = [
        row
        for row in inventory
        if isinstance(row, dict)
        and str(row.get("adset_id") or "") == adset_id
        and str(row.get("effective_status") or "") not in _RETIRED_AD_EFFECTIVE_STATUSES
    ]
    if any(
        str(row.get("effective_status") or "") not in _KNOWN_AD_EFFECTIVE_STATUSES
        for row in live_rows
    ):
        raise LaunchCheckBlocked(
            "INVENTORY_UNVERIFIED",
            ("Fresh Facebook inventory содержит unknown status",),
            proof.auth_id,
        )

    expected_keys = {
        launch_repository.normalize_launch_name(name) for name in expected_names
    }
    root_prefix = f"{identity_key} / "
    current_created_ids = set(created_ad_ids.values())
    try:
        sibling_ids = launch_repository.sibling_launch_ad_ids(proof.auth_id)
    except Exception:  # noqa: BLE001 — неясность = строгая проверка как раньше
        sibling_ids = frozenset()
    for row in live_rows:
        row_id = str(row.get("id") or "")
        row_name = str(row.get("name") or "")
        if row_id in current_created_ids and created_ad_ids.get(row_name) == row_id:
            continue
        try:
            live_key = launch_repository.normalize_launch_name(row_name)
        except (TypeError, ValueError) as exc:
            raise LaunchCheckBlocked(
                "INVENTORY_UNVERIFIED",
                ("Fresh Facebook inventory содержит malformed name",),
                proof.auth_id,
            ) from exc
        if live_key in expected_keys:
            raise LaunchCheckBlocked(
                "DUPLICATE_LIVE",
                ("Fresh Facebook inventory уже содержит exact/card-root duplicate",),
                proof.auth_id,
            )
        if row_id in sibling_ids:
            # Объявление соседнего claim'а того же запуска — не дубль карточки.
            continue
        if live_key == identity_key or live_key.startswith(root_prefix):
            raise LaunchCheckBlocked(
                "DUPLICATE_LIVE",
                ("Fresh Facebook inventory уже содержит exact/card-root duplicate",),
                proof.auth_id,
            )

    created_for_target = sum(
        1 for name in expected_names if name in created_ad_ids
    )
    remaining_current = len(expected_names) - created_for_target
    available = MAX_ADS_PER_ADSET - len(live_rows)
    try:
        launch_reserved = launch_repository.get_reserved_slots(
            adset_id,
            now,
            exclude_auth_id=proof.auth_id,
        )
        replacement_reserved = _get_other_launch_reserved_slots(
            replacement_workflow_id,
            adset_id,
        )
        hard_reserve = _get_launch_hard_reserve_slots()
    except Exception as exc:
        raise _repository_block(exc, proof) from exc
    required = (
        remaining_current
        + hard_reserve
        + launch_reserved
        + replacement_reserved
    )
    if available < required:
        raise LaunchCheckBlocked(
            "CAPACITY_BLOCKED",
            (
                f"Fresh capacity {available}, требуется {required} "
                "с единым hard reserve",
            ),
            proof.auth_id,
        )


def _renew_provider_target(
    proof: ProviderLaunchAuthorization,
    *,
    city: str,
    adset_id: str,
    media_sha256: str,
    now: datetime,
):
    """Атомарно renew+проверка exact target под city/adset lock."""
    try:
        renewed = launch_repository.renew_authorization_target(
            proof,
            city,
            str(adset_id),
            launch_repository.RESERVATION_TTL,
            now,
        )
        validation = getattr(renewed, "validation", None)
        if validation is None:
            validation = validate_authorization_media(
                proof,
                media_sha256,
                repository=launch_repository,
                now=now,
            )
        return _authorized_target(validation, city, str(adset_id))
    except Exception as exc:
        raise _repository_block(exc, proof) from exc


def _preupload_authorization_gate(
    proof: ProviderLaunchAuthorization,
    validation,
    *,
    media_sha256: str,
    replacement_workflow_ids: dict[str, str] | None = None,
) -> None:
    """Renew и fresh live recheck всех targets до первого upload."""
    now = _provider_now()
    targets = tuple(getattr(validation, "targets", ()))
    inventories: dict[tuple[str, str], list[dict[str, object]]] = {}
    for target in targets:
        city = str(getattr(target, "city", ""))
        adset_id = str(getattr(target, "adset_id", ""))
        renewed_target = _renew_provider_target(
            proof,
            city=city,
            adset_id=adset_id,
            media_sha256=media_sha256,
            now=now,
        )
        account_key = (
            str(getattr(renewed_target, "account_kind", "")),
            str(getattr(renewed_target, "account_id", "")).removeprefix("act_"),
        )
        inventory = inventories.get(account_key)
        if inventory is None:
            inventory = fetch_complete_account_ad_inventory(*account_key)
            inventories[account_key] = inventory
        _assert_live_target_safe(
            renewed_target,
            proof=proof,
            created_ad_ids={},
            now=now,
            account_inventory=inventory,
            replacement_workflow_id=(replacement_workflow_ids or {}).get(adset_id),
        )


@contextmanager
def _bind_provider_launch_context(
    proof: ProviderLaunchAuthorization,
    *,
    attempt: ActionAttemptAttestation,
    media_sha256: str,
    account_kind: str,
    account_id: str,
    replacement_workflow_ids: dict[str, str] | None = None,
):
    """Связывает DB-проверенный proof с фактическим provider account/media."""
    context = _ProviderLaunchContext(
        proof=proof,
        attempt=attempt,
        media_sha256=str(media_sha256),
        account_kind=str(account_kind),
        account_id=str(account_id).removeprefix("act_"),
        created_ad_ids={},
        replacement_workflow_ids=dict(replacement_workflow_ids or {}),
    )
    token = _BOUND_PROVIDER_LAUNCH.set(context)
    try:
        with bind_provider_authorization(proof):
            yield
    finally:
        _BOUND_PROVIDER_LAUNCH.reset(token)


_URL_LEAF_KEYS = frozenset({"link", "picture", "url"})


def _normalize_url_leaves(value: object, *, key: str | None = None) -> object:
    """Снимает косметическую нормализацию URL, которую делает Graph API.

    FB отдаёт ссылку креатива в канонизированном виде: к голому домену
    добавляется завершающий «/» (``https://example.com`` → ``https://example.com/``)
    и в ``link_data.link``, и в ``call_to_action.value.link``. Раньше
    строгое сравнение canonical_json считало это дрейфом креатива и рвало
    весь запуск после первого же созданного объявления (из N объявлений
    карточки создавалось одно). Нормализация симметрична и применяется к обеим сторонам,
    поэтому терпит ТОЛЬКО разницу в завершающем слэше — любой другой домен,
    путь или query по-прежнему дрейф.
    """
    if isinstance(value, dict):
        return {
            child_key: _normalize_url_leaves(child, key=str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_normalize_url_leaves(item, key=key) for item in value]
    if (
        isinstance(value, str)
        and key in _URL_LEAF_KEYS
        and value.startswith(("http://", "https://"))
    ):
        return value.rstrip("/")
    return value


def _semantic_projection(live: object, expected: object) -> object:
    """Проецирует Graph-ответ на exact форму отправленного creative payload."""

    if isinstance(expected, dict):
        if not isinstance(live, dict) or any(key not in live for key in expected):
            raise RuntimeError("FB semantic verification field missing")
        return {
            key: _semantic_projection(live[key], value)
            for key, value in expected.items()
        }
    if isinstance(expected, list):
        if not isinstance(live, list) or len(live) != len(expected):
            raise RuntimeError("FB semantic verification list drift")
        return [
            _semantic_projection(live_item, expected_item)
            for live_item, expected_item in zip(live, expected, strict=True)
        ]
    return live


def _post_ad(name: str, adset_id: str, creative: dict) -> str:
    """Единая fail-closed граница CREATE: durable claim → Graph → confirm."""
    proof = get_bound_provider_authorization()
    provider_context = _BOUND_PROVIDER_LAUNCH.get()
    if provider_context is None or provider_context.proof != proof:
        raise LaunchCheckBlocked(
            "AUTHORIZATION_REQUIRED",
            ("Provider CREATE вызван вне проверенного launch context",),
            None,
        )

    actual_account_id = str(get_fb_account_id()).removeprefix("act_").strip()
    if not actual_account_id or actual_account_id != provider_context.account_id:
        raise LaunchCheckBlocked(
            "PROVIDER_SCOPE_DRIFT",
            ("Активный Facebook account изменился после проверки",),
            proof.auth_id,
        )
    scope = ProviderCreateScope(
        account_kind=provider_context.account_kind,
        account_id=actual_account_id,
        city=_provider_city_from_name(name),
        adset_id=str(adset_id),
        ad_name=str(name),
        media_sha256=provider_context.media_sha256,
    )
    try:
        validation = validate_authorization_media(
            proof,
            provider_context.media_sha256,
            repository=launch_repository,
            now=_provider_now(),
        )
        target = _authorized_target(validation, scope.city, str(adset_id))
        _assert_live_target_safe(
            target,
            proof=proof,
            created_ad_ids=provider_context.created_ad_ids,
            now=_provider_now(),
            replacement_workflow_id=provider_context.replacement_workflow_ids.get(
                str(adset_id)
            ),
        )
    except Exception as exc:
        raise _repository_block(exc, proof) from exc
    expected_provider_payload = dict(creative)
    expected_fingerprint = hashlib.sha256(
        canonical_json(expected_provider_payload)
    ).hexdigest()
    try:
        claim_id = launch_repository.claim_provider_create(
            proof,
            scope,
            _provider_now(),
            expected_fingerprint=expected_fingerprint,
            expected_payload=expected_provider_payload,
        )
    except Exception as exc:
        raise _repository_block(exc, proof) from exc

    from integrations.facebook_ads_mutation_transport import create_ad as create_attested_ad

    ad_id = create_attested_ad(
        provider_context.attempt,
        account_id=provider_context.attempt.account_id,
        adset_id=adset_id,
        name=name,
        creative=creative,
        payload_sha256=provider_context.attempt.payload_sha256,
        url_tags=(
            "utm_source=facebook&utm_medium=cpc&"
            "utm_content={{ad.id}}&utm_campaign={{campaign.name}}"
        ),
    )
    try:
        live_response = _throttled_get(
            f"{API}/{ad_id}",
            params={
                "access_token": get_fb_token(),
                "fields": (
                    "id,name,adset_id,creative{id,object_story_spec,asset_feed_spec}"
                ),
            },
        )
        if live_response.status_code != 200:
            raise RuntimeError("FB semantic verification недоступна")
        live_ad = live_response.json()
        if (
            not isinstance(live_ad, dict)
            or str(live_ad.get("id") or "") != ad_id
            or str(live_ad.get("name") or "") != str(name)
            or str(live_ad.get("adset_id") or "") != str(adset_id)
        ):
            raise RuntimeError("FB semantic verification scope mismatch")
        live_creative = live_ad.get("creative")
        if not isinstance(live_creative, dict):
            raise RuntimeError("FB semantic verification creative missing")
        creative_id = str(live_creative.get("id") or "")
        if not creative_id:
            raise RuntimeError("FB semantic verification creative id missing")
        if "creative_id" in expected_provider_payload:
            if str(expected_provider_payload["creative_id"]) != creative_id:
                raise RuntimeError("FB semantic verification creative id drift")
        else:
            live_projection = _semantic_projection(
                live_creative,
                expected_provider_payload,
            )
            if canonical_json(_normalize_url_leaves(live_projection)) != canonical_json(
                _normalize_url_leaves(expected_provider_payload)
            ):
                raise RuntimeError("FB semantic verification creative payload drift")
        launch_repository.record_provider_ad_verified(
            claim_id,
            ad_id,
            creative_id,
            expected_fingerprint,
            _provider_now(),
        )
        launch_repository.record_provider_create_success(
            claim_id,
            ad_id,
            _provider_now(),
        )
    except Exception as exc:
        raise _repository_block(exc, proof) from exc
    provider_context.created_ad_ids[str(name)] = ad_id
    return ad_id


def get_existing_ad_creative_source(
    ad_id: str,
    expected_account_id: str,
    expected_adset_id: str,
    expected_adset_name: str,
    expected_adset_type: str,
) -> dict:
    """Читает source creative с полной привязкой к кабинету и adset."""
    source_id = str(ad_id)
    account_id = str(expected_account_id).removeprefix("act_")
    source_adset_id = str(expected_adset_id)
    if (
        not source_id.isdigit()
        or not account_id.isdigit()
        or not source_adset_id.isdigit()
        or not isinstance(expected_adset_name, str)
        or not expected_adset_name.strip()
        or expected_adset_type not in {"L2", "L1"}
    ):
        raise ExistingCreativeSourceError("source_identity_invalid")
    if not re.search(
        rf"(?<!\w){re.escape(expected_adset_type)}(?!\w)",
        expected_adset_name,
        flags=re.IGNORECASE,
    ):
        raise ExistingCreativeSourceError("source_adset_type_mismatch")
    if str(get_fb_account_id()).removeprefix("act_") != account_id:
        raise ExistingCreativeSourceError("source_account_context_mismatch")

    response = _throttled_get(
        API,
        params={
            "access_token": get_fb_token(),
            "ids": source_id,
            "fields": (
                "id,name,account_id,adset{id,name},status,effective_status,creative{id}"
            ),
        },
    )
    payload = _inventory_payload(response, "source_ad")
    if set(payload) != {source_id}:
        raise ExistingCreativeSourceError("source_ad_not_unique")
    row = payload.get(source_id)
    if not isinstance(row, dict) or str(row.get("id", "")) != source_id:
        raise ExistingCreativeSourceError("source_ad_not_unique")
    creative = row.get("creative")
    creative_id = creative.get("id") if isinstance(creative, dict) else None
    adset = row.get("adset")
    live_adset_id = adset.get("id") if isinstance(adset, dict) else None
    live_adset_name = adset.get("name") if isinstance(adset, dict) else None
    result = {
        "ad_id": source_id,
        "name": row.get("name"),
        "account_id": str(row.get("account_id", "")).removeprefix("act_"),
        "adset_id": str(live_adset_id or ""),
        "adset_name": live_adset_name,
        "status": row.get("status"),
        "effective_status": row.get("effective_status"),
        "creative_id": str(creative_id or ""),
    }
    if (
        not isinstance(result["name"], str)
        or not result["name"]
        or result["account_id"] != account_id
        or result["adset_id"] != source_adset_id
        or result["adset_name"] != expected_adset_name
        or result["status"] != "ACTIVE"
        or result["effective_status"] != "ACTIVE"
        or not result["creative_id"].isdigit()
    ):
        raise ExistingCreativeSourceError("source_ad_incomplete_or_not_active")
    source_city_parts = result["name"].split(" | ", 1)
    source_city = source_city_parts[0].strip()
    if len(source_city_parts) != 2 or not re.search(
        rf"(?<!\w){re.escape(source_city)}(?!\w)",
        expected_adset_name,
        flags=re.IGNORECASE,
    ):
        raise ExistingCreativeSourceError("source_city_adset_mismatch")
    return result


def create_ad_from_existing_creative(
    name: str,
    adset_id: str,
    creative_id: str,
) -> str:
    """Legacy raw CREATE закрыт; разрешён только attested asset gateway."""

    del name, adset_id, creative_id
    raise AssetRecoveryCreateNotStarted("asset_recovery_gateway_required")


def _execute_asset_recovery_manifest_unchecked(
    manifest: AssetRecoveryManifest,
    attempt: ActionAttemptAttestation,
) -> str:
    """Единственная provider-граница existing-creative recovery.

    Имя retained для симметрии с LAUNCH executor: функция private, но сама
    проверяет fsync WAL attestation и durable SQL claim до первого POST.
    """

    if type(attempt) is not ActionAttemptAttestation:
        raise AssetRecoveryCreateNotStarted("asset_recovery_attempt_invalid")
    # Ретрай идёт в кабинет ЦЕЛИ: для маршрутизированного оффлайн-кабинета
    # (карта город→кабинет, например cabinet_b) входим в его контекст сами.
    # Для дефолтного/неизвестного кабинета контекст НЕ трогаем — старая
    # ambient-проверка ниже остаётся авторитетом (fail-closed).
    from contextlib import nullcontext

    from services.fb_token_provider import fb_account, offline_account_context

    routed_context: str | None = None
    try:
        routed_context = offline_account_context(manifest.account_id)
    except RuntimeError:
        routed_context = None
    account_scope = (
        fb_account(routed_context) if routed_context else nullcontext()
    )
    from integrations.facebook_ads_mutation_transport import create_ad as create_attested_ad
    from integrations.facebook_ads_mutation_transport import AttestationRejected

    with account_scope:
        current_account_id = str(get_fb_account_id()).removeprefix("act_")
        if current_account_id != manifest.account_id:
            raise AssetRecoveryCreateNotStarted("asset_recovery_account_drift")
        try:
            return create_attested_ad(
                attempt,
                account_id=attempt.account_id,
                adset_id=manifest.target_adset_id,
                name=manifest.target_ad_name,
                creative={"creative_id": manifest.source_creative_id},
                payload_sha256=attempt.payload_sha256,
                url_tags=(
                    "utm_source=facebook&utm_medium=cpc&"
                    "utm_content={{ad.id}}&utm_campaign={{campaign.name}}"
                ),
            )
        except AttestationRejected as exc:
            raise AssetRecoveryCreateNotStarted(
                "asset_recovery_attempt_invalid"
            ) from exc


def create_ad(name: str, adset_id: str, adset_type: str, video_id: str, image_hash: str, body: str,
              instagram_user_id: str | None = None) -> str:
    """Создаёт видео-объявление, возвращает ad_id.
    instagram_user_id — переопределение IG (для PRODB-кампании используем acme_prodb_ig)."""
    form = LEAD_FORMS[adset_type]
    spec = {"page_id": FB_PAGE_ID}
    if instagram_user_id:
        spec["instagram_user_id"] = instagram_user_id
    spec["video_data"] = {
        "video_id": video_id,
        "image_hash": image_hash,
        "title": AD_TITLE,
        "message": body,
        "call_to_action": {
            "type": form["cta"],
            "value": {"lead_gen_form_id": form["form_id"]},
        },
    }
    return _post_ad(name, adset_id, {"object_story_spec": spec})


def create_image_ad(name: str, adset_id: str, adset_type: str, image_hash: str, body: str,
                     instagram_user_id: str | None = None) -> str:
    """Создаёт объявление с одним изображением, возвращает ad_id.
    instagram_user_id — переопределение IG (для PRODB-кампании)."""
    form = LEAD_FORMS[adset_type]
    # FB требует внешний link (не facebook.com) для LeadGen image ads (ошибка 1815316/2061015)
    external_link = "https://example.com"
    spec = {"page_id": FB_PAGE_ID}
    if instagram_user_id:
        spec["instagram_user_id"] = instagram_user_id
    spec["link_data"] = {
        "image_hash": image_hash,
        "message": body,
        "name": AD_TITLE,
        "link": external_link,
        "call_to_action": {
            "type": form["cta"],
            "value": {"lead_gen_form_id": form["form_id"]},
        },
    }
    return _post_ad(name, adset_id, {"object_story_spec": spec})


# CTA, поддерживаемые каруселью (GET_QUOTE не работает)
_CAROUSEL_CTA_FALLBACK = {
    "GET_QUOTE": "SIGN_UP",
}


def create_placement_image_ad(
    name: str,
    adset_id: str,
    adset_type: str,
    feed_image_hash: str,
    story_image_hash: str,
    body: str,
    instagram_user_id: str | None = None,
) -> str:
    """Создаёт ОДНО Lead Form объявление с placement-кастомизацией:
    в ленте показывается feed_image_hash (1:1/4:5), в сторис/reels —
    story_image_hash (9:16). Использует asset_feed_spec + asset_customization_rules.

    adset_type: "L2"|"L1" → берём форму/CTA из LEAD_FORMS.
    instagram_user_id: переопределение IG (для leadgen_prodb — acme_prodb_ig).
    Возвращает ad_id.
    """
    form = LEAD_FORMS[adset_type]
    external_link = "https://example.com"
    # CTA: asset_feed_spec не принимает GET_QUOTE — фоллбэк как в карусели
    cta_type = _CAROUSEL_CTA_FALLBACK.get(form["cta"], form["cta"])

    object_story_spec = {"page_id": FB_PAGE_ID}
    if instagram_user_id:
        object_story_spec["instagram_user_id"] = instagram_user_id

    asset_feed_spec = {
        "images": [
            {"hash": feed_image_hash,  "adlabels": [{"name": "feed_img"}]},
            {"hash": story_image_hash, "adlabels": [{"name": "story_img"}]},
        ],
        "bodies": [{"text": body}],
        "titles": [{"text": AD_TITLE}],
        "link_urls": [{"website_url": external_link}],
        "ad_formats": ["SINGLE_IMAGE"],
        "call_to_action_types": [cta_type],
        "call_to_actions": [{
            "type": cta_type,
            "value": {"lead_gen_form_id": form["form_id"], "link": external_link},
        }],
        "asset_customization_rules": [
            # Правило 1: лента (feed) — показываем feed-картинку
            {
                "customization_spec": {
                    "publisher_platforms": ["facebook", "instagram"],
                    "facebook_positions": ["feed"],
                    "instagram_positions": ["stream"],
                },
                "image_label": {"name": "feed_img"},
            },
            # Правило 2: сторис и reels — показываем story-картинку (9:16)
            {
                "customization_spec": {
                    "publisher_platforms": ["facebook", "instagram"],
                    "facebook_positions": ["story"],
                    "instagram_positions": ["story", "reels"],
                },
                "image_label": {"name": "story_img"},
            },
            # Правило 3: дефолтный фоллбэк на feed-картинку для всех остальных плейсментов.
            # ОБЯЗАТЕЛЬНО — FB требует покрытия ВСЕХ плейсментов адсета, иначе ошибка.
            {
                "customization_spec": {
                    "publisher_platforms": ["facebook", "instagram", "audience_network", "messenger"],
                    "facebook_positions": ["feed", "right_hand_column", "marketplace", "video_feeds", "search", "instream_video", "facebook_reels"],
                    "instagram_positions": ["stream", "explore", "explore_home", "profile_feed", "ig_search"],
                    "messenger_positions": ["messenger_home", "story"],
                    "audience_network_positions": ["classic", "rewarded_video"],
                },
                "image_label": {"name": "feed_img"},
                "is_default": True,
            },
        ],
    }
    creative = {
        "object_story_spec": object_story_spec,
        "asset_feed_spec": asset_feed_spec,
    }
    return _post_ad(name, adset_id, creative)


def create_carousel_ad(name: str, adset_id: str, adset_type: str, image_hashes: list[str], body: str,
                        instagram_user_id: str | None = None) -> str:
    """Создаёт карусель (2-10 изображений) для lead gen, возвращает ad_id.
    instagram_user_id — переопределение IG (для PRODB-кампании acme_prodb_ig)."""
    form = LEAD_FORMS[adset_type]
    page_link = f"https://www.facebook.com/{FB_PAGE_ID}"
    cta_type = _CAROUSEL_CTA_FALLBACK.get(form["cta"], form["cta"])

    cta = {
        "type": cta_type,
        "value": {
            "lead_gen_form_id": form["form_id"],
            "link": page_link,
        },
    }

    # CTA обязателен на КАЖДОЙ карточке + на уровне link_data
    child_attachments = []
    for image_hash in image_hashes:
        child_attachments.append({
            "image_hash": image_hash,
            "name": AD_TITLE,
            "link": page_link,
            "call_to_action": cta,
        })

    spec = {"page_id": FB_PAGE_ID}
    if instagram_user_id:
        spec["instagram_user_id"] = instagram_user_id
    spec["link_data"] = {
        "message": body,
        "link": page_link,
        "child_attachments": child_attachments,
        "call_to_action": cta,
        "multi_share_optimized": True,
    }
    return _post_ad(name, adset_id, {"object_story_spec": spec})


# ============================================================
# Website ads — для кампании "Owner | WEBSITE | MQL | v1"
# Не используют LEAD_FORMS — вместо этого ссылка на сайт компании (config.WEBSITE_LANDING_URL)
# ============================================================

_WEBSITE_INSTAGRAM_USER_ID = "11651524554970514"


# ============================================================
# Template-based ad creation — для онлайн-кабинета.
# Читает параметры (page, IG, Lead Form, CTA) из существующего
# объявления в адсете, чтобы новые объявления были консистентны.
# ============================================================


def get_adset_creative_template(adset_id: str) -> dict | None:
    """Читает первое объявление адсета и извлекает page/IG/Lead Form/CTA.

    Возвращает None если в адсете нет объявлений или у них нет нужных полей.
    Используется для запуска новых объявлений в уже настроенный адсет —
    чтобы новые креативы наследовали те же страница/форма что и существующие.
    """
    resp = _throttled_get(
        f"{API}/{adset_id}/ads",
        params={
            "access_token": get_fb_token(),
            "fields": "id,name,creative{object_story_spec,asset_feed_spec{call_to_actions}}",
            "limit": 1,
        },
    )
    if resp.status_code != 200:
        return None
    ads = resp.json().get("data", [])
    if not ads:
        return None

    cr = ads[0].get("creative") or {}
    spec = cr.get("object_story_spec") or {}
    page_id = spec.get("page_id")
    ig_user_id = spec.get("instagram_user_id")

    # CTA + Lead Form — могут быть в asset_feed_spec (Advantage+) или в link_data/video_data
    cta_type = None
    form_id = None
    afs = cr.get("asset_feed_spec") or {}
    if afs.get("call_to_actions"):
        cta = afs["call_to_actions"][0]
        cta_type = cta.get("type")
        form_id = (cta.get("value") or {}).get("lead_gen_form_id")
    else:
        ld = spec.get("link_data") or spec.get("video_data") or {}
        cta = ld.get("call_to_action") or {}
        cta_type = cta.get("type")
        form_id = (cta.get("value") or {}).get("lead_gen_form_id")

    if not (page_id and form_id and cta_type):
        return None

    return {
        "page_id": page_id,
        "instagram_user_id": ig_user_id,
        "lead_gen_form_id": form_id,
        "cta_type": cta_type,
    }


def create_video_ad_from_template(name: str, adset_id: str, video_id: str,
                                    image_hash: str, body: str, template: dict) -> str:
    """Создаёт видео Lead Form объявление с параметрами из template."""
    spec = {"page_id": template["page_id"]}
    if template.get("instagram_user_id"):
        spec["instagram_user_id"] = template["instagram_user_id"]
    spec["video_data"] = {
        "video_id": video_id,
        "image_hash": image_hash,
        "title": AD_TITLE,
        "message": body,
        "call_to_action": {
            "type": template["cta_type"],
            "value": {"lead_gen_form_id": template["lead_gen_form_id"]},
        },
    }
    return _post_ad(name, adset_id, {"object_story_spec": spec})


def create_image_ad_from_template(name: str, adset_id: str, image_hash: str,
                                    body: str, template: dict) -> str:
    """Создаёт image Lead Form объявление с параметрами из template."""
    spec = {"page_id": template["page_id"]}
    if template.get("instagram_user_id"):
        spec["instagram_user_id"] = template["instagram_user_id"]
    spec["link_data"] = {
        "image_hash": image_hash,
        "message": body,
        "name": AD_TITLE,
        "link": "https://example.com",
        "call_to_action": {
            "type": template["cta_type"],
            "value": {"lead_gen_form_id": template["lead_gen_form_id"]},
        },
    }
    return _post_ad(name, adset_id, {"object_story_spec": spec})


def create_carousel_ad_from_template(name: str, adset_id: str, image_hashes: list[str],
                                      body: str, template: dict) -> str:
    """Создаёт КАРУСЕЛЬ Lead Form объявление с параметрами из template (онлайн-кабинет).
    Наследует page/IG/Lead-форму/CTA из шаблона адсета (как image/video from_template)."""
    page_id = template["page_id"]
    page_link = f"https://www.facebook.com/{page_id}"
    cta = {
        "type": template["cta_type"],
        "value": {"lead_gen_form_id": template["lead_gen_form_id"], "link": page_link},
    }
    child_attachments = [
        {"image_hash": h, "name": AD_TITLE, "link": page_link, "call_to_action": cta}
        for h in image_hashes
    ]
    spec = {"page_id": page_id}
    if template.get("instagram_user_id"):
        spec["instagram_user_id"] = template["instagram_user_id"]
    spec["link_data"] = {
        "message": body,
        "link": page_link,
        "child_attachments": child_attachments,
        "call_to_action": cta,
        "multi_share_optimized": True,
    }
    return _post_ad(name, adset_id, {"object_story_spec": spec})


def _website_link(city: str, content_tag: str) -> str:
    """Формирует UTM-ссылку для website-кампании."""
    from config import CITY_UTM_CODES, WEBSITE_LANDING_URL
    code = CITY_UTM_CODES.get(city, city.lower())
    return (
        f"{WEBSITE_LANDING_URL}"
        f"?utm_source=facebook&utm_medium=cpc"
        f"&utm_campaign=traffic_cabinet_a_{code}"
        f"&utm_content={content_tag}"
    )


def create_website_video_ad(name: str, adset_id: str, video_id: str,
                            image_hash: str, body: str, city: str,
                            content_tag: str) -> str:
    """Видео-объявление для website-кампании (трафик на сайт)."""
    link = _website_link(city, content_tag)
    creative = {
        "object_story_spec": {
            "page_id": FB_PAGE_ID,
            "instagram_user_id": _WEBSITE_INSTAGRAM_USER_ID,
            "video_data": {
                "video_id": video_id,
                "image_hash": image_hash,
                "title": AD_TITLE,
                "message": body,
                "call_to_action": {"type": "LEARN_MORE", "value": {"link": link}},
            },
        }
    }
    return _post_ad(name, adset_id, creative)


def create_website_image_ad(name: str, adset_id: str, image_hash: str,
                            body: str, city: str, content_tag: str) -> str:
    """Image-объявление для website-кампании."""
    link = _website_link(city, content_tag)
    creative = {
        "object_story_spec": {
            "page_id": FB_PAGE_ID,
            "instagram_user_id": _WEBSITE_INSTAGRAM_USER_ID,
            "link_data": {
                "image_hash": image_hash,
                "link": link,
                "message": body,
                "name": AD_TITLE,
                "call_to_action": {"type": "LEARN_MORE", "value": {"link": link}},
            },
        }
    }
    return _post_ad(name, adset_id, creative)


def _inventory_payload(response, context: str) -> dict:
    """Валидирует FB response без утечки raw body/токенов."""
    if response.status_code != 200:
        raise AdsetInventoryError(f"{context}_http_{response.status_code}")
    try:
        payload = response.json()
    except Exception as exc:
        raise AdsetInventoryError(f"{context}_json_invalid") from exc
    if not isinstance(payload, dict):
        raise AdsetInventoryError(f"{context}_payload_not_object")
    return payload


def _fetch_all_adset_ads(adset_id: str) -> list[dict]:
    """Читает все страницы ads; любая неполнота делает snapshot недоверенным."""
    import time as _time

    ads: list[dict] = []
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    after: str | None = None

    for page_number in range(1, _MAX_ADSET_INVENTORY_PAGES + 1):
        params = {
            "access_token": get_fb_token(),
            "fields": (
                "id,name,status,effective_status,created_time,adset_id,creative{id}"
            ),
            "limit": 100,
        }
        if after is not None:
            params["after"] = after

        response = None
        for attempt in range(2):
            response = _throttled_get(f"{API}/{adset_id}/ads", params=params)
            if response.status_code == 200:
                break
            if attempt == 0:
                _time.sleep(1)
        payload = _inventory_payload(response, "adset_ads")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise AdsetInventoryError("adset_ads_data_not_list")

        for row in rows:
            if type(row) is not dict:
                raise AdsetInventoryError("adset_ads_row_not_object")
            row_id = row.get("id")
            row_adset_id = row.get("adset_id")
            name = row.get("name")
            status = row.get("status")
            effective_status = row.get("effective_status")
            created_time = row.get("created_time")
            creative = row.get("creative")
            creative_id = creative.get("id") if isinstance(creative, dict) else None
            if (
                type(row_id) is not str
                or not row_id
                or type(row_adset_id) is not str
                or row_adset_id != adset_id
                or type(name) is not str
                or type(status) is not str
                or not status
                or type(effective_status) is not str
                or not effective_status
                or type(created_time) is not str
                or not created_time
                or (
                    effective_status not in _RETIRED_AD_EFFECTIVE_STATUSES
                    and (type(creative_id) is not str or not creative_id.isdigit())
                )
            ):
                raise AdsetInventoryError("adset_ads_row_incomplete")
            if row_id in seen_ids:
                raise AdsetInventoryError("adset_ads_duplicate_id")
            seen_ids.add(row_id)
            ads.append(dict(row))

        paging = payload.get("paging")
        if paging is None:
            return ads
        if not isinstance(paging, dict):
            raise AdsetInventoryError("adset_ads_paging_invalid")
        if not paging.get("next"):
            return ads
        cursors = paging.get("cursors")
        next_after = cursors.get("after") if isinstance(cursors, dict) else None
        if not next_after:
            raise AdsetInventoryError("adset_ads_paging_cursor_missing")
        next_after = str(next_after)
        if next_after in seen_cursors:
            raise AdsetInventoryError("adset_ads_paging_cursor_repeated")
        if page_number == _MAX_ADSET_INVENTORY_PAGES:
            raise AdsetInventoryError("adset_ads_page_limit_exceeded")
        seen_cursors.add(next_after)
        after = next_after

    raise AdsetInventoryError("adset_ads_page_limit_exceeded")


def get_adset_info(adset_id: str) -> dict:
    """Полный fail-closed snapshot адсета с exact effective ACTIVE count."""
    if type(adset_id) is not str or not adset_id:
        raise AdsetInventoryError("adset_id_invalid")
    response = _throttled_get(
        f"{API}/{adset_id}",
        params={
            "access_token": get_fb_token(),
            "fields": "daily_budget,name,effective_status,account_id",
        },
    )
    adset_data = _inventory_payload(response, "adset")
    name = adset_data.get("name")
    adset_effective_status = adset_data.get("effective_status")
    account_id = str(adset_data.get("account_id") or "").removeprefix("act_")
    if (
        type(name) is not str
        or type(adset_effective_status) is not str
        or not account_id.isdigit()
    ):
        raise AdsetInventoryError("adset_name_invalid")
    try:
        daily_budget_cents = int(adset_data.get("daily_budget", 0))
    except (TypeError, ValueError) as exc:
        raise AdsetInventoryError("adset_daily_budget_invalid") from exc
    if daily_budget_cents < 0:
        raise AdsetInventoryError("adset_daily_budget_invalid")

    fetched_ads = _fetch_all_adset_ads(adset_id)
    unknown_effective_status_ids = [
        ad["id"]
        for ad in fetched_ads
        if ad["effective_status"] not in _KNOWN_AD_EFFECTIVE_STATUSES
    ]
    ads = [
        ad
        for ad in fetched_ads
        if ad["effective_status"] not in _RETIRED_AD_EFFECTIVE_STATUSES
    ]
    effective_active_ids = [
        ad["id"] for ad in ads if ad["effective_status"] == "ACTIVE"
    ]
    return {
        "adset_id": adset_id,
        "account_id": account_id,
        "name": name,
        "adset_effective_status": adset_effective_status,
        "daily_budget": daily_budget_cents / 100,
        "ad_count": len(ads),
        "ads": ads,
        "ad_ids": [ad["id"] for ad in ads],
        "effective_active_ids": effective_active_ids,
        "effective_active_count": len(effective_active_ids),
        "unknown_effective_status_ids": unknown_effective_status_ids,
        "fetched_ad_count": len(fetched_ads),
        "inventory_complete": True,
    }


def get_adset_capacity(adset_id: str, stale_days: int = 15) -> dict:
    """Рекомендуемое количество креативов и свободные слоты.

    stale_days: PAUSED-объявления возрастом N+ полных дней считаются «стейл».
    """
    info = get_adset_info(adset_id)
    recommended = int(info["daily_budget"] / COST_PER_CREATIVE) if info["daily_budget"] > 0 else 0
    available = MAX_ADS_PER_ADSET - info["ad_count"]

    # Стейл объявления: configured PAUSED, безопасный effective status и возраст.
    cutoff = datetime.now().astimezone() - timedelta(days=stale_days)
    stale_ads = []
    for ad in info["ads"]:
        if (
            ad["status"] == "PAUSED"
            and ad["effective_status"] in _SAFE_CLEANUP_EFFECTIVE_STATUSES
        ):
            created = datetime.fromisoformat(ad["created_time"].replace("+0000", "+00:00"))
            if created <= cutoff:
                stale_ads.append(ad)

    # Сортируем: самые старые первые
    stale_ads.sort(key=lambda a: a["created_time"])

    return {
        "adset_id": adset_id,
        "name": info["name"],
        "adset_effective_status": info["adset_effective_status"],
        "daily_budget": info["daily_budget"],
        "ad_count": info["ad_count"],
        "max_ads": MAX_ADS_PER_ADSET,
        "available": available,
        "recommended": recommended,
        "stale_ads": stale_ads,
        "ad_ids": info["ad_ids"],
        "effective_active_ids": info["effective_active_ids"],
        "effective_active_count": info["effective_active_count"],
        "unknown_effective_status_ids": info["unknown_effective_status_ids"],
        "fetched_ad_count": info["fetched_ad_count"],
        "inventory_complete": info["inventory_complete"],
    }


def _send_cleanup_recovery_alert(adset_id: str, reason: str) -> None:
    """Критический alert не может открыть DELETE при своей ошибке."""
    try:
        from services.notifications import send_critical_alert

        send_critical_alert(
            "CLEANUP заблокирован: нужно восстановление ACTIVE",
            f"Adset: {_safe_error(adset_id)}\nПричина: {_safe_error(reason)}\nDELETE не выполнялся.",
        )
    except Exception as exc:
        logger.error("cleanup guard alert failed: %s", type(exc).__name__)


def _validated_cleanup_capacity(adset_id: str, stale_days: int) -> dict:
    """Возвращает полный live snapshot, пригодный для проверки DELETE."""
    try:
        capacity = get_adset_capacity(adset_id, stale_days=stale_days)
    except Exception as exc:
        logger.error("cleanup inventory unavailable for %s: %s", adset_id, type(exc).__name__)
        _send_cleanup_recovery_alert(adset_id, "inventory_unknown")
        raise CleanupGuardError("cleanup_inventory_unknown") from exc

    ad_ids = capacity.get("ad_ids")
    active_ids = capacity.get("effective_active_ids")
    active_count = capacity.get("effective_active_count")
    available = capacity.get("available")
    unknown_status_ids = capacity.get("unknown_effective_status_ids")
    fetched_ad_count = capacity.get("fetched_ad_count")
    if (
        capacity.get("adset_effective_status") != "ACTIVE"
        or
        capacity.get("inventory_complete") is not True
        or type(ad_ids) is not list
        or any(type(ad_id) is not str or not ad_id for ad_id in ad_ids)
        or len(set(ad_ids)) != len(ad_ids)
        or capacity.get("ad_count") != len(ad_ids)
        or type(available) is not int
        or available != MAX_ADS_PER_ADSET - len(ad_ids)
        or not 0 <= available <= MAX_ADS_PER_ADSET
        or type(active_ids) is not list
        or any(type(ad_id) is not str or not ad_id for ad_id in active_ids)
        or len(set(active_ids)) != len(active_ids)
        or not set(active_ids).issubset(set(ad_ids))
        or type(active_count) is not int
        or active_count != len(active_ids)
        or type(unknown_status_ids) is not list
        or any(type(ad_id) is not str or not ad_id for ad_id in unknown_status_ids)
        or len(set(unknown_status_ids)) != len(unknown_status_ids)
        or not set(unknown_status_ids).issubset(set(ad_ids))
        or type(fetched_ad_count) is not int
        or fetched_ad_count < len(ad_ids)
    ):
        _send_cleanup_recovery_alert(adset_id, "inventory_malformed")
        raise CleanupGuardError("cleanup_inventory_malformed")
    if unknown_status_ids:
        _send_cleanup_recovery_alert(adset_id, "unknown_effective_status")
        raise CleanupGuardError("cleanup_inventory_unknown_effective_status")
    if active_count < 1:
        _send_cleanup_recovery_alert(adset_id, "effective_active_zero")
        raise CleanupGuardError("cleanup_recovery_needed_no_active")

    stale_ads = capacity.get("stale_ads")
    if (
        type(stale_ads) is not list
        or any(type(ad) is not dict for ad in stale_ads)
        or any(type(ad.get("status")) is not str or ad["status"] != "PAUSED" for ad in stale_ads)
        or any(
            type(ad.get("effective_status")) is not str
            or ad["effective_status"] not in _SAFE_CLEANUP_EFFECTIVE_STATUSES
            for ad in stale_ads
        )
        or any(type(ad.get("adset_id")) is not str or ad["adset_id"] != adset_id for ad in stale_ads)
    ):
        _send_cleanup_recovery_alert(adset_id, "stale_inventory_malformed")
        raise CleanupGuardError("cleanup_inventory_malformed")
    stale_ids = [ad.get("id") for ad in stale_ads]
    if (
        any(type(ad_id) is not str or not ad_id for ad_id in stale_ids)
        or len(set(stale_ids)) != len(stale_ids)
        or not set(stale_ids).issubset(set(ad_ids))
    ):
        _send_cleanup_recovery_alert(adset_id, "stale_inventory_malformed")
        raise CleanupGuardError("cleanup_inventory_malformed")

    return dict(capacity)


def _fetch_cleanup_lifetime_zero(ad_id: str) -> dict[str, float | int]:
    """Читает exact lifetime delivery evidence прямо перед DELETE."""
    response = _throttled_get(
        f"{API}/{ad_id}/insights",
        params={
            "access_token": get_fb_token(),
            "date_preset": "maximum",
            "fields": "spend,impressions,clicks",
            "limit": 2,
        },
    )
    payload = _inventory_payload(response, "cleanup_lifetime_insights")
    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise CleanupGuardError("cleanup_lifetime_insights_ambiguous")
    row = rows[0]
    try:
        spend = float(row["spend"])
        impressions = int(row["impressions"])
        clicks = int(row["clicks"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CleanupGuardError("cleanup_lifetime_insights_invalid") from exc
    if (
        not math.isfinite(spend)
        or spend != 0
        or impressions != 0
        or clicks != 0
    ):
        raise CleanupGuardError("cleanup_lifetime_insights_nonzero")
    return {"spend": spend, "impressions": impressions, "clicks": clicks}


def get_cleanup_capacity(
    adset_id: str,
    stale_days: int = 15,
    *,
    candidate_id: str,
) -> dict:
    """Выдаёт одноразовую DELETE-evidence для одного exact candidate."""
    if type(adset_id) is not str or not adset_id:
        raise CleanupGuardError("cleanup_adset_id_invalid")
    if type(candidate_id) is not str or not candidate_id:
        raise CleanupGuardError("cleanup_candidate_id_invalid")
    guarded = _validated_cleanup_capacity(adset_id, stale_days)
    stale_ids = [ad["id"] for ad in guarded["stale_ads"]]
    if candidate_id not in stale_ids:
        raise CleanupGuardError("cleanup_candidate_not_live_stale")

    guarded["cleanup_guard_evidence"] = _mint_cleanup_guard_evidence(
        adset_id=adset_id,
        stale_days=stale_days,
        candidate_id=candidate_id,
        total_ids=frozenset(guarded["ad_ids"]),
        effective_active_ids=frozenset(guarded["effective_active_ids"]),
    )
    return guarded


def _canonicalize_cleanup_candidates(
    stale_ads: object,
    count: int,
) -> tuple[tuple[str, str], str | None]:
    """Копирует один недоверенный candidate в неизменяемые значения."""
    if type(stale_ads) is not list or len(stale_ads) != 1 or count != 1:
        raise CleanupGuardError("cleanup_candidates_invalid")

    candidate = stale_ads[0]
    if type(candidate) is not dict:
        raise CleanupGuardError("cleanup_candidates_invalid")
    candidate_id = candidate.get("id")
    if type(candidate_id) is not str or not candidate_id:
        raise CleanupGuardError("cleanup_candidates_invalid")
    candidate_name = candidate.get("name")
    safe_name = (
        _safe_error(candidate_name) if type(candidate_name) is str else candidate_id
    )
    explicit_adset_id = None
    if "adset_id" in candidate:
        explicit_adset_id = candidate.get("adset_id")
        if type(explicit_adset_id) is not str or not explicit_adset_id:
            raise CleanupGuardError("cleanup_candidates_invalid")
    return (candidate_id, safe_name), explicit_adset_id


def cleanup_stale_ads(
    stale_ads: list,
    count: int,
    *,
    guard_evidence: CleanupGuardEvidence,
    authorization: CleanupDeleteAuthorization,
) -> list:
    """Блокирует DELETE/ARCHIVE: для операции нет typed owner contract."""

    del stale_ads, count, guard_evidence, authorization
    from integrations.facebook_ads_mutation_transport import ForbiddenMutation

    raise ForbiddenMutation("DELETE_ARCHIVE_OPERATION_FORBIDDEN")

    """
    if type(count) is not int or count != 1:
        raise CleanupGuardError("cleanup_count_invalid")
    canonical_candidate, explicit_adset_id = _canonicalize_cleanup_candidates(
        stale_ads,
        count,
    )
    candidate_id, safe_name = canonical_candidate
    bound_evidence = _consume_cleanup_guard_evidence(
        guard_evidence,
        candidate_id,
    )
    if (
        explicit_adset_id is not None
        and explicit_adset_id != bound_evidence.adset_id
    ):
        raise CleanupGuardError("cleanup_guard_adset_binding_mismatch")
    if (
        not isinstance(authorization, CleanupDeleteAuthorization)
        or authorization.adset_id != bound_evidence.adset_id
        or authorization.ad_id != candidate_id
        or authorization.purpose != "REPLACEMENT_SLOT"
        or not authorization.claim_id
        or not authorization.workflow_id
    ):
        raise CleanupAuthorizationError("cleanup_authorization_binding_mismatch")
    from services.adset_pause_guard import adset_mutation_lock

    with adset_mutation_lock(bound_evidence.adset_id):
        final_capacity = _validated_cleanup_capacity(
            bound_evidence.adset_id,
            bound_evidence.stale_days,
        )
        final_stale_by_id = {
            ad["id"]: ad
            for ad in final_capacity["stale_ads"]
        }
        if candidate_id not in final_stale_by_id:
            raise CleanupGuardError("cleanup_candidate_not_live_stale")

        final_active_ids = frozenset(final_capacity["effective_active_ids"])
        if not final_active_ids:
            _send_cleanup_recovery_alert(bound_evidence.adset_id, "effective_active_zero")
            raise CleanupGuardError("cleanup_recovery_needed_no_active")
        if candidate_id in final_active_ids:
            raise CleanupGuardError("cleanup_candidate_is_active")

        final_total_ids = frozenset(final_capacity["ad_ids"])
        if (
            final_total_ids != bound_evidence.total_ids
            or final_active_ids != bound_evidence.effective_active_ids
        ):
            raise CleanupGuardError("cleanup_inventory_drifted_before_delete")
        if len(final_total_ids - {candidate_id}) < 1:
            _send_cleanup_recovery_alert(bound_evidence.adset_id, "last_total_ad")
            raise CleanupGuardError("cleanup_last_total_ad_blocked")

        lifetime_zero = _fetch_cleanup_lifetime_zero(candidate_id)
        authorization_claim_id = getattr(authorization, "claim_id", "")
        authorization_workflow_id = getattr(authorization, "workflow_id", "")
        try:
            (
                fresh_local,
                fresh_local_hash,
                runtime_config_identity,
            ) = _prepare_current_cleanup_delete_boundary(
                claim_id=authorization_claim_id,
                workflow_id=authorization_workflow_id,
                adset_id=bound_evidence.adset_id,
                ad_id=candidate_id,
            )
        except Exception as exc:
            # До HTTP-start capability можно и нужно отозвать: повтор с тем же
            # claim после drift/fresh-evidence failure запрещён.
            from services.cleanup_authorization import revoke_delete_authorization

            try:
                revoke_delete_authorization(authorization_claim_id)
            except Exception as revoke_error:
                logger.error(
                    "cleanup authorization revoke failed: %s",
                    type(revoke_error).__name__,
                )
            raise CleanupGuardError("cleanup_delete_boundary_not_authorized") from exc
        consume_delete_authorization(
            authorization,
            claim_id=authorization_claim_id,
            workflow_id=authorization_workflow_id,
            adset_id=bound_evidence.adset_id,
            ad_id=candidate_id,
            live_capacity_before=final_capacity["available"],
        )

        def require_reconcile(error_code: str, cause: Exception | None = None):
            try:
                finish_cleanup_delete(
                    authorization_claim_id,
                    "RECONCILE_REQUIRED",
                    capacity_after=None,
                    evidence={
                        "candidate_id": candidate_id,
                        "live_lifetime": lifetime_zero,
                        "fresh_local_zero": fresh_local,
                        "fresh_local_zero_sha256": fresh_local_hash,
                        "runtime_config": runtime_config_identity,
                        "http_started": True,
                    },
                    error=error_code,
                )
            except Exception as state_error:
                logger.critical(
                    "cleanup reconcile state update failed: %s",
                    type(state_error).__name__,
                )
            reconcile_error = CleanupDeleteReconcileRequired(
                "cleanup_delete_reconcile_required"
            )
            if cause is None:
                raise reconcile_error
            raise reconcile_error from cause

        raise ForbiddenMutation("DELETE_ARCHIVE_OPERATION_FORBIDDEN")
        status_code = getattr(response, "status_code", None)
        if type(status_code) is not int or status_code != 200:
            require_reconcile("cleanup_delete_http_ambiguous")
        try:
            payload = response.json()
        except Exception as exc:
            require_reconcile("cleanup_delete_response_invalid", exc)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            require_reconcile("cleanup_delete_response_invalid")
        try:
            post_capacity = _validated_cleanup_capacity(
                bound_evidence.adset_id,
                bound_evidence.stale_days,
            )
        except Exception as exc:
            require_reconcile("cleanup_delete_postcheck_unavailable", exc)
        expected_total_ids = bound_evidence.total_ids - {candidate_id}
        if (
            candidate_id in post_capacity["ad_ids"]
            or frozenset(post_capacity["ad_ids"]) != expected_total_ids
            or post_capacity["effective_active_count"] < 1
        ):
            require_reconcile("cleanup_delete_postcondition_failed")
        finish_cleanup_delete(
            authorization_claim_id,
            "DELETED",
            capacity_after=post_capacity["available"],
            evidence={
                "candidate_absent": True,
                "active_count": post_capacity["effective_active_count"],
                "live_lifetime": lifetime_zero,
                "fresh_local_zero": fresh_local,
                "fresh_local_zero_sha256": fresh_local_hash,
                "runtime_config": runtime_config_identity,
                "inventory_complete": True,
            },
            error=None,
        )
        print(f"   🗑 Удалено: {safe_name} ({candidate_id})")
        return [candidate_id]
    """


def cleanup_all_adsets() -> dict:
    """Прямой cleanup без workflow навсегда отключён."""
    raise CleanupGuardError("direct_cleanup_disabled")


def launch_creative(card_name: str, adset_type: str, media: dict, body: str,
                    campaign_type: str = "leadgen",
                    cities: list[str] | None = None,
                    progress_cb=None,
                    product: str | None = None,
                    prepare_city_cb=None,
                    city_success_cb=None,
                    authorization: ProviderLaunchAuthorization | None = None) -> dict:
    """Legacy raw-media entrypoint навсегда закрыт.

    Provider CREATE разрешён только private executor-у immutable
    ``LaunchManifest`` после durable permit в Approval Gateway.
    """
    del (
        card_name,
        adset_type,
        media,
        body,
        campaign_type,
        cities,
        progress_cb,
        product,
        prepare_city_cb,
        city_success_cb,
        authorization,
    )
    raise LaunchCheckBlocked(
        "LEGACY_LAUNCH_BYPASS_FORBIDDEN",
        ("Raw media/callback launch запрещён; требуется sealed Action Gateway",),
        None,
    )


def _launch_authorized_creative(
    card_name: str,
    adset_type: str,
    media: dict,
    body: str,
    campaign_type: str,
    cities: list[str] | None,
    progress_cb,
    product: str | None,
    prepare_city_cb,
    city_success_cb,
    authorization: ProviderLaunchAuthorization,
    validation,
    media_sha256: str,
    prepared_media: tuple,
) -> dict:
    """Удалённый legacy executor: raw proof/media/callback всегда запрещён."""
    del (
        card_name,
        adset_type,
        media,
        body,
        campaign_type,
        cities,
        progress_cb,
        product,
        prepare_city_cb,
        city_success_cb,
        authorization,
        validation,
        media_sha256,
        prepared_media,
    )
    raise LaunchCheckBlocked(
        "LEGACY_LAUNCH_BYPASS_FORBIDDEN",
        ("Raw authorized executor удалён; требуется immutable LaunchManifest",),
        None,
    )


def _format_planned_name(base_name: str, product: str | None) -> str:
    return format_ad_name(base_name, product) if product else base_name


def _expected_city_ad_names(
    city: str,
    card_name: str,
    media_type: str,
    video_assets: list[dict],
    image_assets: list[dict],
    placement_pairs: list[dict],
    single_image_assets: list[dict],
    product: str | None,
) -> list[str]:
    """Строит те же exact names, которые ниже передаются в CREATE."""
    if media_type == "video":
        bases = [
            f"{city} | {card_name}" if len(video_assets) == 1
            else f"{city} | {card_name} / {asset['label']}"
            for asset in video_assets
        ]
    elif media_type == "image":
        bases = [
            f"{city} | {card_name}" if len(image_assets) == 1
            else f"{city} | {card_name} / {asset['label']}"
            for asset in image_assets
        ]
    elif media_type == "carousel":
        bases = [f"{city} | {card_name}"]
    elif media_type == "placement_pairs":
        bases = [f"{city} | {card_name} / {pair['label']}" for pair in placement_pairs]
        bases.extend(
            f"{city} | {card_name} / {asset['label']}"
            for asset in single_image_assets
        )
    else:
        bases = []
    return [_format_planned_name(base, product) for base in bases]


def _canonical_launch_media(
    media: dict,
) -> tuple[str, list, list[dict], tuple[tuple[str, str], ...]]:
    """Нормализует exact порядок CREATE и безопасные относительные identity."""
    if not isinstance(media, dict):
        raise LaunchMediaBindingError("launch_media_payload_invalid")
    media_type = media.get("type")
    raw_paths = media.get("paths")
    if media_type not in {"video", "image", "carousel", "placement_pairs"}:
        raise LaunchMediaBindingError("launch_media_type_invalid")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise LaunchMediaBindingError("launch_media_paths_invalid")

    bindings: list[tuple[str, str]] = []
    singles: list[dict] = []
    if media_type == "placement_pairs":
        paths: list[dict] = []
        for raw_pair in raw_paths:
            if not isinstance(raw_pair, dict):
                raise LaunchMediaBindingError("launch_media_pair_invalid")
            label = raw_pair.get("label")
            feed = raw_pair.get("feed")
            story = raw_pair.get("story")
            if not all(isinstance(value, str) and value.strip() for value in (label, feed, story)):
                raise LaunchMediaBindingError("launch_media_pair_invalid")
            paths.append({"label": label.strip(), "feed": feed, "story": story})
        paths.sort(key=lambda pair: pair["label"])

        raw_singles = media.get("singles", [])
        if not isinstance(raw_singles, list):
            raise LaunchMediaBindingError("launch_media_singles_invalid")
        for raw_single in raw_singles:
            if not isinstance(raw_single, dict):
                raise LaunchMediaBindingError("launch_media_single_invalid")
            label = raw_single.get("label")
            path = raw_single.get("path")
            if not isinstance(label, str) or not label.strip() or not isinstance(path, str) or not path:
                raise LaunchMediaBindingError("launch_media_single_invalid")
            singles.append({"label": label.strip(), "path": path})
        singles.sort(key=lambda single: single["label"])

        for pair in paths:
            bindings.extend(
                (
                    (f"{pair['label']}.feed:{Path(pair['feed']).name}", pair["feed"]),
                    (f"{pair['label']}.story:{Path(pair['story']).name}", pair["story"]),
                )
            )
        bindings.extend(
            (f"{single['label']}.single:{Path(single['path']).name}", single["path"])
            for single in singles
        )
    else:
        if any(not isinstance(path, str) or not path for path in raw_paths):
            raise LaunchMediaBindingError("launch_media_path_invalid")
        paths = sorted(raw_paths, key=lambda value: Path(value).name)
        if media_type == "carousel":
            paths = paths[:10]
        bindings.extend((Path(path).name, path) for path in paths)

    relative_names = [relative_name for relative_name, _path in bindings]
    if (
        not relative_names
        or any(not relative_name for relative_name in relative_names)
        or len(relative_names) != len(set(relative_names))
    ):
        raise LaunchMediaBindingError("launch_media_identity_invalid")
    return media_type, paths, singles, tuple(bindings)


def _hash_launch_media_files(
    bindings: tuple[tuple[str, str], ...],
) -> tuple[LaunchMediaFileEvidence, ...]:
    """Читает exact bytes без сохранения абсолютных путей в evidence."""
    evidence: list[LaunchMediaFileEvidence] = []
    for relative_name, raw_path in bindings:
        digest = hashlib.sha256()
        bytes_read = 0
        try:
            with open(raw_path, "rb") as media_file:
                before = os.fstat(media_file.fileno())
                while chunk := media_file.read(1024 * 1024):
                    digest.update(chunk)
                    bytes_read += len(chunk)
                after = os.fstat(media_file.fileno())
        except OSError as exc:
            raise LaunchMediaBindingError("launch_media_unreadable") from exc
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_identity != after_identity or bytes_read != after.st_size:
            raise LaunchMediaBindingError("launch_media_changed_during_read")
        if bytes_read <= 0:
            raise LaunchMediaBindingError("launch_media_empty")
        evidence.append(
            LaunchMediaFileEvidence(
                relative_name=relative_name,
                size=bytes_read,
                content_sha256=digest.hexdigest(),
            )
        )
    return tuple(evidence)


def _launch_media_sha256_from_evidence(
    media_type: str,
    files: tuple[LaunchMediaFileEvidence, ...],
) -> str:
    payload = {
        "media_type": media_type,
        "files": [
            {
                "relative_name": item.relative_name,
                "size": item.size,
                "content_sha256": item.content_sha256,
            }
            for item in files
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _prepare_launch_media(media: dict) -> tuple:
    media_type, paths, singles, bindings = _canonical_launch_media(media)
    files = _hash_launch_media_files(bindings)
    return media_type, paths, singles, bindings, files


def calculate_launch_media_sha256(media: dict) -> str:
    """Возвращает server-owned SHA exact ordered local media bytes."""
    prepared = _prepare_launch_media(media)
    return _launch_media_sha256_from_evidence(prepared[0], prepared[4])


def _staged_manifest_directory(manifest: LaunchManifest) -> Path:
    root = Path(manifest.staging_root)
    directory = Path(manifest.staging_directory)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_directory = directory.resolve(strict=True)
    except OSError as exc:
        raise LaunchCheckBlocked(
            "LAUNCH_INPUT_DRIFT",
            ("Staged launch directory недоступна",),
            manifest.manifest_id,
        ) from exc
    # Exact claim несёт manifest_id «база:N», а staging опубликован под базовым
    # id — сверяем директорию с базовым (регрессия LAUNCH_STAGING_DRIFT).
    from services.launch_staging import base_manifest_id

    if (
        root.is_symlink()
        or directory.is_symlink()
        or resolved_directory.parent != resolved_root
        or resolved_directory.name != base_manifest_id(manifest.manifest_id)
    ):
        raise LaunchCheckBlocked(
            "LAUNCH_INPUT_DRIFT",
            ("Staged launch path вышел за immutable root",),
            manifest.manifest_id,
        )
    return resolved_directory


def _verify_staged_manifest_files(manifest: LaunchManifest) -> Path:
    """Перехеширует все media/text до первого provider upload."""

    from services.approval_source_media import rehash_staged_files

    directory = _staged_manifest_directory(manifest)
    _verify_staging_marker(manifest, directory)
    media_paths = tuple(asset.staged_relative_path for asset in manifest.media_assets)
    try:
        media_digests = rehash_staged_files(media_paths, staging_root=directory)
    except Exception as exc:
        raise LaunchCheckBlocked(
            "LAUNCH_INPUT_DRIFT",
            ("Staged media bytes нельзя подтвердить",),
            manifest.manifest_id,
        ) from exc
    actual_media = tuple(
        (item.order_index, item.size_bytes, item.content_sha256)
        for item in media_digests
    )
    expected_media = tuple(
        (asset.order_index, asset.size_bytes, asset.content_sha256)
        for asset in manifest.media_assets
    )
    if actual_media != expected_media:
        raise LaunchCheckBlocked(
            "LAUNCH_INPUT_DRIFT",
            ("Staged media bytes изменились",),
            manifest.manifest_id,
        )
    body_contract: dict[str, str] = {}
    for destination in manifest.destinations:
        for creative in destination.creatives:
            previous = body_contract.setdefault(
                creative.body_staged_relative_path,
                creative.body_sha256,
            )
            if previous != creative.body_sha256:
                raise LaunchCheckBlocked(
                    "LAUNCH_INPUT_DRIFT",
                    ("Один staged body связан с разными hashes",),
                    manifest.manifest_id,
                )
    try:
        body_digests = rehash_staged_files(
            tuple(body_contract),
            staging_root=directory,
        )
    except Exception as exc:
        raise LaunchCheckBlocked(
            "LAUNCH_INPUT_DRIFT",
            ("Staged ad text нельзя подтвердить",),
            manifest.manifest_id,
        ) from exc
    if any(
        digest.normalized_text_sha256 != body_contract[digest.relative_path]
        for digest in body_digests
    ):
        raise LaunchCheckBlocked(
            "LAUNCH_INPUT_DRIFT",
            ("Staged ad text изменился",),
            manifest.manifest_id,
        )
    return directory


def _verify_staging_marker(manifest: LaunchManifest, directory: Path) -> None:
    """Доказывает, что execution видит завершённый exact staging publish."""

    from services.approval_source_media import _open_safe_file, _safe_relative_path

    try:
        descriptor, _path, marker_info = _open_safe_file(
            directory,
            _safe_relative_path("manifest.complete"),
        )
        chunks: list[bytes] = []
        try:
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RuntimeError) as exc:
        raise LaunchCheckBlocked(
            "LAUNCH_INPUT_DRIFT",
            ("Staging complete marker недоступен",),
            manifest.manifest_id,
        ) from exc
    if marker_info.st_nlink != 1 or not isinstance(payload, dict):
        raise LaunchCheckBlocked(
            "LAUNCH_INPUT_DRIFT",
            ("Staging complete marker небезопасен",),
            manifest.manifest_id,
        )
    marker_contract = {
        "manifest_id": payload.get("manifest_id"),
        "trello": payload.get("trello"),
        "card_name_sha256": payload.get("card_name_sha256"),
        "campaign_type": payload.get("campaign_type"),
        "config_version_sha256": payload.get("config_version_sha256"),
        "media_assets": payload.get("media_assets"),
        "destinations": payload.get("destinations"),
        "media_manifest_sha256": payload.get("media_manifest_sha256"),
    }
    expected_contract = {
        "manifest_id": manifest.manifest_id,
        "trello": manifest.trello,
        "card_name_sha256": manifest.card_name_sha256,
        "campaign_type": manifest.campaign_type,
        "config_version_sha256": manifest.config_version_sha256,
        "media_assets": manifest.media_assets,
        "destinations": manifest.destinations,
        "media_manifest_sha256": manifest.media_manifest_sha256,
    }
    from services.launch_staging import base_manifest_id, marker_matches_claim

    if manifest.manifest_id == base_manifest_id(manifest.manifest_id):
        # Полный манифест: байт-в-байт, как раньше.
        marker_ok = canonical_json(marker_contract) == canonical_json(expected_contract)
    else:
        # Exact claim «база:N»: его destination/creative — подмножество marker.
        marker_ok = marker_matches_claim(marker_contract, expected_contract)
    if not marker_ok:
        raise LaunchCheckBlocked(
            "LAUNCH_INPUT_DRIFT",
            ("Staging marker не совпадает с immutable manifest",),
            manifest.manifest_id,
        )


def _validate_launch_manifest_scope(manifest: LaunchManifest) -> None:
    """Проверяет exact ordering/capacity/status до первого asset upload."""

    assets_by_id = {asset.asset_id: asset for asset in manifest.media_assets}
    if [asset.order_index for asset in manifest.media_assets] != list(
        range(len(manifest.media_assets))
    ):
        raise LaunchCheckBlocked(
            "INVALID_LAUNCH_MANIFEST",
            ("Media order_index должен быть непрерывным",),
            manifest.manifest_id,
        )
    from services.launch_staging import base_manifest_id

    # Exact claim «база:N» (agent/launcher._owner_launch_plan) несёт ОДИН creative города с его
    # исходным order_index: второй креатив города приходит с индексом 1, третий — 2. Требование
    # «индексы с нуля подряд» проверяется на полном манифесте при staging; на claim оно ложно
    # отвергало всё, кроме первого креатива города (карточка с несколькими креативами на город
    # запускалась частично — INVALID_LAUNCH_MANIFEST «Creative order/capacity»).
    is_claim = manifest.manifest_id != base_manifest_id(manifest.manifest_id)
    seen_scopes: set[tuple[str, str]] = set()
    for destination in manifest.destinations:
        creatives = tuple(
            sorted(destination.creatives, key=lambda item: item.order_index)
        )
        order = [creative.order_index for creative in creatives]
        order_ok = (
            len(set(order)) == len(order) and all(index >= 0 for index in order)
            if is_claim
            else order == list(range(len(creatives)))
        )
        if (
            not order_ok
            or destination.capacity_available
            < len(creatives) + destination.hard_reserve_slots
        ):
            raise LaunchCheckBlocked(
                "INVALID_LAUNCH_MANIFEST",
                ("Creative order/capacity не подтверждает безопасный запуск",),
                manifest.manifest_id,
            )
        for creative in creatives:
            scope = (destination.adset_id, creative.ad_name)
            if (
                scope in seen_scopes
                or creative.expected_configured_status != "ACTIVE"
                or not creative.media_asset_ids
                or any(asset_id not in assets_by_id for asset_id in creative.media_asset_ids)
            ):
                raise LaunchCheckBlocked(
                    "INVALID_LAUNCH_MANIFEST",
                    ("Creative exact scope/status/media binding неверен",),
                    manifest.manifest_id,
                )
            seen_scopes.add(scope)


def _read_staged_text(directory: Path, relative_path: str) -> str:
    """Читает уже проверенный UTF-8 body через O_NOFOLLOW."""

    from services.approval_source_media import _open_safe_file, _safe_relative_path

    try:
        descriptor, _path, _info = _open_safe_file(
            directory,
            _safe_relative_path(relative_path),
        )
        chunks: list[bytes] = []
        try:
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        return b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeDecodeError, RuntimeError) as exc:
        raise LaunchCheckBlocked(
            "LAUNCH_INPUT_DRIFT",
            ("Staged ad text недоступен",),
            None,
        ) from exc


def _snapshot_manifest_inputs(
    manifest: LaunchManifest,
    source_directory: Path,
    snapshot_directory: Path,
) -> Path:
    """Копирует exact staged bytes из O_NOFOLLOW fd в private snapshot.

    Provider upload и thumbnail extraction работают только со snapshot, поэтому
    замена pathname после проверки не меняет фактически загружаемые байты.
    """

    from services.approval_source_media import (
        _open_safe_file,
        _safe_relative_path,
        rehash_staged_files,
    )

    relative_paths = {
        asset.staged_relative_path for asset in manifest.media_assets
    }
    relative_paths.update(
        creative.body_staged_relative_path
        for destination in manifest.destinations
        for creative in destination.creatives
    )
    try:
        for relative_path in sorted(relative_paths):
            safe_relative = _safe_relative_path(relative_path)
            source_fd, _source_path, before = _open_safe_file(
                source_directory,
                safe_relative,
            )
            target_path = snapshot_directory.joinpath(*safe_relative.parts)
            target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target_fd = os.open(
                target_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o400,
            )
            try:
                while chunk := os.read(source_fd, 1024 * 1024):
                    view = memoryview(chunk)
                    while view:
                        written = os.write(target_fd, view)
                        if written <= 0:
                            raise OSError("snapshot write incomplete")
                        view = view[written:]
                os.fsync(target_fd)
                after = os.fstat(source_fd)
            finally:
                os.close(target_fd)
                os.close(source_fd)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise RuntimeError("staged inode изменился во время snapshot")

        media_digests = rehash_staged_files(
            tuple(asset.staged_relative_path for asset in manifest.media_assets),
            staging_root=snapshot_directory,
        )
        actual_media = tuple(
            (item.order_index, item.size_bytes, item.content_sha256)
            for item in media_digests
        )
        expected_media = tuple(
            (asset.order_index, asset.size_bytes, asset.content_sha256)
            for asset in manifest.media_assets
        )
        if actual_media != expected_media:
            raise RuntimeError("snapshot media hash mismatch")

        body_contract = {
            creative.body_staged_relative_path: creative.body_sha256
            for destination in manifest.destinations
            for creative in destination.creatives
        }
        body_digests = rehash_staged_files(
            tuple(body_contract),
            staging_root=snapshot_directory,
        )
        if any(
            digest.normalized_text_sha256 != body_contract[digest.relative_path]
            for digest in body_digests
        ):
            raise RuntimeError("snapshot body hash mismatch")
    except Exception as exc:
        raise LaunchCheckBlocked(
            "LAUNCH_INPUT_DRIFT",
            ("Provider-owned snapshot не совпадает с immutable manifest",),
            manifest.manifest_id,
        ) from exc
    return snapshot_directory


def _call_to_action(creative: CreativeSpec, *, link: str | None = None) -> dict:
    value: dict[str, str] = {}
    if creative.lead_form_id:
        value["lead_gen_form_id"] = creative.lead_form_id
    actual_link = link or creative.link_url
    if actual_link:
        value["link"] = actual_link
    return {"type": creative.call_to_action, "value": value}


def _story_spec(creative: CreativeSpec) -> dict:
    result = {"page_id": creative.page_id}
    if creative.instagram_actor_id not in {"", "NONE"}:
        result["instagram_user_id"] = creative.instagram_actor_id
    return result


def _creative_payload(
    creative: CreativeSpec,
    assets: tuple[MediaAssetSpec, ...],
    provider_assets: dict[str, dict[str, str]],
    body: str,
) -> dict:
    """Строит provider creative только из immutable CreativeSpec."""

    story_spec = _story_spec(creative)
    if len(assets) == 1 and assets[0].media_type is MediaType.VIDEO:
        provider = provider_assets[assets[0].asset_id]
        story_spec["video_data"] = {
            "video_id": provider["video_id"],
            "image_hash": provider["thumb_hash"],
            "title": creative.title,
            "message": body,
            "call_to_action": _call_to_action(creative),
        }
        return {"object_story_spec": story_spec}
    if len(assets) == 1 and assets[0].media_type is MediaType.IMAGE:
        if not creative.link_url:
            raise LaunchCheckBlocked(
                "INVALID_LAUNCH_MANIFEST",
                ("Image creative требует exact link_url",),
                None,
            )
        story_spec["link_data"] = {
            "image_hash": provider_assets[assets[0].asset_id]["image_hash"],
            "message": body,
            "name": creative.title,
            "link": creative.link_url,
            "call_to_action": _call_to_action(creative),
        }
        return {"object_story_spec": story_spec}
    if assets and all(asset.media_type is MediaType.CAROUSEL_ITEM for asset in assets):
        if not 2 <= len(assets) <= 10 or not creative.link_url:
            raise LaunchCheckBlocked(
                "INVALID_LAUNCH_MANIFEST",
                ("Carousel scope должен содержать 2..10 assets и link",),
                None,
            )
        cta = _call_to_action(creative, link=creative.link_url)
        story_spec["link_data"] = {
            "message": body,
            "name": creative.title,
            "link": creative.link_url,
            "child_attachments": [
                {
                    "image_hash": provider_assets[asset.asset_id]["image_hash"],
                    "name": creative.title,
                    "link": creative.link_url,
                    "call_to_action": cta,
                }
                for asset in assets
            ],
            "call_to_action": cta,
        }
        return {"object_story_spec": story_spec}
    roles = {asset.placement_role: asset for asset in assets}
    if (
        len(assets) == 2
        and all(asset.media_type is MediaType.PLACEMENT_PAIR for asset in assets)
        and set(roles) == {PlacementRole.FEED, PlacementRole.STORY}
        and creative.link_url
    ):
        feed_hash = provider_assets[roles[PlacementRole.FEED].asset_id]["image_hash"]
        story_hash = provider_assets[roles[PlacementRole.STORY].asset_id]["image_hash"]
        cta = _call_to_action(creative, link=creative.link_url)
        return {
            "object_story_spec": story_spec,
            "asset_feed_spec": {
                "images": [
                    {"hash": feed_hash, "adlabels": [{"name": "feed_img"}]},
                    {"hash": story_hash, "adlabels": [{"name": "story_img"}]},
                ],
                "bodies": [{"text": body}],
                "titles": [{"text": creative.title}],
                "link_urls": [{"website_url": creative.link_url}],
                "ad_formats": ["SINGLE_IMAGE"],
                "call_to_action_types": [creative.call_to_action],
                "call_to_actions": [cta],
                "asset_customization_rules": [
                    {
                        "customization_spec": {
                            "publisher_platforms": ["facebook", "instagram"],
                            "facebook_positions": ["feed"],
                            "instagram_positions": ["stream"],
                        },
                        "image_label": {"name": "feed_img"},
                    },
                    {
                        "customization_spec": {
                            "publisher_platforms": ["facebook", "instagram"],
                            "facebook_positions": ["story"],
                            "instagram_positions": ["story", "reels"],
                        },
                        "image_label": {"name": "story_img"},
                    },
                    {
                        "customization_spec": {
                            "publisher_platforms": [
                                "facebook",
                                "instagram",
                                "audience_network",
                                "messenger",
                            ]
                        },
                        "image_label": {"name": "feed_img"},
                        "is_default": True,
                    },
                ],
            },
        }
    raise LaunchCheckBlocked(
        "INVALID_LAUNCH_MANIFEST",
        ("Creative media binding не поддерживается",),
        None,
    )


def _upload_manifest_assets(
    manifest: LaunchManifest,
    directory: Path,
) -> dict[str, dict[str, str]]:
    provider_context = _BOUND_PROVIDER_LAUNCH.get()
    if provider_context is None:
        raise LaunchCheckBlocked(
            "AUTHORIZATION_REQUIRED", ("Asset upload context отсутствует",), None
        )
    provider_assets: dict[str, dict[str, str]] = {}
    for asset in sorted(manifest.media_assets, key=lambda item: item.order_index):
        existing = launch_repository.claim_provider_asset_upload(
            provider_context.proof.auth_id,
            asset.asset_id,
            asset.content_sha256,
            _provider_now(),
        )
        if existing is not None:
            provider_assets[asset.asset_id] = existing
            continue
        path = str(directory / asset.staged_relative_path)
        if asset.media_type is MediaType.VIDEO:
            video_id = upload_video(path, asset.asset_id)
            thumbnail_path = extract_thumbnail(path)
            try:
                thumb_hash = upload_image(thumbnail_path)
            finally:
                if os.path.exists(thumbnail_path):
                    os.remove(thumbnail_path)
            provider_asset = {
                "video_id": video_id,
                "thumb_hash": thumb_hash,
            }
        else:
            provider_asset = {"image_hash": upload_image(path)}
        launch_repository.record_provider_asset_upload(
            provider_context.proof.auth_id,
            asset.asset_id,
            asset.content_sha256,
            provider_asset,
            _provider_now(),
        )
        provider_assets[asset.asset_id] = provider_asset
    return provider_assets


def _validate_manifest_creatives_before_upload(
    manifest: LaunchManifest,
    directory: Path,
) -> None:
    """Отклоняет malformed binding до первой мутации asset upload."""

    placeholder_assets: dict[str, dict[str, str]] = {}
    for asset in manifest.media_assets:
        if asset.media_type is MediaType.VIDEO:
            placeholder_assets[asset.asset_id] = {
                "video_id": f"video-{asset.asset_id}",
                "thumb_hash": f"thumb-{asset.asset_id}",
            }
        else:
            placeholder_assets[asset.asset_id] = {
                "image_hash": f"image-{asset.asset_id}"
            }
    _manifest_creative_payloads(manifest, directory, placeholder_assets)


def _manifest_creative_payloads(
    manifest: LaunchManifest,
    directory: Path,
    provider_assets: dict[str, dict[str, str]],
) -> dict[tuple[str, str], dict]:
    assets_by_id = {asset.asset_id: asset for asset in manifest.media_assets}
    payloads: dict[tuple[str, str], dict] = {}
    for destination in manifest.destinations:
        for creative in sorted(destination.creatives, key=lambda item: item.order_index):
            try:
                assets = tuple(assets_by_id[asset_id] for asset_id in creative.media_asset_ids)
            except KeyError as exc:
                raise LaunchCheckBlocked(
                    "INVALID_LAUNCH_MANIFEST",
                    ("Creative ссылается на неизвестный media asset",),
                    manifest.manifest_id,
                ) from exc
            body = _read_staged_text(directory, creative.body_staged_relative_path)
            key = (destination.adset_id, creative.ad_name)
            if key in payloads:
                raise LaunchCheckBlocked(
                    "INVALID_LAUNCH_MANIFEST",
                    ("Duplicate adset/name внутри launch manifest",),
                    manifest.manifest_id,
                )
            payloads[key] = _creative_payload(creative, assets, provider_assets, body)
    return payloads


def _assert_staged_authorization_targets(manifest: LaunchManifest, validation) -> None:
    """Сопоставляет staged manifest с exact scopes обычного proof."""
    actual = tuple(
        (
            destination.city,
            destination.account_id.removeprefix("act_"),
            destination.adset_id,
            tuple(
                creative.ad_name
                for creative in sorted(
                    destination.creatives,
                    key=lambda creative: creative.order_index,
                )
            ),
        )
        for destination in manifest.destinations
    )
    authorized = tuple(
        (
            str(target.city),
            str(target.account_id).removeprefix("act_"),
            str(target.adset_id),
            tuple(target.expected_names),
        )
        for target in sorted(validation.targets, key=lambda target: target.ordinal)
    )
    if actual != authorized or str(validation.campaign_type) != manifest.campaign_type:
        raise LaunchCheckBlocked(
            "PROVIDER_SCOPE_DRIFT",
            ("Staged manifest не совпадает с DB-backed authorization",),
            str(getattr(validation, "auth_id", "")) or None,
        )


def _manifest_account_context_name(
    expected_account_id: str,
    manifest_id: str,
    *,
    block_code: str,
    block_reason: str,
) -> str | None:
    """Имя thread-контекста FB для кабинета манифеста (fail-closed).

    - онлайн-кабинет (config.FB_ACCOUNT_ID_ONLINE) → "online";
    - дефолтный оффлайн (config.FB_ACCOUNT_ID, cabinet_a) → None;
    - маршрутизированный оффлайн-кабинет карты город→кабинет
      (services/launch_routing.py, например cabinet_b) → "offline:<id>";
    - незарегистрированный кабинет → LaunchCheckBlocked с переданным кодом.
    """
    from config import FB_ACCOUNT_ID_ONLINE
    from services.fb_token_provider import offline_account_context

    normalized = str(expected_account_id).removeprefix("act_").strip()
    if normalized and normalized == str(FB_ACCOUNT_ID_ONLINE).removeprefix("act_"):
        return "online"
    try:
        return offline_account_context(normalized)
    except RuntimeError as exc:
        raise LaunchCheckBlocked(
            block_code,
            (block_reason,),
            manifest_id,
        ) from exc


def _execute_launch_manifest_unchecked(
    manifest: LaunchManifest,
    *,
    attempt: ActionAttemptAttestation,
    authorization: ProviderLaunchAuthorization | None = None,
) -> tuple[str, ...]:
    """Private exact executor, но без обычного proof он fail-closed.

    Функция не делает discovery, не читает config mappings и не принимает
    callbacks. Она исполняет только destinations/creatives immutable manifest.
    """

    if not isinstance(manifest, LaunchManifest):
        raise TypeError("manifest должен быть LaunchManifest")
    if not isinstance(authorization, ProviderLaunchAuthorization):
        raise LaunchCheckBlocked(
            "AUTHORIZATION_REQUIRED",
            ("Staged executor требует DB-backed provider authorization",),
            manifest.manifest_id,
        )
    if not manifest.destinations:
        raise ValueError("LaunchManifest не содержит destinations")
    _validate_launch_manifest_scope(manifest)
    account_ids = {destination.account_id.removeprefix("act_") for destination in manifest.destinations}
    if len(account_ids) != 1:
        raise LaunchCheckBlocked(
            "INVALID_LAUNCH_MANIFEST",
            ("Один launch item не может менять несколько FB accounts",),
            manifest.manifest_id,
        )
    expected_account_id = next(iter(account_ids))
    from services.fb_token_provider import fb_account

    account_name = _manifest_account_context_name(
        expected_account_id,
        manifest.manifest_id,
        block_code="INVALID_LAUNCH_MANIFEST",
        block_reason="FB account отсутствует в code-owned account registry",
    )

    staged_directory = _verify_staged_manifest_files(manifest)
    with ExitStack() as execution_scope:
        snapshot_root = Path(
            execution_scope.enter_context(
                tempfile.TemporaryDirectory(prefix="acme-provider-launch-")
            )
        )
        directory = _snapshot_manifest_inputs(
            manifest,
            staged_directory,
            snapshot_root,
        )
        _validate_manifest_creatives_before_upload(manifest, directory)
        # Аттестация владельца несёт account_id цели (ProposedTarget →
        # permit → attempt): расхождение с кабинетом манифеста — это чужая
        # аттестация, а не наш запуск. Отказ до первой provider-мутации.
        attempt_account_id = str(
            getattr(attempt, "account_id", "")
        ).removeprefix("act_").strip()
        if attempt_account_id != expected_account_id:
            raise LaunchCheckBlocked(
                "PROVIDER_SCOPE_DRIFT",
                ("Attestation несёт другой FB account, чем manifest",),
                manifest.manifest_id,
            )
        execution_scope.enter_context(fb_account(account_name))
        actual_account_id = str(get_fb_account_id()).removeprefix("act_")
        if actual_account_id != expected_account_id:
            raise LaunchCheckBlocked(
                "PROVIDER_SCOPE_DRIFT",
                ("Активный Facebook account не совпадает с manifest",),
                manifest.manifest_id,
            )
        validation = validate_authorization_media(
            authorization,
            manifest.media_manifest_sha256,
            repository=launch_repository,
            now=_provider_now(),
        )
        _assert_staged_authorization_targets(manifest, validation)
        replacement_workflow_ids = {
            destination.adset_id: destination.replacement_workflow_id
            for destination in manifest.destinations
            if destination.replacement_workflow_id is not None
        }
        # Locks уже удерживает LaunchActionAdapter.execution_scope.
        # Private executor не берёт их повторно и не порождает
        # callback-based nested critical sections.
        _preupload_authorization_gate(
            authorization,
            validation,
            media_sha256=manifest.media_manifest_sha256,
            replacement_workflow_ids=replacement_workflow_ids,
        )
        with _bind_provider_launch_context(
            authorization,
            attempt=attempt,
            media_sha256=manifest.media_manifest_sha256,
            account_kind=str(validation.account_kind),
            account_id=expected_account_id,
            replacement_workflow_ids=replacement_workflow_ids,
        ):
            provider_assets = _upload_manifest_assets(manifest, directory)
            payloads = _manifest_creative_payloads(
                manifest,
                directory,
                provider_assets,
            )
            created_ids: list[str] = []
            for destination in manifest.destinations:
                _renew_provider_target(
                    authorization,
                    city=destination.city,
                    adset_id=destination.adset_id,
                    media_sha256=manifest.media_manifest_sha256,
                    now=_provider_now(),
                )
                for creative in sorted(
                    destination.creatives,
                    key=lambda item: item.order_index,
                ):
                    key = (destination.adset_id, creative.ad_name)
                    created_ids.append(
                        _post_ad(
                            creative.ad_name,
                            destination.adset_id,
                            payloads[key],
                        )
                    )
    return tuple(created_ids)


def _observe_launch_manifest_postcondition(
    manifest: LaunchManifest,
    created_ids: tuple[str, ...],
    now: datetime,
) -> ActionObservation:
    """Возвращает полный force-live post observation, не повторяя POST."""

    if not isinstance(manifest, LaunchManifest):
        raise TypeError("manifest должен быть LaunchManifest")
    expected_count = sum(
        len(destination.creatives) for destination in manifest.destinations
    )
    if len(created_ids) != expected_count or len(created_ids) != len(set(created_ids)):
        raise LaunchCheckBlocked(
            "LAUNCH_POSTCONDITION_INCOMPLETE",
            ("Provider вернул неполный или повторяющийся набор ad IDs",),
            manifest.manifest_id,
        )
    from services.approval_source_facebook import read_action_postcondition
    from services.fb_token_provider import fb_account

    account_ids = {
        destination.account_id.removeprefix("act_")
        for destination in manifest.destinations
    }
    if len(account_ids) != 1:
        raise LaunchCheckBlocked(
            "LAUNCH_POSTCONDITION_INCOMPLETE",
            ("Post-read не может охватить несколько FB accounts",),
            manifest.manifest_id,
        )
    account_id = next(iter(account_ids))
    # Пост-чтение обязано идти в кабинет ЦЕЛИ: для маршрутизированного
    # оффлайн-кабинета (например cabinet_b у CityF) контекст несёт
    # его account_id, иначе reconciliation ниже упрётся в чужой кабинет.
    account_name = _manifest_account_context_name(
        account_id,
        manifest.manifest_id,
        block_code="LAUNCH_POSTCONDITION_INCOMPLETE",
        block_reason="Post-read не знает FB account манифеста (нет в реестре)",
    )
    expected_scope = {
        creative.ad_name: destination.adset_id
        for destination in manifest.destinations
        for creative in destination.creatives
    }
    # Читаем ТОЛЬКО целевые адсеты манифеста: без серверного фильтра
    # выборка идёт по всему кабинету и на большом кабинете (тысячи объявлений с
    # архивом) гарантированно упирается в «превышен лимит 10 страниц» —
    # пост-проверка падала после первого созданного объявления, гейтвей уходил в
    # RECONCILE_REQUIRED, и карточка оставалась 1 из N.
    # Проверка «лишний exact duplicate» ниже по определению живёт в тех же
    # адсетах, поэтому фильтр её не ослабляет.
    target_adset_ids = sorted(
        {str(destination.adset_id) for destination in manifest.destinations}
    )
    with fb_account(account_name):
        inventory = fetch_ads_for_launch_reconciliation(
            account_id,
            adset_ids=target_adset_ids,
        )
        rows_by_id = {
            str(row.get("id")): row
            for row in inventory
            if isinstance(row, dict) and row.get("id") is not None
        }
        if set(created_ids) - set(rows_by_id):
            raise LaunchCheckBlocked(
                "LAUNCH_POSTCONDITION_INCOMPLETE",
                ("Не все exact created IDs найдены в полном account inventory",),
                manifest.manifest_id,
            )
        for ad_id in created_ids:
            row = rows_by_id[ad_id]
            creative = row.get("creative")
            if (
                row.get("name") not in expected_scope
                or str(row.get("adset_id") or "")
                != expected_scope[str(row.get("name"))]
                or row.get("status") != "ACTIVE"
                or row.get("effective_status")
                not in {"ACTIVE", "PENDING_REVIEW", "IN_PROCESS"}
                or not isinstance(creative, dict)
                or not str(creative.get("id") or "")
            ):
                raise LaunchCheckBlocked(
                    "LAUNCH_POSTCONDITION_INCOMPLETE",
                    ("Created ad scope/status/creative binding не подтверждены",),
                    manifest.manifest_id,
                )
        created_set = set(created_ids)
        if any(
            str(row.get("id") or "") not in created_set
            and row.get("name") in expected_scope
            and str(row.get("adset_id") or "")
            == expected_scope[str(row.get("name"))]
            for row in inventory
            if isinstance(row, dict)
        ):
            raise LaunchCheckBlocked(
                "LAUNCH_POSTCONDITION_INCOMPLETE",
                ("После запуска найден лишний exact duplicate",),
                manifest.manifest_id,
            )
        observation = read_action_postcondition(manifest, created_ids, now)
    states = observation.target_state.split("|")
    allowed_effective = {"ACTIVE", "PENDING_REVIEW", "IN_PROCESS"}
    if (
        len(states) != expected_count * 2
        or any(states[index] != "ACTIVE" for index in range(0, len(states), 2))
        or any(
            states[index] not in allowed_effective
            for index in range(1, len(states), 2)
        )
        or observation.subject_ids != tuple(sorted(created_ids))
    ):
        raise LaunchCheckBlocked(
            "LAUNCH_POSTCONDITION_INCOMPLETE",
            ("Live Facebook status не подтвердил exact созданные объявления",),
            manifest.manifest_id,
        )
    return observation


def _build_city_launch_media_manifest(
    *,
    media_type: str,
    card_name: str,
    city: str,
    campaign_type: str,
    adset_id: str,
    expected_ad_names: list[str],
    files: tuple[LaunchMediaFileEvidence, ...],
) -> LaunchMediaManifest:
    """Связывает actual local bytes с exact provider scope и ordered names."""
    account_kind = "online" if campaign_type in {"mql_online", "prodb_online"} else "offline"
    account_id = str(get_fb_account_id()).removeprefix("act_").strip()
    normalized_adset_id = str(adset_id).strip()
    names = tuple(expected_ad_names)
    card_name_sha256 = hashlib.sha256(card_name.encode("utf-8")).hexdigest()
    if not account_id or not normalized_adset_id or not names or len(names) != len(set(names)):
        raise LaunchMediaBindingError("launch_media_scope_invalid")
    payload = {
        "media_type": media_type,
        "card_name_sha256": card_name_sha256,
        "city": city,
        "account_kind": account_kind,
        "account_id": account_id,
        "adset_id": normalized_adset_id,
        "expected_ad_names": list(names),
        "files": [
            {
                "relative_name": item.relative_name,
                "size": item.size,
                "content_sha256": item.content_sha256,
            }
            for item in files
        ],
    }
    manifest_sha256 = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return LaunchMediaManifest(
        manifest_sha256=manifest_sha256,
        media_type=media_type,
        card_name_sha256=card_name_sha256,
        city=city,
        account_kind=account_kind,
        account_id=account_id,
        adset_id=normalized_adset_id,
        expected_ad_names=names,
        files=files,
    )


def _assert_launch_media_unchanged(
    bindings: tuple[tuple[str, str], ...],
    expected_files: tuple[LaunchMediaFileEvidence, ...],
) -> None:
    if _hash_launch_media_files(bindings) != expected_files:
        raise LaunchMediaBindingError("launch_media_changed_before_create")


def _resolve_launch_adsets(
    campaign_type: str,
    adset_type: str,
    cities: list[str] | None,
) -> list[tuple[str, str]]:
    """Строит exact target adsets до первого provider upload."""
    from agent.adset_discovery import get_adsets_dict, get_mql_adsets_dict

    if campaign_type == "website":
        adset_iter = list(get_mql_adsets_dict().items())
        if not adset_iter:
            raise ValueError("ADSETS_MQL пуст — нечего запускать в website-кампанию")
    elif campaign_type == "leadgen_prodb":
        # PRODB-инвентарь — выделенные PRODB-адсеты (тип PRODB карты роутинга), а не
        # PRODA-адсеты по языку карточки: раньше ветка брала
        # adsets[adset_type] и PRODB-креативы уезжали в PRODA-адсеты городов.
        # Язык (adset_type) для PRODB выбирает только лид-форму и текст.
        leadgen_adsets = {
            city: adsets
            for city, adsets in get_adsets_dict().items()
            if city != "Онлайн"
        }
        adset_iter = [
            (city, adsets["PRODB"])
            for city, adsets in leadgen_adsets.items()
            if adsets.get("PRODB")
        ]
        missing_prodb = sorted(
            city for city, adsets in leadgen_adsets.items() if not adsets.get("PRODB")
        )
        if missing_prodb:
            logger.warning(
                "_resolve_launch_adsets: у городов %s нет живого PRODB-адсета — "
                "они пропущены для leadgen_prodb (PRODB_ADSET_NOT_DISCOVERED)",
                ", ".join(missing_prodb),
            )
        if not adset_iter:
            raise ValueError(
                "Ни одного PRODB-адсета (тип PRODB) не найдено — нечего запускать в leadgen_prodb"
            )
    elif campaign_type == "mql_online":
        online_adsets = (get_adsets_dict().get("Онлайн") or {})
        if not online_adsets:
            raise ValueError("Не найдено адсетов 'Онлайн' (MQL_Online)")
        adset_iter = [
            ("Онлайн", online_adsets[atype])
            for atype in (adset_type,)
            if atype in online_adsets
        ]
        if not adset_iter:
            raise ValueError(f"Адсет 'Онлайн {adset_type}' не найден")
    elif campaign_type == "prodb_online":
        from config import PRODB_ONLINE_ADSETS

        adset_id = PRODB_ONLINE_ADSETS.get(adset_type)
        if not adset_id:
            raise ValueError(f"Нет адсета PRODB-онлайн для языка {adset_type}")
        adset_iter = [("Онлайн", adset_id)]
    else:
        adset_iter = [
            (city, adsets[adset_type])
            for city, adsets in get_adsets_dict().items()
            if adset_type in adsets and city != "Онлайн"
        ]

    if cities:
        cities_set = {city.strip() for city in cities if city and city.strip()}
        before = len(adset_iter)
        adset_iter = [
            (city, adset_id)
            for city, adset_id in adset_iter
            if city in cities_set
        ]
        if not adset_iter:
            raise ValueError(
                f"Города {cities_set} не найдены среди доступных адсетов "
                f"({campaign_type}). Было: {before}."
            )
    return [(str(city), str(adset_id)) for city, adset_id in adset_iter]


def adset_effective_statuses(adset_ids: list[str]) -> dict[str, str]:
    """effective_status адсетов одним Graph-запросом (в текущем контексте кабинета).

    Отсутствующий или невалидный ответ по адсету → статус «UNKNOWN»: правило «выключенный
    адсет пропускаем» должно ошибаться в сторону запуска-как-раньше, а не тихо вырезать город.
    """
    ids = [str(item).strip() for item in adset_ids if str(item).strip()]
    if not ids:
        return {}
    response = _throttled_get(
        API,
        params={
            "access_token": get_fb_token(),
            "ids": ",".join(ids),
            "fields": "id,effective_status",
        },
    )
    try:
        payload = response.json()
    except ValueError:
        return {adset_id: "UNKNOWN" for adset_id in ids}
    result: dict[str, str] = {}
    for adset_id in ids:
        item = payload.get(adset_id) if isinstance(payload, dict) else None
        status = item.get("effective_status") if isinstance(item, dict) else None
        result[adset_id] = str(status) if isinstance(status, str) and status else "UNKNOWN"
    return result


def _planned_city_names(
    city: str,
    card_name: str,
    product: str | None,
    prepared_media: tuple,
) -> tuple[str, ...]:
    media_type, paths, singles, _bindings, _files = prepared_media
    if media_type == "video":
        video_assets = [{"label": Path(path).stem} for path in paths]
        image_assets: list[dict] = []
    elif media_type == "image":
        video_assets = []
        image_assets = [{"label": Path(path).stem} for path in paths]
    else:
        video_assets = []
        image_assets = []
    placement_pairs = (
        [{"label": pair["label"]} for pair in paths]
        if media_type == "placement_pairs"
        else []
    )
    single_assets = (
        [{"label": single["label"]} for single in singles]
        if media_type == "placement_pairs"
        else []
    )
    return tuple(
        _expected_city_ad_names(
            city,
            card_name,
            media_type,
            video_assets,
            image_assets,
            placement_pairs,
            single_assets,
            product,
        )
    )


def _assert_authorized_launch_targets(
    validation,
    *,
    adset_iter: list[tuple[str, str]],
    card_name: str,
    product: str | None,
    prepared_media: tuple,
    account_kind: str,
    account_id: str,
    auth_id: str,
) -> None:
    """Сверяет exact city/adset/names до любых asset uploads."""
    authorized_targets = getattr(validation, "targets", None)
    if not isinstance(authorized_targets, tuple):
        raise LaunchCheckBlocked(
            "AUTHORIZATION_SCOPE_UNAVAILABLE",
            ("Authorization не содержит immutable target scopes",),
            auth_id,
        )
    if len(authorized_targets) != len(adset_iter):
        raise LaunchCheckBlocked(
            "PROVIDER_SCOPE_DRIFT",
            ("Количество target adsets изменилось после preflight",),
            auth_id,
        )
    for ordinal, ((city, adset_id), target) in enumerate(
        zip(adset_iter, authorized_targets, strict=True)
    ):
        expected_names = _planned_city_names(
            city,
            card_name,
            product,
            prepared_media,
        )
        target_account_id = str(getattr(target, "account_id", "")).removeprefix(
            "act_"
        )
        if (
            getattr(target, "ordinal", None) != ordinal
            or str(getattr(target, "city", "")) != city
            or str(getattr(target, "account_kind", "")) != account_kind
            or target_account_id != account_id
            or str(getattr(target, "adset_id", "")) != adset_id
            or tuple(getattr(target, "expected_names", ())) != expected_names
        ):
            raise LaunchCheckBlocked(
                "PROVIDER_SCOPE_DRIFT",
                (f"Target scope изменился для города {city}",),
                auth_id,
            )


def _call_prepare_city_callback(
    callback,
    city: str,
    adset_id: str,
    expected_names: list[str],
    manifest: LaunchMediaManifest,
):
    """Передаёт новый manifest, сохраняя старый read-only callback contract."""
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(city, adset_id, expected_names, manifest)
    try:
        signature.bind(city, adset_id, expected_names, manifest)
    except TypeError:
        signature.bind(city, adset_id, expected_names)
        return callback(city, adset_id, expected_names)
    return callback(city, adset_id, expected_names, manifest)


def _launch_creative_impl(card_name: str, adset_type: str, media: dict, body: str,
                           campaign_type: str, cities: list[str] | None,
                           progress_cb=None, product: str | None = None,
                           prepare_city_cb=None, city_success_cb=None,
                          prepared_media: tuple | None = None,
                          adset_iter: list[tuple[str, str]] | None = None) -> dict:
    """Удалённый raw executor; provider mutation доступна только manifest path."""
    del (
        card_name,
        adset_type,
        media,
        body,
        campaign_type,
        cities,
        progress_cb,
        product,
        prepare_city_cb,
        city_success_cb,
        prepared_media,
        adset_iter,
    )
    raise LaunchCheckBlocked(
        "LEGACY_LAUNCH_BYPASS_FORBIDDEN",
        ("Raw creative executor удалён; требуется immutable LaunchManifest",),
        None,
    )


def fetch_ads_for_launch_reconciliation(
    expected_account_id: str,
    adset_ids: list[str] | None = None,
) -> list[dict]:
    """Читает account ads для crash reconciliation запуска.

    Любая неполная пагинация считается ошибкой: вызывающий код блокирует
    повторный CREATE, чтобы не создать дубль после падения процесса.

    ``adset_ids`` — серверный фильтр по целевым адсетам городских планов.
    Без него выборка читает ВЕСЬ кабинет (включая годы архива) и на больших
    кабинетах гарантированно упирается в потолок страниц — сверка становилась
    невозможной в принципе. Если провайдер молча проигнорирует фильтр,
    потолок страниц сработает как раньше (fail-closed, полнота не соврёт).
    """
    account_id = str(get_fb_account_id()).removeprefix("act_")
    expected = str(expected_account_id).removeprefix("act_")
    if account_id != expected:
        raise RuntimeError("FB reconciliation заблокирован: активен другой account_id")
    filtering: str | None = None
    if adset_ids:
        normalized = sorted({str(item) for item in adset_ids if str(item)})
        filtering = json.dumps(
            [{"field": "adset.id", "operator": "IN", "value": normalized}]
        )
    cursor: str | None = None
    seen_cursors: set[str] = set()
    ads: list[dict] = []

    for _page in range(10):
        params: dict = {
            "access_token": get_fb_token(),
            "fields": (
                "id,name,adset_id,created_time,status,effective_status,creative{id}"
            ),
            "limit": 500,
        }
        if filtering:
            params["filtering"] = filtering
        if cursor:
            params["after"] = cursor
        response = _throttled_get(f"{API}/act_{account_id}/ads", params=params)
        if response.status_code != 200:
            raise RuntimeError(f"FB reconciliation /ads: HTTP {response.status_code}")
        payload = response.json()
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise RuntimeError("FB reconciliation /ads: data не является списком")
        ads.extend(ad for ad in rows if isinstance(ad, dict) and ad.get("id"))

        paging = payload.get("paging") or {}
        if "next" not in paging:
            return ads
        next_cursor = (paging.get("cursors") or {}).get("after")
        if not next_cursor or next_cursor in seen_cursors:
            raise RuntimeError("FB reconciliation /ads: неполная пагинация")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    raise RuntimeError("FB reconciliation /ads: превышен лимит 10 страниц")


def _is_routed_offline_account(account_id: str) -> bool:
    """Кабинет входит в оффлайн-карту маршрутизации город→кабинет.

    Fail-closed: любая ошибка чтения карты — «нет», а не «да».
    """
    try:
        from services.launch_routing import accounts_to_scan

        return str(account_id).removeprefix("act_") in accounts_to_scan()
    except Exception:
        return False


def fetch_complete_account_ad_inventory(
    account_kind: str,
    expected_account_id: str,
) -> list[dict[str, object]]:
    """Читает полный read-only inventory одного exact рекламного кабинета.

    Успешный возврат означает доказанно завершённую cursor pagination. Любая
    неоднозначность scope, строки или paging поднимает ``AdsetInventoryError``;
    частичный список наружу никогда не возвращается.
    """
    if type(account_kind) is not str or account_kind not in {"offline", "online"}:
        raise AdsetInventoryError("account_inventory_kind_invalid")
    if type(expected_account_id) is not str:
        raise AdsetInventoryError("account_inventory_account_id_invalid")
    expected = expected_account_id.removeprefix("act_").strip()
    if not expected.isdigit():
        raise AdsetInventoryError("account_inventory_account_id_invalid")

    from services.fb_token_provider import fb_account

    rows = CompleteAccountAdInventory(account_kind, expected)
    seen_ad_ids: set[str] = set()
    seen_cursors: set[str] = set()
    after: str | None = None
    with fb_account("online" if account_kind == "online" else None):
        live_account_id = str(get_fb_account_id()).removeprefix("act_").strip()
        if live_account_id != expected and not (
            account_kind == "offline" and _is_routed_offline_account(expected)
        ):
            # Оффлайн FB_TOKEN обслуживает ВСЕ кабинеты карты маршрутизации
            # город→кабинет (cabinet_a + cabinet_b), поэтому оффлайн-скан чужого
            # для process-default, но маршрутизированного кабинета легален.
            # Любой другой кабинет — отказ до первого HTTP.
            raise AdsetInventoryError("account_inventory_scope_mismatch")

        for page_number in range(1, _MAX_ACCOUNT_INVENTORY_PAGES + 1):
            params: dict[str, object] = {
                "access_token": get_fb_token(),
                "fields": (
                    "id,name,account_id,adset_id,created_time,status,effective_status"
                ),
                "limit": 500,
            }
            if after is not None:
                params["after"] = after
            response = _throttled_get(
                f"{API}/act_{expected}/ads",
                params=params,
            )
            payload = _inventory_payload(response, "account_ads")
            page_rows = payload.get("data")
            if not isinstance(page_rows, list):
                raise AdsetInventoryError("account_ads_data_not_list")

            for raw_row in page_rows:
                if not isinstance(raw_row, dict):
                    raise AdsetInventoryError("account_ads_row_not_object")
                ad_id = raw_row.get("id")
                name = raw_row.get("name")
                row_account_id = str(raw_row.get("account_id") or "").removeprefix(
                    "act_"
                )
                adset_id = raw_row.get("adset_id")
                created_time = raw_row.get("created_time")
                status = raw_row.get("status")
                effective_status = raw_row.get("effective_status")
                if (
                    not isinstance(ad_id, str)
                    or not ad_id
                    or ad_id in seen_ad_ids
                    or not isinstance(name, str)
                    or row_account_id != expected
                    or not isinstance(adset_id, str)
                    or not adset_id
                    or not isinstance(created_time, str)
                    or not created_time
                    or not isinstance(status, str)
                    or not status
                    or not isinstance(effective_status, str)
                    or not effective_status
                ):
                    raise AdsetInventoryError("account_ads_row_incomplete_or_mismatched")
                try:
                    created_at = datetime.fromisoformat(
                        created_time.replace("Z", "+00:00")
                    )
                except ValueError as exc:
                    raise AdsetInventoryError("account_ads_created_time_invalid") from exc
                if created_at.tzinfo is None or created_at.utcoffset() is None:
                    raise AdsetInventoryError("account_ads_created_time_naive")
                seen_ad_ids.add(ad_id)
                rows.append(
                    {
                        **raw_row,
                        "id": ad_id,
                        "account_kind": account_kind,
                        "account_id": expected,
                        "adset_id": adset_id,
                        "inventory_complete": True,
                    }
                )

            paging = payload.get("paging")
            if paging is None:
                return rows
            if not isinstance(paging, dict):
                raise AdsetInventoryError("account_ads_paging_invalid")
            if not paging.get("next"):
                return rows
            cursors = paging.get("cursors")
            next_after = cursors.get("after") if isinstance(cursors, dict) else None
            if (
                not isinstance(next_after, str)
                or not next_after
                or next_after in seen_cursors
            ):
                raise AdsetInventoryError("account_ads_paging_cursor_invalid")
            if page_number == _MAX_ACCOUNT_INVENTORY_PAGES:
                raise AdsetInventoryError("account_ads_page_limit_exceeded")
            seen_cursors.add(next_after)
            after = next_after

    raise AdsetInventoryError("account_ads_page_limit_exceeded")


def rename_ad(ad_id: str, new_name: str) -> bool:
    """Блокирует RENAME до появления отдельного owner proposal contract."""

    del ad_id, new_name
    from integrations.facebook_ads_mutation_transport import ForbiddenMutation

    raise ForbiddenMutation("RENAME_OPERATION_FORBIDDEN")
