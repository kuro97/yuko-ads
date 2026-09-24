"""Fail-closed восстановление одного отсутствующего рекламного ассета.

Модуль намеренно не связан с Trello и обычным launcher: manifest уже содержит
точную цель, а recovery никогда не отмечает карточку, не ставит рекламу на паузу
и не удаляет объявления. Единственная разрешённая бизнес-мутация — CREATE одного
объявления после полного live-preflight конкретного adset.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from integrations.facebook import MAX_ADS_PER_ADSET
from services.product_tags import PRODUCTS, VALID_PRODUCTS, format_ad_name


AccountKind = Literal["offline"]
AdsetType = Literal["L2", "L1"]
NameMode = Literal["with_media_label", "card_only"]
RecoveryStatus = Literal["SUCCEEDED", "BLOCKED"]
PreparedMediaType = Literal["video", "image", "existing_creative"]

DEFAULT_LEDGER_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "ad_asset_recovery_state.json"
)
logger = logging.getLogger(__name__)


class RecoveryError(RuntimeError):
    """Базовая безопасная ошибка recovery."""


class ManifestValidationError(ValueError):
    """Manifest не задаёт одну точную и безопасную цель."""


class RecoveryLedgerError(RecoveryError):
    """Durable ledger нельзя надёжно прочитать или записать."""


class RecoveryGatewayRequired(RecoveryError):
    """Gateway attempt существует, но требует только read-only reconciliation."""

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__("asset_recovery_gateway_reconciliation_required")


def _asset_recovery_hard_reserve_slots() -> int:
    """Читает server-owned reserve из того же cleaner config, что launch recovery."""

    from services.autopilot import get_autopilot_config

    cleaner = get_autopilot_config().get("cleaner")
    if not isinstance(cleaner, dict):
        raise RecoveryError("asset_recovery_cleaner_config_invalid")
    value = cleaner.get("hard_reserve_slots", 1)
    if type(value) is not int or not 1 <= value <= 5:
        raise RecoveryError("asset_recovery_hard_reserve_invalid")
    return value


@dataclass(frozen=True, slots=True)
class RecoveryManifest:
    """Полная immutable-идентичность одного объявления в одном городе."""

    account_kind: AccountKind
    account_id: str
    card_id: str
    card_title: str
    media_basename: str
    city: str
    adset_id: str
    expected_adset_name: str
    adset_type: AdsetType
    expected_ad_name: str
    sibling_expected_ad_name: str
    product: str
    window_start: str
    window_end: str
    drive_url: str | None = None
    source_ad_id: str | None = None
    source_adset_id: str | None = None
    expected_source_adset_name: str | None = None
    name_mode: NameMode = "with_media_label"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RecoveryManifest":
        """Создаёт и полностью валидирует manifest на границе CLI/API."""
        if not isinstance(value, dict):
            raise ManifestValidationError("manifest должен быть JSON-объектом")
        expected_fields = set(cls.__dataclass_fields__)
        optional_fields = {
            "drive_url",
            "source_ad_id",
            "source_adset_id",
            "expected_source_adset_name",
            "name_mode",
        }
        actual_fields = set(value)
        missing = sorted(expected_fields - optional_fields - actual_fields)
        extra = sorted(actual_fields - expected_fields)
        if missing or extra:
            raise ManifestValidationError(
                f"неверные поля manifest: missing={missing}, extra={extra}"
            )
        manifest = cls(**value)
        manifest.validate()
        return manifest

    def validate(self) -> None:
        """Проверяет exact identity без сетевых вызовов."""
        required_text_fields = set(self.__dataclass_fields__) - {
            "drive_url",
            "source_ad_id",
            "source_adset_id",
            "expected_source_adset_name",
            "sibling_expected_ad_name",
        }
        text_fields = {
            field_name: getattr(self, field_name)
            for field_name in required_text_fields
        }
        invalid_text = [
            name
            for name, value in text_fields.items()
            if not isinstance(value, str) or not value.strip()
        ]
        if invalid_text:
            raise ManifestValidationError(
                f"пустые или нестроковые поля manifest: {sorted(invalid_text)}"
            )
        if self.account_kind != "offline":
            raise ManifestValidationError(
                "recovery поддерживает только offline account с обычной Lead Form"
            )
        if not self.account_id.isdigit() or not self.adset_id.isdigit():
            raise ManifestValidationError("account_id и adset_id должны состоять из цифр")
        if self.adset_type not in {"L2", "L1"}:
            raise ManifestValidationError("adset_type должен быть L2 или L1")
        _validate_adset_identity(
            self.expected_adset_name,
            self.city,
            self.adset_type,
            field_name="expected_adset_name",
        )
        if self.product not in VALID_PRODUCTS:
            raise ManifestValidationError(f"неизвестный product: {self.product}")
        if Path(self.media_basename).name != self.media_basename:
            raise ManifestValidationError("media_basename не должен содержать путь")
        has_drive = isinstance(self.drive_url, str) and bool(self.drive_url.strip())
        has_source = isinstance(self.source_ad_id, str) and bool(self.source_ad_id.strip())
        if has_drive == has_source:
            raise ManifestValidationError(
                "нужно указать ровно один источник: drive_url или source_ad_id"
            )
        if has_source and not str(self.source_ad_id).isdigit():
            raise ManifestValidationError("source_ad_id должен состоять из цифр")
        if has_source:
            if (
                not isinstance(self.source_adset_id, str)
                or not self.source_adset_id.isdigit()
                or not isinstance(self.expected_source_adset_name, str)
                or not self.expected_source_adset_name.strip()
            ):
                raise ManifestValidationError(
                    "source recovery требует source_adset_id и expected_source_adset_name"
                )
            _validate_adset_type_token(
                self.expected_source_adset_name,
                self.adset_type,
                field_name="expected_source_adset_name",
            )
        elif self.source_adset_id or self.expected_source_adset_name:
            raise ManifestValidationError(
                "Drive manifest не должен содержать source adset identity"
            )
        if not has_source and (
            not isinstance(self.sibling_expected_ad_name, str)
            or not self.sibling_expected_ad_name.strip()
        ):
            raise ManifestValidationError("Drive recovery требует sibling exact name")
        if (
            self.sibling_expected_ad_name
            and self.expected_ad_name == self.sibling_expected_ad_name
        ):
            raise ManifestValidationError("target и sibling exact name совпадают")
        if self.name_mode not in {"with_media_label", "card_only"}:
            raise ManifestValidationError("неизвестный name_mode")

        media_label = Path(self.media_basename).stem
        base_name = f"{self.city} | {self.card_title}"
        if self.name_mode == "with_media_label":
            base_name += f" / {media_label}"
        canonical_name = format_ad_name(base_name, self.product)
        known_tag_product = next(
            (
                product
                for product, config in PRODUCTS.items()
                if self.expected_ad_name.endswith(config["tag"])
            ),
            None,
        )
        if known_tag_product is not None and known_tag_product != self.product:
            raise ManifestValidationError(
                "expected_ad_name содержит тег другого продукта"
            )
        allowed_names = (
            {base_name, canonical_name}
            if has_source
            else {canonical_name}
        )
        if self.expected_ad_name not in allowed_names:
            raise ManifestValidationError(
                "expected_ad_name не совпадает с city/card/media/product"
            )

        start = _parse_aware_time(self.window_start, "window_start")
        end = _parse_aware_time(self.window_end, "window_end")
        if start >= end:
            raise ManifestValidationError("window_start должен быть раньше window_end")

    @property
    def key(self) -> str:
        """Стабильный ключ exact manifest, включая разрешённое временное окно."""
        payload = json.dumps(
            asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def start_at(self) -> datetime:
        return _parse_aware_time(self.window_start, "window_start")

    @property
    def end_at(self) -> datetime:
        return _parse_aware_time(self.window_end, "window_end")


@dataclass(frozen=True, slots=True)
class PreparedAsset:
    """Уже загруженный в FB одиночный ассет для одного CREATE."""

    media_type: PreparedMediaType
    image_hash: str
    video_id: str | None = None
    creative_id: str | None = None
    source_name: str | None = None
    gateway_idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Детерминированный результат одной exact цели."""

    manifest_key: str
    city: str
    adset_id: str
    status: RecoveryStatus
    reason: str
    ad_id: str | None = None


class RecoveryBackend(Protocol):
    """Граница внешних FB/Drive операций; в тестах полностью заменяется fake."""

    def account_scope(self, account_kind: AccountKind) -> AbstractContextManager[None]: ...

    def adset_lock(self, adset_id: str) -> AbstractContextManager[None]: ...

    def current_account_id(self) -> str: ...

    def get_adset_info(self, adset_id: str) -> dict[str, Any]: ...

    def prepare_asset(self, manifest: RecoveryManifest) -> PreparedAsset: ...

    def revalidate_prepared(
        self, manifest: RecoveryManifest, prepared: PreparedAsset
    ) -> None: ...

    def create_asset(
        self, manifest: RecoveryManifest, prepared: PreparedAsset
    ) -> str: ...

    def now(self) -> datetime: ...


class ProductionRecoveryBackend:
    """Адаптер существующих интеграций без собственной FB-бизнес-логики."""

    def account_scope(self, account_kind: AccountKind) -> AbstractContextManager[None]:
        from services.fb_token_provider import fb_account

        if account_kind != "offline":
            raise ManifestValidationError("неподдерживаемый account_kind")
        return fb_account(None)

    def adset_lock(self, adset_id: str) -> AbstractContextManager[None]:
        # Production existing-creative path берёт source+target locks единым
        # numeric-ordered scope внутри approval adapter.
        del adset_id
        return nullcontext()

    def current_account_id(self) -> str:
        from services.fb_token_provider import get_fb_account_id

        return str(get_fb_account_id()).removeprefix("act_")

    def get_adset_info(self, adset_id: str) -> dict[str, Any]:
        from integrations.facebook import get_adset_info

        return get_adset_info(adset_id)

    def prepare_asset(self, manifest: RecoveryManifest) -> PreparedAsset:
        """Читает exact source creative; production Drive upload запрещён."""
        from integrations.facebook import get_existing_ad_creative_source

        if not (
            manifest.source_ad_id
            and manifest.source_adset_id
            and manifest.expected_source_adset_name
        ):
            raise RecoveryError("production_recovery_source_only")
        source = get_existing_ad_creative_source(
            manifest.source_ad_id,
            manifest.account_id,
            manifest.source_adset_id,
            manifest.expected_source_adset_name,
            manifest.adset_type,
        )
        _validate_source_identity(manifest, source)
        return PreparedAsset(
            media_type="existing_creative",
            image_hash="",
            creative_id=source["creative_id"],
            source_name=source["name"],
            gateway_idempotency_key=str(uuid.uuid4()),
        )

    def revalidate_prepared(
        self, manifest: RecoveryManifest, prepared: PreparedAsset
    ) -> None:
        if prepared.media_type != "existing_creative":
            return
        from integrations.facebook import get_existing_ad_creative_source

        if not manifest.source_ad_id:
            raise RecoveryError("source_ad_id отсутствует у existing creative")
        source = get_existing_ad_creative_source(
            manifest.source_ad_id,
            manifest.account_id,
            str(manifest.source_adset_id),
            str(manifest.expected_source_adset_name),
            manifest.adset_type,
        )
        _validate_source_identity(manifest, source)
        if (
            source["creative_id"] != prepared.creative_id
            or source["name"] != prepared.source_name
        ):
            raise RecoveryError("source creative изменился после подготовки")

    def create_asset(
        self, manifest: RecoveryManifest, prepared: PreparedAsset
    ) -> str:
        if prepared.media_type != "existing_creative" or not prepared.creative_id:
            raise RecoveryError("production_recovery_source_only")
        if not prepared.gateway_idempotency_key:
            raise RecoveryError("asset_recovery_gateway_id_missing")
        from services.action_gateway import execute_action
        from integrations.facebook import get_existing_ad_creative_source
        from services.approval_checker_models import (
            ActionKind,
            ActionOrigin,
            ActionResult,
            AssetRecoveryManifest,
        )
        from services.approval_source_facebook import (
            asset_recovery_inventory_sha256,
            asset_recovery_source_sha256,
        )
        from services.launch_repository import normalize_launch_name

        source = get_existing_ad_creative_source(
            str(manifest.source_ad_id),
            manifest.account_id,
            str(manifest.source_adset_id),
            str(manifest.expected_source_adset_name),
            manifest.adset_type,
        )
        _validate_source_identity(manifest, source)
        target = self.get_adset_info(manifest.adset_id)
        _validate_inventory(manifest, target)
        capacity = MAX_ADS_PER_ADSET - int(target["ad_count"])
        idempotency_key = prepared.gateway_idempotency_key
        action = AssetRecoveryManifest(
            kind=ActionKind.ASSET_RECOVERY,
            manifest_id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"acme:asset-recovery:{idempotency_key}:{manifest.key}",
                )
            ),
            origin=ActionOrigin.ASSET_RECOVERY,
            idempotency_key=idempotency_key,
            prepared_at=self.now(),
            account_kind=manifest.account_kind,
            account_id=manifest.account_id,
            campaign_type="asset_recovery",
            source="RECOVERY",
            city=manifest.city,
            source_ad_id=str(manifest.source_ad_id),
            source_adset_id=str(manifest.source_adset_id),
            source_adset_name=str(manifest.expected_source_adset_name),
            adset_type=manifest.adset_type,
            source_ad_name=str(source["name"]),
            source_creative_id=str(source["creative_id"]),
            source_identity_sha256=asset_recovery_source_sha256(source),
            target_adset_id=manifest.adset_id,
            target_adset_name=manifest.expected_adset_name,
            target_ad_name=manifest.expected_ad_name,
            target_identity_key=normalize_launch_name(manifest.expected_ad_name),
            pre_inventory_sha256=asset_recovery_inventory_sha256(target),
            capacity_available=capacity,
            hard_reserve_slots=_asset_recovery_hard_reserve_slots(),
        )
        run = execute_action(action, action.prepared_at)
        if (
            run.result is not ActionResult.CONFIRMED
            or len(run.executions) != 1
            or len(run.executions[0].created_ids) != 1
            or not run.executions[0].created_ids[0].isdigit()
        ):
            raise RecoveryGatewayRequired(run.operation_id)
        return run.executions[0].created_ids[0]

    def confirm_existing_target(
        self,
        manifest: RecoveryManifest,
        target_ad_id: str,
    ) -> bool:
        """Подтверждает pre-existing target только через exact source binding."""

        from integrations.facebook import get_existing_ad_creative_source

        source = get_existing_ad_creative_source(
            str(manifest.source_ad_id),
            manifest.account_id,
            str(manifest.source_adset_id),
            str(manifest.expected_source_adset_name),
            manifest.adset_type,
        )
        _validate_source_identity(manifest, source)
        target = self.get_adset_info(manifest.adset_id)
        if (
            target.get("adset_effective_status") != "ACTIVE"
            or str(target.get("account_id") or "").removeprefix("act_")
            != manifest.account_id
        ):
            return False
        matches = [
            row
            for row in target.get("ads", [])
            if isinstance(row, dict)
            and str(row.get("id") or "") == target_ad_id
            and row.get("name") == manifest.expected_ad_name
        ]
        if len(matches) != 1:
            return False
        creative = matches[0].get("creative")
        return (
            isinstance(creative, dict)
            and str(creative.get("id") or "") == source["creative_id"]
            and matches[0].get("status") == "ACTIVE"
            and matches[0].get("effective_status") == "ACTIVE"
        )

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class RecoveryLedger:
    """Строгий durable JSON ledger: повреждение всегда блокирует CREATE."""

    def __init__(self, path: Path = DEFAULT_LEDGER_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            with self._process_lock():
                attempts = self._load()["attempts"]
                entry = attempts.get(key)
                if entry is None:
                    return None
                if not isinstance(entry, dict):
                    raise RecoveryLedgerError("ledger attempt не является объектом")
                return dict(entry)

    def put(self, manifest: RecoveryManifest, phase: str, **fields: Any) -> None:
        with self._lock:
            with self._process_lock():
                state = self._load()
                now_iso = datetime.now(timezone.utc).isoformat()
                existing = state["attempts"].get(manifest.key)
                entry = dict(existing) if isinstance(existing, dict) else {}
                entry.update(
                    {
                        "manifest": asdict(manifest),
                        "phase": phase,
                        "updated_at": now_iso,
                        **fields,
                    }
                )
                entry.setdefault("prepared_at", now_iso)
                state["attempts"][manifest.key] = entry
                self._save(state)

    @contextmanager
    def _process_lock(self):
        """Сериализует read-modify-write ledger между CLI-процессами."""
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "attempts": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RecoveryLedgerError("recovery ledger нечитаем") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise RecoveryLedgerError("неподдерживаемая схема recovery ledger")
        if not isinstance(value.get("attempts"), dict):
            raise RecoveryLedgerError("recovery ledger attempts не является объектом")
        return value

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temp_path.open("w", encoding="utf-8") as output:
                json.dump(state, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                logger.warning(
                    "asset recovery ledger temp cleanup failed: %s",
                    type(cleanup_error).__name__,
                )
            raise RecoveryLedgerError("не удалось записать recovery ledger") from exc


@dataclass(frozen=True, slots=True)
class _TargetDecision:
    state: Literal["ABSENT", "ACTIVE", "BLOCKED"]
    reason: str
    ad_id: str | None = None


def recover_one(
    manifest: RecoveryManifest,
    *,
    backend: RecoveryBackend | None = None,
    ledger: RecoveryLedger | None = None,
) -> RecoveryResult:
    """Восстанавливает одну exact цель без автоматического retry.

    Повторный вызов сначала делает live reconciliation. После фазы CREATING или
    BLOCKED новый CREATE запрещён, пока exact target не найден как единственный
    effective ACTIVE внутри разрешённого временного окна.
    """
    manifest.validate()
    backend = backend or ProductionRecoveryBackend()
    ledger = ledger or RecoveryLedger()
    if isinstance(backend, ProductionRecoveryBackend) and not manifest.source_ad_id:
        return _block(ledger, manifest, "production_recovery_source_only")
    gateway_authoritative = (
        isinstance(backend, ProductionRecoveryBackend)
        and manifest.source_ad_id is not None
    )
    previous = ledger.get(manifest.key)
    if gateway_authoritative and previous:
        operation_id = previous.get("gateway_operation_id")
        if isinstance(operation_id, str) and operation_id:
            try:
                from services.action_gateway import reconcile_operation
                from services.approval_checker_models import ActionResult

                gateway_run = reconcile_operation(operation_id, backend.now())
            except Exception as exc:
                return _block(
                    ledger,
                    manifest,
                    "asset_recovery_gateway_reconciliation_required",
                    type(exc).__name__,
                    gateway_operation_id=operation_id,
                )
            if (
                gateway_run.result is ActionResult.CONFIRMED
                and len(gateway_run.executions) == 1
                and len(gateway_run.executions[0].created_ids) == 1
                and gateway_run.executions[0].created_ids[0].isdigit()
            ):
                ad_id = gateway_run.executions[0].created_ids[0]
                ledger.put(
                    manifest,
                    "SUCCEEDED",
                    reason="gateway_reconciled",
                    ad_id=ad_id,
                    gateway_operation_id=operation_id,
                )
                return RecoveryResult(
                    manifest.key,
                    manifest.city,
                    manifest.adset_id,
                    "SUCCEEDED",
                    "gateway_reconciled",
                    ad_id,
                )
            return _block(
                ledger,
                manifest,
                "asset_recovery_gateway_reconciliation_required",
                gateway_operation_id=operation_id,
            )

    with backend.account_scope(manifest.account_kind):
        try:
            _verify_account(manifest, backend)
        except Exception as exc:
            return _block(ledger, manifest, "account_mismatch", type(exc).__name__)
        with backend.adset_lock(manifest.adset_id):
            try:
                _verify_account(manifest, backend)
            except Exception as exc:
                return _block(ledger, manifest, "account_mismatch", type(exc).__name__)
            try:
                initial_info = backend.get_adset_info(manifest.adset_id)
                _validate_inventory(manifest, initial_info)
            except Exception as exc:
                return _block(
                    ledger, manifest, "initial_inventory_failed", type(exc).__name__
                )

            target = _reconcile_target(manifest, initial_info)
            if target.state == "ACTIVE":
                if gateway_authoritative:
                    try:
                        confirmed = backend.confirm_existing_target(  # type: ignore[attr-defined]
                            manifest,
                            str(target.ad_id),
                        )
                    except Exception as exc:
                        return _block(
                            ledger,
                            manifest,
                            "existing_target_source_binding_failed",
                            type(exc).__name__,
                        )
                    if not confirmed:
                        return _block(
                            ledger,
                            manifest,
                            "existing_target_creative_mismatch",
                        )
                ledger.put(
                    manifest,
                    "SUCCEEDED",
                    reason="already_exists_reconciled",
                    ad_id=target.ad_id,
                )
                return RecoveryResult(
                    manifest.key,
                    manifest.city,
                    manifest.adset_id,
                    "SUCCEEDED",
                    "already_exists_reconciled",
                    target.ad_id,
                )
            if target.state == "BLOCKED":
                return _block(ledger, manifest, target.reason)

            time_reason = _current_time_reason(manifest, backend.now())
            if time_reason:
                return _block(ledger, manifest, time_reason)

            if previous and previous.get("phase") in {"CREATING", "BLOCKED", "SUCCEEDED"}:
                return _block(ledger, manifest, "previous_attempt_target_absent")

            preflight_reason = _preflight_reason(manifest, initial_info)
            if preflight_reason:
                return _block(ledger, manifest, preflight_reason)

            ledger.put(manifest, "PREPARED", reason="preflight_passed")
            try:
                prepared = backend.prepare_asset(manifest)
            except Exception as exc:
                return _block(
                    ledger, manifest, "asset_prepare_failed", type(exc).__name__
                )
            if prepared.gateway_idempotency_key is not None:
                ledger.put(
                    manifest,
                    "PREPARED",
                    reason="gateway_attempt_reserved",
                    gateway_idempotency_key=prepared.gateway_idempotency_key,
                )

            # Последний snapshot непосредственно перед CREATE остаётся под тем же
            # межпроцессным adset lock. Любое изменение инвентаря блокирует вызов.
            try:
                final_info = backend.get_adset_info(manifest.adset_id)
                _validate_inventory(manifest, final_info)
            except Exception as exc:
                return _block(
                    ledger, manifest, "final_inventory_failed", type(exc).__name__
                )
            final_target = _reconcile_target(manifest, final_info)
            if final_target.state == "ACTIVE":
                if gateway_authoritative:
                    try:
                        confirmed = backend.confirm_existing_target(  # type: ignore[attr-defined]
                            manifest,
                            str(final_target.ad_id),
                        )
                    except Exception as exc:
                        return _block(
                            ledger,
                            manifest,
                            "appeared_target_source_binding_failed",
                            type(exc).__name__,
                        )
                    if not confirmed:
                        return _block(
                            ledger,
                            manifest,
                            "appeared_target_creative_mismatch",
                        )
                ledger.put(
                    manifest,
                    "SUCCEEDED",
                    reason="appeared_before_create",
                    ad_id=final_target.ad_id,
                )
                return RecoveryResult(
                    manifest.key,
                    manifest.city,
                    manifest.adset_id,
                    "SUCCEEDED",
                    "appeared_before_create",
                    final_target.ad_id,
                )
            if final_target.state == "BLOCKED":
                return _block(ledger, manifest, final_target.reason)
            final_reason = _preflight_reason(manifest, final_info)
            if final_reason:
                return _block(ledger, manifest, final_reason)

            try:
                backend.revalidate_prepared(manifest, prepared)
            except Exception as exc:
                return _block(
                    ledger,
                    manifest,
                    "source_revalidation_failed",
                    type(exc).__name__,
                )

            time_reason = _current_time_reason(manifest, backend.now())
            if time_reason:
                return _block(ledger, manifest, time_reason)

            ledger.put(
                manifest,
                "CREATING",
                reason="create_started",
                gateway_idempotency_key=prepared.gateway_idempotency_key,
            )
            returned_ad_id: str | None = None
            create_error_type: str | None = None
            try:
                raw_ad_id = backend.create_asset(manifest, prepared)
                if isinstance(raw_ad_id, str) and raw_ad_id.strip():
                    returned_ad_id = raw_ad_id.strip()
                else:
                    create_error_type = "InvalidCreateResponse"
            except RecoveryGatewayRequired as exc:
                return _block(
                    ledger,
                    manifest,
                    "asset_recovery_gateway_reconciliation_required",
                    gateway_operation_id=exc.operation_id,
                )
            except Exception as exc:
                create_error_type = type(exc).__name__

            try:
                outcome_info = backend.get_adset_info(manifest.adset_id)
                _validate_inventory(manifest, outcome_info)
                outcome = _reconcile_target(manifest, outcome_info)
            except Exception as exc:
                return _block(
                    ledger,
                    manifest,
                    "outcome_inventory_failed",
                    type(exc).__name__,
                )

            if outcome.state != "ACTIVE":
                reason = (
                    "create_exception_target_absent"
                    if create_error_type and outcome.state == "ABSENT"
                    else outcome.reason
                )
                return _block(ledger, manifest, reason, create_error_type)
            if returned_ad_id and returned_ad_id != outcome.ad_id:
                return _block(ledger, manifest, "returned_ad_id_mismatch")

            success_reason = (
                "create_exception_reconciled"
                if create_error_type
                else "created_and_reconciled"
            )
            ledger.put(
                manifest,
                "SUCCEEDED",
                reason=success_reason,
                ad_id=outcome.ad_id,
                create_error_type=create_error_type,
            )
            return RecoveryResult(
                manifest.key,
                manifest.city,
                manifest.adset_id,
                "SUCCEEDED",
                success_reason,
                outcome.ad_id,
            )


def recover_sequential(
    manifests: list[RecoveryManifest],
    *,
    backend: RecoveryBackend | None = None,
    ledger: RecoveryLedger | None = None,
) -> list[RecoveryResult]:
    """Обрабатывает города строго по порядку; первый BLOCKED останавливает batch."""
    if not manifests:
        raise ManifestValidationError("список manifests пуст")
    keys = [manifest.key for manifest in manifests]
    if len(keys) != len(set(keys)):
        raise ManifestValidationError("batch содержит дубли exact manifest")
    backend = backend or ProductionRecoveryBackend()
    ledger = ledger or RecoveryLedger()
    results: list[RecoveryResult] = []
    for manifest in manifests:
        result = recover_one(manifest, backend=backend, ledger=ledger)
        results.append(result)
        if result.status == "BLOCKED":
            break
    return results


def _verify_account(manifest: RecoveryManifest, backend: RecoveryBackend) -> None:
    account_id = str(backend.current_account_id()).removeprefix("act_")
    if account_id != manifest.account_id:
        raise ManifestValidationError("активный FB account не совпадает с manifest")


def _validate_inventory(
    manifest: RecoveryManifest, info: dict[str, Any]
) -> None:
    if not isinstance(info, dict):
        raise RecoveryError("inventory не является объектом")
    if str(info.get("adset_id", "")) != manifest.adset_id:
        raise RecoveryError("inventory относится к другому adset")
    if info.get("name") != manifest.expected_adset_name:
        raise RecoveryError("inventory adset name не совпадает с manifest")
    _validate_adset_identity(
        manifest.expected_adset_name,
        manifest.city,
        manifest.adset_type,
        field_name="expected_adset_name",
    )
    if info.get("inventory_complete") is not True:
        raise RecoveryError("inventory incomplete")
    unknown = info.get("unknown_effective_status_ids")
    if not isinstance(unknown, list) or unknown:
        raise RecoveryError("inventory содержит неизвестные effective status")
    ads = info.get("ads")
    if not isinstance(ads, list) or any(not isinstance(ad, dict) for ad in ads):
        raise RecoveryError("inventory ads не является полным списком")
    if type(info.get("ad_count")) is not int or info["ad_count"] != len(ads):
        raise RecoveryError("inventory ad_count не совпадает с ads")
    active_count = info.get("effective_active_count")
    if type(active_count) is not int or active_count < 0:
        raise RecoveryError("inventory effective_active_count некорректен")


def _reconcile_target(
    manifest: RecoveryManifest, info: dict[str, Any]
) -> _TargetDecision:
    exact = [
        ad for ad in info["ads"]
        if str(ad.get("name", "")) == manifest.expected_ad_name
    ]
    if not exact:
        return _TargetDecision("ABSENT", "target_absent")
    if len(exact) != 1:
        return _TargetDecision("BLOCKED", "target_exact_count_not_one")
    target = exact[0]
    created_at = _parse_fb_time(target.get("created_time"))
    if created_at is None:
        return _TargetDecision("BLOCKED", "target_created_time_invalid")
    if not (manifest.start_at <= created_at <= manifest.end_at):
        return _TargetDecision("BLOCKED", "target_outside_manifest_time_window")
    if target.get("status") != "ACTIVE" or target.get("effective_status") != "ACTIVE":
        return _TargetDecision("BLOCKED", "target_not_effective_active")
    ad_id = target.get("id")
    if not isinstance(ad_id, str) or not ad_id:
        return _TargetDecision("BLOCKED", "target_id_invalid")
    return _TargetDecision("ACTIVE", "target_effective_active", ad_id)


def _preflight_reason(
    manifest: RecoveryManifest, info: dict[str, Any]
) -> str | None:
    if info["effective_active_count"] < 1:
        return "zero_effective_active"
    if MAX_ADS_PER_ADSET - info["ad_count"] < 1:
        return "no_free_slot"
    if manifest.source_ad_id and not manifest.sibling_expected_ad_name:
        # В source-режиме exact ACTIVE source заменяет sibling-ассет; он ещё
        # раз проверяется непосредственно перед CREATE.
        return None
    siblings = [
        ad for ad in info["ads"]
        if str(ad.get("name", "")) == manifest.sibling_expected_ad_name
    ]
    if len(siblings) != 1:
        return "sibling_exact_count_not_one"
    sibling = siblings[0]
    if sibling.get("status") != "ACTIVE" or sibling.get("effective_status") != "ACTIVE":
        return "sibling_not_effective_active"
    return None


def _validate_source_identity(
    manifest: RecoveryManifest,
    source: dict[str, Any],
) -> None:
    """Source и target должны отличаться только префиксом города."""
    if (
        source.get("ad_id") != manifest.source_ad_id
        or source.get("account_id") != manifest.account_id
        or source.get("adset_id") != manifest.source_adset_id
        or source.get("adset_name") != manifest.expected_source_adset_name
        or source.get("status") != "ACTIVE"
        or source.get("effective_status") != "ACTIVE"
    ):
        raise RecoveryError("source identity не совпадает с manifest")
    source_name = source.get("name")
    if not isinstance(source_name, str):
        raise RecoveryError("source name отсутствует")
    if _city_stripped_name(source_name) != _city_stripped_name(
        manifest.expected_ad_name
    ):
        raise RecoveryError("source и target имеют разную exact identity")


def _city_stripped_name(ad_name: str) -> str:
    parts = ad_name.split(" | ", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise RecoveryError("имя объявления не содержит точный city prefix")
    return parts[1]


def _validate_adset_type_token(
    adset_name: str,
    adset_type: str,
    *,
    field_name: str,
) -> None:
    """Проверяет тип adset как отдельный Unicode-сегмент."""
    if not _contains_unicode_segment(adset_name, adset_type):
        raise ManifestValidationError(
            f"{field_name} не содержит exact token типа {adset_type}"
        )


def _validate_adset_identity(
    adset_name: str,
    city: str,
    adset_type: str,
    *,
    field_name: str,
) -> None:
    """Привязывает ожидаемое имя adset к exact городу и типу."""
    _validate_adset_type_token(
        adset_name,
        adset_type,
        field_name=field_name,
    )
    if not _contains_unicode_segment(adset_name, city):
        raise ManifestValidationError(
            f"{field_name} не содержит отдельный сегмент города {city}"
        )


def _contains_unicode_segment(text: str, expected: str) -> bool:
    """Ищет значение по Unicode-границам, не требуя конкретных разделителей."""
    value = expected.strip()
    if not value:
        return False
    return re.search(
        rf"(?<!\w){re.escape(value)}(?!\w)",
        text,
        flags=re.IGNORECASE,
    ) is not None


def _current_time_reason(
    manifest: RecoveryManifest,
    now: datetime,
) -> str | None:
    if now.tzinfo is None:
        return "backend_now_not_timezone_aware"
    if not (manifest.start_at <= now <= manifest.end_at):
        return "outside_manifest_time_window"
    return None


def _block(
    ledger: RecoveryLedger,
    manifest: RecoveryManifest,
    reason: str,
    error_type: str | None = None,
    **fields: Any,
) -> RecoveryResult:
    ledger.put(
        manifest,
        "BLOCKED",
        reason=reason,
        error_type=error_type,
        **fields,
    )
    return RecoveryResult(
        manifest.key,
        manifest.city,
        manifest.adset_id,
        "BLOCKED",
        reason,
    )


def _parse_aware_time(raw: str, field_name: str) -> datetime:
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestValidationError(f"{field_name} не является ISO datetime") from exc
    if value.tzinfo is None:
        raise ManifestValidationError(f"{field_name} должен содержать timezone")
    return value


def _parse_fb_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("+0000", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def load_manifest_file(path: Path) -> list[RecoveryManifest]:
    """Читает один manifest или JSON-массив manifests для sequential запуска."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ManifestValidationError("manifest-файл нечитаем") from exc
    items = raw if isinstance(raw, list) else [raw]
    if not items:
        raise ManifestValidationError("manifest-файл содержит пустой список")
    return [RecoveryManifest.from_dict(item) for item in items]


def result_to_dict(result: RecoveryResult) -> dict[str, Any]:
    return asdict(result)
