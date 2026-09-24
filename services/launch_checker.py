"""Fail-closed launch checker and deterministic provider launch plan.

Модуль не знает про HTTP, Trello и Facebook. Чтение media, target adsets и live
inventory передаётся через зависимости, поэтому правила можно проверять без сети.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import uuid
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from collections.abc import Callable
from typing import Any, ContextManager, Iterator, Literal, Mapping, Protocol, Sequence

from services.launch_repository import launch_identity_key, normalize_launch_name


HARD_RESERVE_SLOTS = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HASHED_ACTOR_RE = re.compile(r"^api-key:sha256:[0-9a-f]{64}$")
# Тема-вето: карточку с этим словом в названии автоматически не запускаем
# (та же тема, что _VETO_KEYWORD в services/decision_policy.py). Коды отказа
# TOPIC_VETO / override_topic_veto — исторические имена контракта API.
VETO_TOPIC_KEYWORD = "бонус"
_VETO_TOPIC_RE = re.compile(rf"(?<!\w){re.escape(VETO_TOPIC_KEYWORD)}", re.IGNORECASE)
_OPEN_ATTEMPT_PHASES = frozenset({"LAUNCHING", "RECONCILING", "PARTIAL"})


class LaunchCheckBlocked(RuntimeError):
    """Типизированный отказ checker-а, безопасный для API/launcher слоёв."""

    def __init__(
        self,
        code: str,
        reasons: tuple[str, ...] | Sequence[str],
        check_id: str | None = None,
    ) -> None:
        self.code = _required_text(code, "code")
        self.reasons = tuple(str(reason) for reason in reasons)
        self.check_id = _optional_text(check_id)
        super().__init__(f"{self.code}: {'; '.join(self.reasons)}")


class LaunchSource(StrEnum):
    CRON = "CRON"
    AUTO_LAUNCH_NOW = "AUTO_LAUNCH_NOW"
    MANUAL = "MANUAL"
    BATCH = "BATCH"
    AGENT_RUN = "AGENT_RUN"
    RECOVERY = "RECOVERY"


class CheckerMode(StrEnum):
    OBSERVE = "observe"
    ENFORCE = "enforce"


class LaunchCheckStatus(StrEnum):
    ELIGIBLE = "eligible"
    BLOCKED = "blocked"
    NEEDS_FULL_PREFLIGHT = "needs_full_preflight"


@dataclass(frozen=True, slots=True)
class ProviderLaunchAuthorization:
    auth_id: str
    secret: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(auth_id={self.auth_id!r}, "
            "secret='<redacted>')"
        )


@dataclass(frozen=True, slots=True)
class ProviderCreateScope:
    account_kind: Literal["offline", "online"]
    account_id: str
    city: str
    adset_id: str
    ad_name: str
    media_sha256: str


@dataclass(frozen=True, slots=True)
class LaunchCheckRequest:
    source: LaunchSource
    campaign_type: str
    cities: tuple[str, ...] | None
    as_carousel: bool
    override_topic_veto: bool = False
    override_reason: str | None = None
    actor: str = "system"


@dataclass(frozen=True, slots=True)
class PreparedLaunchMedia:
    """Канонический media payload и SHA фактических bytes/manifest."""

    media: Mapping[str, Any]
    media_sha256: str


@dataclass(frozen=True, slots=True)
class LaunchTarget:
    city: str
    ordinal: int
    account_kind: Literal["offline", "online"]
    account_id: str
    adset_id: str
    expected_names: tuple[str, ...]
    identity_key: str = ""
    reserved_slots: int = 0


@dataclass(frozen=True, slots=True)
class LiveAd:
    ad_id: str
    name: str


@dataclass(frozen=True, slots=True)
class LiveAdsetInventory:
    adset_id: str
    effective_status: str
    inventory_complete: bool
    ads: tuple[LiveAd, ...]
    ad_count: int
    max_ads: int
    available: int
    other_reserved_slots: int = 0


@dataclass(frozen=True, slots=True)
class LaunchCheckResult:
    check_id: str
    card_id: str
    status: LaunchCheckStatus
    reason_codes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    topic_override_available: bool = False

    @property
    def allowed(self) -> bool:
        return self.status is not LaunchCheckStatus.BLOCKED


@dataclass(frozen=True, slots=True)
class CheckedLaunchPlan:
    check_id: str
    card_id: str
    card_name: str
    trello_pos: float
    request: LaunchCheckRequest
    product: str
    language: Literal["L2", "L1"]
    media: Mapping[str, Any]
    media_sha256: str
    targets: tuple[LaunchTarget, ...]
    expected_names_by_city: Mapping[str, tuple[str, ...]]
    authorization: ProviderLaunchAuthorization | None
    plan_sha256: str
    recovery_plan_id: str | None = None
    # Города, пропущенные гейтом автозапуска: (город, код, причина).
    # ALREADY_EXISTS / DUPLICATE_LIVE — город уже запущен прошлым прогоном; CAPACITY_BLOCKED — нет слотов.
    skipped_targets: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class LaunchAuditEvent:
    check_id: str
    event_type: str
    source: str
    card_id: str
    actor: str
    auth_id: str | None = None
    reason_codes: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    event_id: str = field(default_factory=lambda: f"launch-event-{uuid.uuid4().hex}")


class MediaPreparer(Protocol):
    def __call__(
        self, card: Mapping[str, Any], request: LaunchCheckRequest
    ) -> PreparedLaunchMedia: ...


class LanguageResolver(Protocol):
    def __call__(self, card: Mapping[str, Any]) -> Literal["L2", "L1"]: ...


class ProductResolver(Protocol):
    def __call__(self, card: Mapping[str, Any]) -> str: ...


class TargetResolver(Protocol):
    def __call__(
        self,
        card: Mapping[str, Any],
        request: LaunchCheckRequest,
        language: Literal["L2", "L1"],
        product: str,
        media: PreparedLaunchMedia,
    ) -> Sequence[LaunchTarget]: ...


class InventoryReader(Protocol):
    def __call__(self, target: LaunchTarget) -> LiveAdsetInventory: ...


class AdsetLockFactory(Protocol):
    def __call__(self, adset_id: str) -> ContextManager[Any]: ...


class RepositoryAdapter(Protocol):
    def reserve_authorization(
        self, plan: CheckedLaunchPlan, secret_sha256: str, now: datetime
    ) -> None: ...

    def append_launch_audit(self, event: LaunchAuditEvent) -> None: ...

    def validate_authorization_media(
        self,
        proof: ProviderLaunchAuthorization,
        actual_media_sha256: str,
        now: datetime,
    ) -> object: ...


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} не должен быть пустым")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _sha256(value: object, field_name: str) -> str:
    digest = _required_text(value, field_name).lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError(f"{field_name} должен быть SHA-256 hex")
    return digest


def hashed_api_key_actor(api_key: str) -> str:
    """Возвращает audit actor без хранения исходного API key."""
    key = _required_text(api_key, "api_key")
    return f"api-key:sha256:{hashlib.sha256(key.encode()).hexdigest()}"


def _blocked(
    check_id: str, card_id: str, code: str, reason: str, *, veto: bool = False
) -> LaunchCheckResult:
    return LaunchCheckResult(
        check_id=check_id,
        card_id=card_id,
        status=LaunchCheckStatus.BLOCKED,
        reason_codes=(code,),
        reasons=(reason,),
        topic_override_available=veto,
    )


def _valid_ad_ids(entry: Mapping[str, Any]) -> bool:
    ad_ids = entry.get("ad_ids")
    return (
        isinstance(ad_ids, Sequence)
        and not isinstance(ad_ids, (str, bytes))
        and bool(ad_ids)
        and all(isinstance(ad_id, str) and bool(ad_id.strip()) for ad_id in ad_ids)
    )


def _resume_ready(card_id: str, state: Mapping[str, Any]) -> bool:
    """Частичный запуск сверен и ротирован на дозапуск: попытка PREPARED/FAILED_RETRYABLE с городами
    без объявлений (auto_launch.attempt_resumable). Дубли всё равно ловят гейт инвентаря и граница FB."""
    from services.auto_launch import attempt_resumable

    attempts = state.get("launch_attempts", {})
    if not isinstance(attempts, Mapping):
        return False
    return any(
        isinstance(attempt, Mapping)
        and str(attempt.get("card_id") or "") == card_id
        and str(attempt.get("phase") or "").upper() in {"PREPARED", "FAILED_RETRYABLE"}
        and attempt_resumable(dict(attempt))
        for attempt in attempts.values()
    )


def _state_result(
    card_id: str, check_id: str, state: Mapping[str, Any]
) -> LaunchCheckResult | None:
    launched = state.get("launched_ever", {})
    if not isinstance(launched, Mapping):
        return _blocked(check_id, card_id, "STATE_INVALID", "launched_ever повреждён")
    entry = launched.get(card_id)
    if isinstance(entry, str):
        return _blocked(
            check_id,
            card_id,
            "UNKNOWN_LEGACY",
            "Старая запись запуска не содержит подтверждённых ad_id",
        )
    if entry is not None and not isinstance(entry, Mapping):
        return _blocked(check_id, card_id, "STATE_INVALID", "Запись запуска повреждена")
    if isinstance(entry, Mapping):
        if entry.get("legacy_unverified") is True or (
            entry.get("complete") is True and not _valid_ad_ids(entry)
        ):
            return _blocked(
                check_id,
                card_id,
                "UNKNOWN_LEGACY",
                "Запуск не подтверждён точными ad_id",
            )
        if _valid_ad_ids(entry) and entry.get("complete") is True:
            return _blocked(
                check_id, card_id, "ALREADY_LAUNCHED", "Карточка уже полностью запущена"
            )
        if _valid_ad_ids(entry) and not _resume_ready(card_id, state):
            return _blocked(
                check_id,
                card_id,
                "RECONCILIATION_REQUIRED",
                "Частичный запуск требует exact reconciliation",
            )
    attempts = state.get("launch_attempts", {})
    if isinstance(attempts, Mapping):
        for attempt in attempts.values():
            if (
                isinstance(attempt, Mapping)
                and str(attempt.get("card_id") or "") == card_id
                and str(attempt.get("phase") or "").upper() in _OPEN_ATTEMPT_PHASES
            ):
                return _blocked(
                    check_id,
                    card_id,
                    "RECONCILIATION_REQUIRED",
                    "Есть незавершённая durable попытка запуска",
                )
    return None


def check_candidate(
    card: Mapping[str, Any],
    request: LaunchCheckRequest,
    state: Mapping[str, Any],
    *,
    check_id: str | None = None,
) -> LaunchCheckResult:
    """Чистая быстрая проверка state/veto до внешнего preflight."""
    if check_id is None:
        identity = {
            "card_id": str(card.get("id") or "") if isinstance(card, Mapping) else "",
            "source": request.source.value,
            "campaign_type": request.campaign_type,
            "cities": request.cities,
            "as_carousel": request.as_carousel,
            "override_topic_veto": request.override_topic_veto,
            "actor": request.actor,
        }
        digest = hashlib.sha256(
            json.dumps(
                identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        current_check_id = f"launch-check-{digest[:32]}"
    else:
        current_check_id = check_id
    if not isinstance(card, Mapping) or not isinstance(state, Mapping):
        return _blocked(current_check_id, "unknown", "INPUT_INVALID", "Неверный payload")
    card_id = str(card.get("id") or "").strip()
    card_name = str(card.get("name") or "").strip()
    raw_pos = card.get("pos")
    if (
        not card_id
        or not card_name
        or isinstance(raw_pos, bool)
        or not isinstance(raw_pos, (int, float))
        or not math.isfinite(float(raw_pos))
    ):
        return _blocked(
            current_check_id,
            card_id or "unknown",
            "CARD_INVALID",
            "Карточка не содержит валидные id/name/pos",
        )
    state_block = _state_result(card_id, current_check_id, state)
    if state_block is not None:
        return state_block

    has_veto_topic = _VETO_TOPIC_RE.search(card_name) is not None
    if request.override_reason and not request.override_topic_veto:
        return _blocked(
            current_check_id,
            card_id,
            "TOPIC_OVERRIDE_INVALID",
            "override_reason допустим только с override_topic_veto=true",
        )
    if request.override_topic_veto:
        reason = _optional_text(request.override_reason)
        valid_override = (
            has_veto_topic
            and request.source in {LaunchSource.MANUAL, LaunchSource.BATCH}
            and reason is not None
            and 10 <= len(reason) <= 300
            and _HASHED_ACTOR_RE.fullmatch(request.actor) is not None
        )
        if not valid_override:
            return _blocked(
                current_check_id,
                card_id,
                "TOPIC_OVERRIDE_INVALID",
                "Override темы-вето требует manual/batch, reason 10–300 и hashed actor",
            )
    elif has_veto_topic:
        return _blocked(
            current_check_id,
            card_id,
            "TOPIC_VETO",
            f"Карточка с темой «{VETO_TOPIC_KEYWORD}» запрещена для автоматического запуска",
            veto=request.source in {LaunchSource.MANUAL, LaunchSource.BATCH},
        )
    return LaunchCheckResult(
        check_id=current_check_id,
        card_id=card_id,
        status=LaunchCheckStatus.NEEDS_FULL_PREFLIGHT,
    )


def _canonical_targets(
    card_name: str,
    request: LaunchCheckRequest,
    raw_targets: Sequence[LaunchTarget],
) -> tuple[LaunchTarget, ...]:
    targets: list[LaunchTarget] = []
    seen_cities: set[str] = set()
    seen_ordinals: set[int] = set()
    for target in raw_targets:
        city = _required_text(target.city, "target.city")
        if city in seen_cities or target.ordinal in seen_ordinals:
            raise ValueError("targets содержат повтор city/ordinal")
        seen_cities.add(city)
        seen_ordinals.add(target.ordinal)
        if type(target.ordinal) is not int or target.ordinal < 0:
            raise ValueError("target.ordinal должен быть int >= 0")
        if target.account_kind not in {"offline", "online"}:
            raise ValueError("account_kind должен быть offline/online")
        names = tuple(_required_text(name, "ad_name") for name in target.expected_names)
        name_keys = tuple(normalize_launch_name(name) for name in names)
        if not names or len(set(name_keys)) != len(names):
            raise ValueError("expected names пусты или не уникальны после normalization")
        identity = launch_identity_key(city, card_name)
        if any(
            name_key != identity and not name_key.startswith(f"{identity} / ")
            for name_key in name_keys
        ):
            raise ValueError("expected name не принадлежит server-owned card identity")
        if target.identity_key and target.identity_key != identity:
            raise ValueError("target identity_key не server-owned")
        targets.append(
            replace(
                target,
                city=city,
                account_id=_required_text(target.account_id, "account_id"),
                adset_id=_required_text(target.adset_id, "adset_id"),
                expected_names=names,
                identity_key=identity,
                reserved_slots=len(names),
            )
        )
    if not targets:
        raise ValueError("targets пусты")
    targets.sort(key=lambda target: target.ordinal)
    # Сверка городов — по exact множеству, без порядка: с маршрутизацией
    # город→кабинет targets собираются по кабинетам (cabinet_a,
    # потом cabinet_b), и их порядок закономерно расходится с порядком
    # запрошенного списка. Пропуск или лишний город остаются отказом.
    if request.cities is not None and tuple(
        sorted(target.city for target in targets)
    ) != tuple(sorted(request.cities)):
        raise ValueError("targets не совпадают с exact selected cities")
    # Один запуск не смешивает offline и online контуры (разные токены).
    # Внутри offline допустимы НЕСКОЛЬКО кабинетов: карта маршрутизации
    # город→кабинет закрепляет exact account_id на каждом target, а живой
    # инвентарь (read_inventory) обязан доказать, что адсет реально живёт
    # в кабинете своего города.
    kinds = {target.account_kind for target in targets}
    if len(kinds) != 1:
        raise ValueError("Один запуск не может смешивать offline и online FB accounts")
    if "online" in kinds and len({target.account_id for target in targets}) != 1:
        raise ValueError("Онлайн-запуск не может смешивать FB accounts")
    if any(not target.adset_id.isdecimal() for target in targets):
        raise ValueError("adset_id должен быть numeric для canonical lock order")
    return tuple(targets)


# Отказы по одному городу, которые в режиме наблюдения пропускают город, а не всю карточку.
_CITY_SKIPPABLE_DENIALS = frozenset({"ALREADY_EXISTS", "DUPLICATE_LIVE", "CAPACITY_BLOCKED"})


def _inventory_denial(
    target: LaunchTarget, inventory: LiveAdsetInventory
) -> tuple[str, str] | None:
    prefix = f"{target.city}:"
    if (
        inventory.adset_id != target.adset_id
        or inventory.effective_status != "ACTIVE"
        or inventory.inventory_complete is not True
    ):
        return "INVENTORY_UNVERIFIED", f"{prefix} adset/inventory не подтверждён ACTIVE"
    ids = [ad.ad_id for ad in inventory.ads]
    if (
        any(not ad_id.strip() for ad_id in ids)
        or len(ids) != len(set(ids))
        or inventory.ad_count != len(inventory.ads)
        or inventory.max_ads <= 0
        or inventory.available != inventory.max_ads - inventory.ad_count
        or inventory.other_reserved_slots < 0
    ):
        return "INVENTORY_UNVERIFIED", f"{prefix} incomplete/несогласованный inventory"

    expected_keys = tuple(normalize_launch_name(name) for name in target.expected_names)
    live_keys = [normalize_launch_name(ad.name) for ad in inventory.ads]
    exact_counts = tuple(live_keys.count(key) for key in expected_keys)
    if exact_counts and all(count == 1 for count in exact_counts):
        return "ALREADY_EXISTS", f"{prefix} полный exact набор уже существует"
    root_prefix = f"{target.identity_key} / "
    same_identity = [
        key for key in live_keys if key == target.identity_key or key.startswith(root_prefix)
    ]
    if any(exact_counts) or same_identity:
        return "DUPLICATE_LIVE", f"{prefix} найден частичный/дублирующий card identity"
    required = len(target.expected_names) + HARD_RESERVE_SLOTS + inventory.other_reserved_slots
    if inventory.available < required:
        return (
            "CAPACITY_BLOCKED",
            f"{prefix} свободно {inventory.available}, требуется {required}",
        )
    return None


def _plan_sha256(
    card_id: str,
    card_name: str,
    request: LaunchCheckRequest,
    media_sha256: str,
    targets: Sequence[LaunchTarget],
) -> str:
    payload = {
        "card_id": card_id,
        "card_name": card_name,
        "source": request.source.value,
        "campaign_type": request.campaign_type,
        "media_sha256": media_sha256,
        "targets": [
            {
                "city": target.city,
                "ordinal": target.ordinal,
                "account_kind": target.account_kind,
                "account_id": target.account_id,
                "adset_id": target.adset_id,
                "identity_key": target.identity_key,
                "names": list(target.expected_names),
            }
            for target in targets
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _lock_sort_key(adset_id: str) -> tuple[int, int | str]:
    return (0, int(adset_id))


def _translate_repository_error(exc: Exception, check_id: str) -> LaunchCheckBlocked:
    code = str(getattr(exc, "code", "REPOSITORY_BLOCKED"))
    reasons = tuple(getattr(exc, "reasons", (str(exc),)))
    repository_check_id = getattr(exc, "check_id", None)
    return LaunchCheckBlocked(code, reasons, repository_check_id or check_id)


class LaunchChecker:
    """Оркестрирует preflight через внедрённые read-only границы."""

    def __init__(
        self,
        *,
        mode: CheckerMode,
        repository: RepositoryAdapter,
        prepare_media: MediaPreparer,
        resolve_language: LanguageResolver,
        resolve_product: ProductResolver,
        resolve_targets: TargetResolver,
        read_inventory: InventoryReader,
        adset_lock: AdsetLockFactory,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.mode = CheckerMode(mode)
        self.repository = repository
        self.prepare_media = prepare_media
        self.resolve_language = resolve_language
        self.resolve_product = resolve_product
        self.resolve_targets = resolve_targets
        self.read_inventory = read_inventory
        self.adset_lock = adset_lock
        self.now = now

    def _audit(
        self,
        result: LaunchCheckResult,
        request: LaunchCheckRequest,
        event_type: str,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.repository.append_launch_audit(
            LaunchAuditEvent(
                check_id=result.check_id,
                event_type=event_type,
                source=request.source.value,
                card_id=result.card_id,
                actor=request.actor,
                reason_codes=result.reason_codes,
                evidence=evidence or {},
                created_at=self.now(),
            )
        )

    def prepare_and_reserve(
        self,
        card: Mapping[str, Any],
        request: LaunchCheckRequest,
        state: Mapping[str, Any],
    ) -> CheckedLaunchPlan:
        result = check_candidate(
            card, request, state, check_id=f"launch-check-{uuid.uuid4().hex}"
        )
        self._audit(result, request, "CANDIDATE")
        if not result.allowed:
            self._audit(result, request, "DENIED")
            raise LaunchCheckBlocked(result.reason_codes[0], result.reasons, result.check_id)
        try:
            prepared = self.prepare_media(card, request)
            media_sha256 = _sha256(prepared.media_sha256, "media_sha256")
            if not isinstance(prepared.media, Mapping) or not prepared.media:
                raise ValueError("media payload пуст")
            language = self.resolve_language(card)
            if language not in {"L2", "L1"}:
                raise ValueError("language должен быть L2/L1")
            product = _required_text(self.resolve_product(card), "product")
            targets = _canonical_targets(
                str(card["name"]).strip(),
                request,
                self.resolve_targets(card, request, language, product, prepared),
            )
        except Exception as exc:
            denied = _blocked(result.check_id, result.card_id, "PREFLIGHT_INVALID", str(exc))
            self._audit(denied, request, "DENIED")
            raise LaunchCheckBlocked("PREFLIGHT_INVALID", denied.reasons, result.check_id) from exc

        plan = CheckedLaunchPlan(
            check_id=result.check_id,
            card_id=result.card_id,
            card_name=str(card["name"]).strip(),
            trello_pos=float(card["pos"]),
            request=request,
            product=product,
            language=language,
            media=prepared.media,
            media_sha256=media_sha256,
            targets=targets,
            expected_names_by_city={target.city: target.expected_names for target in targets},
            authorization=None,
            plan_sha256=_plan_sha256(
                result.card_id,
                str(card["name"]).strip(),
                request,
                media_sha256,
                targets,
            ),
        )
        distinct_ids = sorted({target.adset_id for target in targets}, key=_lock_sort_key)
        try:
            with ExitStack() as stack:
                for adset_id in distinct_ids:
                    stack.enter_context(self.adset_lock(adset_id))
                skipped: list[tuple[str, str, str]] = []
                for target in targets:
                    try:
                        inventory = self.read_inventory(target)
                    except Exception as exc:
                        denied = _blocked(
                            result.check_id,
                            result.card_id,
                            "PROVIDER_UNAVAILABLE",
                            f"{target.city}: live inventory недоступен",
                        )
                        self._audit(denied, request, "DENIED")
                        raise LaunchCheckBlocked(
                            "PROVIDER_UNAVAILABLE", denied.reasons, result.check_id
                        ) from exc
                    try:
                        denial = _inventory_denial(target, inventory)
                    except (TypeError, ValueError) as exc:
                        denied = _blocked(
                            result.check_id,
                            result.card_id,
                            "INVENTORY_UNVERIFIED",
                            f"{target.city}: malformed live inventory",
                        )
                        self._audit(denied, request, "DENIED")
                        raise LaunchCheckBlocked(
                            "INVENTORY_UNVERIFIED", denied.reasons, result.check_id
                        ) from exc
                    if denial is not None:
                        if self.mode is CheckerMode.OBSERVE and denial[0] in _CITY_SKIPPABLE_DENIALS:
                            # Гейт автозапуска: город уже запущен или без слотов — не повод
                            # держать остальные города карточки.
                            skipped.append((target.city, denial[0], denial[1]))
                            continue
                        denied = _blocked(result.check_id, result.card_id, *denial)
                        self._audit(denied, request, "DENIED")
                        raise LaunchCheckBlocked(denial[0], (denial[1],), result.check_id)
                if skipped:
                    remaining = tuple(
                        target for target in targets if target.city not in {city for city, _c, _r in skipped}
                    )
                    if not remaining:
                        code, reason = skipped[0][1], skipped[0][2]
                        denied = _blocked(result.check_id, result.card_id, code, reason)
                        self._audit(denied, request, "DENIED")
                        raise LaunchCheckBlocked(code, tuple(r for _c, _k, r in skipped), result.check_id)
                    targets = remaining
                    plan = replace(
                        plan,
                        targets=remaining,
                        expected_names_by_city={target.city: target.expected_names for target in remaining},
                        plan_sha256=_plan_sha256(
                            result.card_id, str(card["name"]).strip(), request, media_sha256, remaining
                        ),
                        skipped_targets=tuple(skipped),
                    )
                eligible = replace(result, status=LaunchCheckStatus.ELIGIBLE)
                self._audit(
                    eligible,
                    request,
                    "PREFLIGHT",
                    evidence={"targets": len(targets), "mode": self.mode.value},
                )
                if self.mode is CheckerMode.OBSERVE:
                    return plan
                proof = ProviderLaunchAuthorization(
                    auth_id=f"launch-auth-{uuid.uuid4().hex}",
                    secret=secrets.token_urlsafe(32),
                )
                authorized = replace(plan, authorization=proof)
                self.repository.reserve_authorization(
                    authorized,
                    hashlib.sha256(proof.secret.encode()).hexdigest(),
                    self.now(),
                )
                return authorized
        except LaunchCheckBlocked:
            raise
        except Exception as exc:
            raise _translate_repository_error(exc, result.check_id) from exc


def prepare_and_reserve(
    card: Mapping[str, Any],
    request: LaunchCheckRequest,
    state: Mapping[str, Any],
    *,
    checker: LaunchChecker,
) -> CheckedLaunchPlan:
    """Явная integration-функция без скрытого глобального checker state."""
    return checker.prepare_and_reserve(card, request, state)


_BOUND_AUTHORIZATION: ContextVar[ProviderLaunchAuthorization | None] = ContextVar(
    "launch_provider_authorization", default=None
)


@contextmanager
def bind_provider_authorization(
    proof: ProviderLaunchAuthorization,
) -> Iterator[None]:
    """Временно связывает proof с низкоуровневым provider CREATE контекстом."""
    if not isinstance(proof, ProviderLaunchAuthorization):
        raise LaunchCheckBlocked("INVALID_AUTHORIZATION", ("Proof имеет неверный тип",), None)
    _required_text(proof.auth_id, "proof.auth_id")
    _required_text(proof.secret, "proof.secret")
    token = _BOUND_AUTHORIZATION.set(proof)
    try:
        yield
    finally:
        _BOUND_AUTHORIZATION.reset(token)


def get_bound_provider_authorization() -> ProviderLaunchAuthorization:
    proof = _BOUND_AUTHORIZATION.get()
    if proof is None:
        raise LaunchCheckBlocked(
            "AUTHORIZATION_REQUIRED", ("Provider CREATE вызван без bound proof",), None
        )
    return proof


def validate_authorization_media(
    proof: ProviderLaunchAuthorization,
    actual_media_sha256: str,
    *,
    repository: RepositoryAdapter,
    now: datetime | None = None,
) -> object:
    """Проверяет DB-backed proof и media до первого provider upload."""
    actual = _sha256(actual_media_sha256, "actual_media_sha256")
    try:
        return repository.validate_authorization_media(
            proof, actual, now or datetime.now(timezone.utc)
        )
    except Exception as exc:
        raise _translate_repository_error(exc, proof.auth_id) from exc


class RecoveryAuthorizationIssuer(Protocol):
    def __call__(
        self, plan_id: str, approved_manifest_sha256: str, actor: str
    ) -> ProviderLaunchAuthorization: ...


def issue_trusted_recovery_authorization(
    plan_id: str,
    approved_manifest_sha256: str,
    actor: str,
    *,
    issuer: RecoveryAuthorizationIssuer,
) -> ProviderLaunchAuthorization:
    """Recovery не имеет fallback: authorization выдаёт только durable issuer."""
    _required_text(plan_id, "plan_id")
    _sha256(approved_manifest_sha256, "approved_manifest_sha256")
    _required_text(actor, "actor")
    try:
        proof = issuer(plan_id, approved_manifest_sha256, actor)
    except Exception as exc:
        raise _translate_repository_error(exc, None or plan_id) from exc
    if not isinstance(proof, ProviderLaunchAuthorization):
        raise LaunchCheckBlocked(
            "INVALID_AUTHORIZATION", ("Recovery issuer вернул неверный proof",), None
        )
    return proof


__all__ = [
    "CheckerMode",
    "CheckedLaunchPlan",
    "HARD_RESERVE_SLOTS",
    "LaunchCheckBlocked",
    "LaunchCheckRequest",
    "LaunchCheckResult",
    "LaunchCheckStatus",
    "LaunchChecker",
    "LaunchSource",
    "LaunchTarget",
    "LiveAd",
    "LiveAdsetInventory",
    "PreparedLaunchMedia",
    "ProviderCreateScope",
    "ProviderLaunchAuthorization",
    "bind_provider_authorization",
    "check_candidate",
    "get_bound_provider_authorization",
    "hashed_api_key_actor",
    "issue_trusted_recovery_authorization",
    "launch_identity_key",
    "normalize_launch_name",
    "prepare_and_reserve",
    "validate_authorization_media",
]
