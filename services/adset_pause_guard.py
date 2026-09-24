"""Fail-closed защита последнего ACTIVE-объявления в adset.

Модуль является единственной безопасной точкой для автоматической PAUSE.
Он не удаляет объявления и не создаёт replacement workflow (это Release B).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import re
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Literal, Mapping, TypedDict, cast

from services.approval_checker_models import (
    AdStatusSnapshot,
    ActionResult,
    CheckIssue,
    PauseManifest,
    SourceSystem,
    canonical_json,
)


logger = logging.getLogger(__name__)

_MAX_INVENTORY_PAGES = 10
_BATCH_SIZE = 50
_NUMERIC_ID_RE = re.compile(r"^[0-9]+$")
_LOCKS_DIR = Path(__file__).resolve().parent.parent / "data" / "locks"
_LOCK_STATE = threading.local()
_TOMBSTONE_SCHEMA_VERSION = 1
_TOMBSTONE_TTL = timedelta(hours=1)
_TOKEN_PATTERNS = (
    re.compile(r"(?i)(access_token\s*[=:]\s*)[^&\s,\"']+"),
    re.compile(r"(?i)([\"']access_token[\"']\s*:\s*[\"'])[^\"']+"),
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~-]+"),
)


class PauseGuardError(RuntimeError):
    """Внутренняя ошибка с безопасным стабильным кодом."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class AdLiveContext(TypedDict):
    ad_id: str
    adset_id: str
    configured_status: str
    effective_status: str


# Общие exact-поля, которые есть и в точечном контексте, и в полном inventory.
_LIVE_CONTEXT_FIELDS = ("ad_id", "adset_id", "configured_status", "effective_status")


def _canonical_live_context(context: Mapping[str, object]) -> dict[str, object]:
    """Обрезает контекст до общих полей: exact несёт ещё и ``name``."""

    return {field: context.get(field) for field in _LIVE_CONTEXT_FIELDS}


class ExactAdContext(AdLiveContext, total=False):
    """Exact FB-поля объявления; ``name`` обязателен только для launch verify."""

    name: str


class AdsetInventory(TypedDict):
    adset_id: str
    active_ids: set[str]
    candidate_context: dict[str, AdLiveContext]
    inventory_context: dict[str, AdLiveContext]
    state_sha256: str
    complete: bool
    pages_read: int
    error: str | None


class PauseTombstone(TypedDict):
    state: Literal["RESERVED", "PAUSE_CONFIRMED", "AMBIGUOUS"]
    reserved_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PauseOutcome:
    ok: bool
    reason: str
    ad_id: str
    adset_id: str | None
    active_other_ids: tuple[str, ...] = ()

    @property
    def action_result(self) -> ActionResult:
        """Typed outcome для checked ACTION_RESULT без прямого транспорта."""

        return ActionResult.CONFIRMED if self.ok else ActionResult.FAILED

    @property
    def check_reason(self) -> CheckIssue | None:
        """Возвращает машинную причину отказа для будущего action gateway."""

        if self.ok:
            return None
        return _pause_issue(_legacy_reason_code(self.reason), self.reason)


@dataclass(frozen=True, slots=True)
class LockedPauseValidation:
    """Результат повторной live-проверки внутри уже взятого adset lock."""

    action_result: ActionResult | None
    check_reason: CheckIssue | None
    ad_id: str
    adset_id: str
    active_other_ids: tuple[str, ...]
    inventory_sha256: str | None

    @property
    def allowed(self) -> bool:
        return self.action_result is None and self.check_reason is None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _redact_sensitive(value: object) -> str:
    """Удаляет токены из текста, который остаётся только в локальном логе."""
    redacted = str(value)
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


def _exception_reason(prefix: str, exc: Exception) -> str:
    """Наружу уходит только стабильный код и класс исключения, без message."""
    if isinstance(exc, PauseGuardError):
        return exc.code
    return f"{prefix}:{type(exc).__name__}"


def _legacy_reason_code(reason: str) -> str:
    """Нормализует старые причины в закрытый набор машинных кодов."""

    mapping = {
        "candidate_not_active": "PAUSE_TARGET_CHANGED",
        "candidate_missing_from_active_inventory": "PAUSE_TARGET_CHANGED",
        "last_active_without_replacement": "LAST_EFFECTIVE_ACTIVE",
        "missing_candidate_context": "INVENTORY_CHANGED",
        "missing_candidate_id": "INVALID_CONTRACT",
    }
    return mapping.get(reason, "PAUSE_GUARD_DENIED")


def _pause_issue(code: str, message: str) -> CheckIssue:
    return CheckIssue(
        code=code,
        message=message,
        blocking=True,
        source=SourceSystem.FACEBOOK,
    )


def _locked_denial(
    manifest: PauseManifest,
    code: str,
    message: str,
    *,
    active_other_ids: tuple[str, ...] = (),
    inventory_sha256: str | None = None,
) -> LockedPauseValidation:
    """Формирует typed ACTION_RESULT; Telegram здесь принципиально отсутствует."""

    return LockedPauseValidation(
        action_result=ActionResult.FAILED,
        check_reason=_pause_issue(code, message),
        ad_id=manifest.ad_id,
        adset_id=manifest.adset_id,
        active_other_ids=active_other_ids,
        inventory_sha256=inventory_sha256,
    )


def _response_json(response) -> dict:
    if response.status_code != 200:
        raise PauseGuardError(f"fb_http_{response.status_code}")
    try:
        payload = response.json()
    except Exception as exc:
        raise PauseGuardError("fb_json_invalid") from exc
    if not isinstance(payload, dict):
        raise PauseGuardError("fb_payload_not_object")
    return payload


def fetch_exact_ad_contexts(
    ad_ids: list[str],
    *,
    require_names: bool = False,
) -> tuple[dict[str, ExactAdContext], str | None]:
    """Читает exact ad objects без мутаций и fail-closed проверяет cardinality.

    ``require_names`` включается для replacement verify: созданные IDs должны
    совпасть не только с adset, но и с immutable expected names.
    """
    from agent.fb_common import API, _throttled_get
    from services.fb_token_provider import get_fb_token

    if type(require_names) is not bool:
        raise TypeError("require_names должен быть bool")
    unique_ids = list(dict.fromkeys(str(ad_id or "").strip() for ad_id in ad_ids))
    if not unique_ids or any(not ad_id for ad_id in unique_ids):
        return {}, "invalid_candidate_ids"
    if len(unique_ids) != len(ad_ids):
        return {}, "duplicate_candidate_ids"

    contexts: dict[str, ExactAdContext] = {}
    for offset in range(0, len(unique_ids), _BATCH_SIZE):
        chunk = unique_ids[offset : offset + _BATCH_SIZE]
        try:
            response = _throttled_get(
                API,
                params={
                    "access_token": get_fb_token(),
                    "ids": ",".join(chunk),
                    "fields": "id,name,adset_id,account_id,status,effective_status",
                },
            )
            payload = _response_json(response)
        except Exception as exc:
            logger.warning(
                "adset_pause_guard: exact lookup error %s",
                type(exc).__name__,
            )
            return contexts, _exception_reason("exact_candidates_error", exc)

        for requested_id in chunk:
            raw = payload.get(requested_id)
            if not isinstance(raw, dict):
                return contexts, "missing_candidate"
            ad_id = str(raw.get("id") or "")
            adset_id = str(raw.get("adset_id") or "")
            configured_status = str(raw.get("status") or "")
            effective_status = str(raw.get("effective_status") or "")
            name = str(raw.get("name") or "").strip()
            if ad_id != requested_id or not adset_id or not configured_status or not effective_status:
                return contexts, "incomplete_candidate"
            if require_names and not name:
                return contexts, "candidate_name_missing"
            contexts[requested_id] = {
                "ad_id": ad_id,
                "adset_id": adset_id,
                "configured_status": configured_status,
                "effective_status": effective_status,
            }
            if name:
                contexts[requested_id]["name"] = name
            # Кабинет объявления — для producer gateway (_account_id):
            # без него PAUSE-предложения по cabinet_b создавались с дефолтным
            # кабинетом (cabinet_a), и live-review исполнения вечно падал на
            # FB_ADSET_ACCOUNT_NOT_REQUESTED (шторм ретраев).
            # В канонический live-контекст (_LIVE_CONTEXT_FIELDS) поле не
            # входит — подписи инвентаря не меняются, как и с ``name``.
            account_id = str(raw.get("account_id") or "").removeprefix("act_")
            if account_id:
                contexts[requested_id]["account_id"] = account_id

    return contexts, None


def _get_exact_candidates(ad_ids: list[str]) -> tuple[dict[str, AdLiveContext], str | None]:
    """Обратносуместимый wrapper для внутреннего pause inventory."""
    contexts, error = fetch_exact_ad_contexts(ad_ids)
    return contexts, error


def inventory_state_sha256(
    rows: Iterable[Mapping[str, object]],
    *,
    expected_adset_id: str | None = None,
) -> str:
    """Возвращает canonical digest полного inventory без transport metadata.

    Строки должны содержать только exact provider-поля. Порядок строк не
    влияет на результат; дубли и смешивание разных adset блокируются.
    """

    required_fields = {
        "ad_id",
        "adset_id",
        "configured_status",
        "effective_status",
    }
    if expected_adset_id is not None and (
        not isinstance(expected_adset_id, str) or not expected_adset_id
    ):
        raise ValueError("expected_adset_id должен быть непустой строкой")

    normalized: list[AdLiveContext] = []
    seen_ad_ids: set[str] = set()
    observed_adset_id = expected_adset_id
    for raw_row in rows:
        if not isinstance(raw_row, Mapping) or set(raw_row) != required_fields:
            raise ValueError("inventory row должен содержать exact provider fields")
        values = {field: raw_row[field] for field in required_fields}
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ValueError("inventory row содержит пустое или нестроковое поле")
        typed_values = cast(dict[str, str], values)

        ad_id = typed_values["ad_id"]
        adset_id = typed_values["adset_id"]
        if ad_id in seen_ad_ids:
            raise ValueError("inventory содержит duplicate ad_id")
        if observed_adset_id is None:
            observed_adset_id = adset_id
        elif adset_id != observed_adset_id:
            raise ValueError("inventory row относится к другому adset")
        seen_ad_ids.add(ad_id)
        normalized.append(
            {
                "ad_id": ad_id,
                "adset_id": adset_id,
                "configured_status": typed_values["configured_status"],
                "effective_status": typed_values["effective_status"],
            }
        )

    ordered = tuple(sorted(normalized, key=lambda row: row["ad_id"]))
    return hashlib.sha256(canonical_json(ordered)).hexdigest()


def unrelated_inventory_baseline(
    inventory: Mapping[str, object],
    *,
    target_ad_id: str,
    expected_adset_id: str,
) -> tuple[str, tuple[AdStatusSnapshot, ...]]:
    """Строит immutable baseline всех объявлений adset кроме target."""

    raw_context = inventory.get("inventory_context")
    if not isinstance(raw_context, Mapping):
        raise ValueError("inventory_context отсутствует")
    rows: list[Mapping[str, object]] = []
    for ad_id, raw_row in raw_context.items():
        if ad_id == target_ad_id:
            continue
        if not isinstance(raw_row, Mapping):
            raise ValueError("inventory_context содержит не object")
        rows.append(raw_row)
    digest = inventory_state_sha256(rows, expected_adset_id=expected_adset_id)
    snapshot = tuple(
        AdStatusSnapshot(
            ad_id=str(row["ad_id"]),
            adset_id=str(row["adset_id"]),
            configured_status=str(row["configured_status"]),
            effective_status=str(row["effective_status"]),
        )
        for row in sorted(rows, key=lambda item: str(item["ad_id"]))
    )
    if hashlib.sha256(canonical_json(snapshot)).hexdigest() != digest:
        raise ValueError("typed sibling snapshot не совпадает с inventory digest")
    return digest, snapshot


def _inventory_sha256(inventory: AdsetInventory) -> str:
    """Хеширует только exact provider state, без времени и транспорта."""

    return inventory_state_sha256(
        inventory["inventory_context"].values(),
        expected_adset_id=inventory["adset_id"],
    )


def _fetch_adset_inventory(
    adset_id: str,
    candidate_context: dict[str, AdLiveContext],
) -> AdsetInventory:
    """Читает все страницы полного live inventory одного adset."""
    from agent.fb_common import API, _throttled_get
    from services.fb_token_provider import get_fb_token

    inventory: AdsetInventory = {
        "adset_id": adset_id,
        "active_ids": set(),
        "candidate_context": candidate_context,
        "inventory_context": {},
        "state_sha256": "",
        "complete": False,
        "pages_read": 0,
        "error": None,
    }
    after: str | None = None
    seen_cursors: set[str] = set()

    for page_number in range(1, _MAX_INVENTORY_PAGES + 1):
        try:
            params = {
                "access_token": get_fb_token(),
                "fields": "id,adset_id,status,effective_status",
                "limit": 100,
            }
            if after is not None:
                params["after"] = after
            response = _throttled_get(f"{API}/{adset_id}/ads", params=params)
            payload = _response_json(response)
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise RuntimeError("FB inventory не содержит data list")
        except Exception as exc:
            inventory["pages_read"] = page_number
            logger.warning(
                "adset_pause_guard: inventory error %s",
                type(exc).__name__,
            )
            inventory["error"] = _exception_reason("inventory_error", exc)
            inventory["active_ids"] = set()
            return inventory

        inventory["pages_read"] = page_number
        for row in rows:
            if not isinstance(row, dict):
                inventory["error"] = "inventory_row_not_object"
                inventory["active_ids"] = set()
                return inventory
            row_id = str(row.get("id") or "")
            row_adset_id = str(row.get("adset_id") or "")
            row_status = str(row.get("status") or "")
            row_effective_status = str(row.get("effective_status") or "")
            if not row_id or row_adset_id != adset_id or not row_status or not row_effective_status:
                inventory["error"] = "incomplete_inventory_row"
                inventory["active_ids"] = set()
                return inventory
            if row_id in inventory["inventory_context"]:
                inventory["error"] = "duplicate_inventory_row"
                inventory["active_ids"] = set()
                return inventory
            inventory["inventory_context"][row_id] = {
                "ad_id": row_id,
                "adset_id": row_adset_id,
                "configured_status": row_status,
                "effective_status": row_effective_status,
            }
            if row_effective_status == "ACTIVE":
                inventory["active_ids"].add(row_id)

        paging = payload.get("paging")
        if paging is None:
            inventory["complete"] = True
            inventory["state_sha256"] = _inventory_sha256(inventory)
            return inventory
        if not isinstance(paging, dict):
            inventory["error"] = "invalid_paging"
            inventory["active_ids"] = set()
            return inventory
        if not paging.get("next"):
            inventory["complete"] = True
            inventory["state_sha256"] = _inventory_sha256(inventory)
            return inventory

        cursors = paging.get("cursors") or {}
        next_after = cursors.get("after") if isinstance(cursors, dict) else None
        if not next_after:
            inventory["error"] = "paging_cursor_missing"
            inventory["active_ids"] = set()
            return inventory
        next_after = str(next_after)
        if next_after in seen_cursors:
            inventory["error"] = "paging_cursor_repeated"
            inventory["active_ids"] = set()
            return inventory
        if page_number == _MAX_INVENTORY_PAGES:
            inventory["error"] = "inventory_page_limit_exceeded"
            inventory["active_ids"] = set()
            return inventory
        seen_cursors.add(next_after)
        after = next_after

    inventory["error"] = "inventory_page_limit_exceeded"
    inventory["active_ids"] = set()
    return inventory


def fetch_pause_inventory(ad_ids: list[str]) -> dict[str, AdsetInventory]:
    """Возвращает полные live inventory adset для кандидатов PAUSE.

    Любая ошибка exact lookup помечает все уже найденные adset как incomplete.
    Кандидат без найденного контекста будет заблокирован планировщиком.
    """
    unique_ids = list(dict.fromkeys(str(ad_id) for ad_id in ad_ids if str(ad_id)))
    if not unique_ids:
        return {}

    contexts, exact_error = _get_exact_candidates(unique_ids)
    by_adset: dict[str, dict[str, AdLiveContext]] = {}
    for ad_id, context in contexts.items():
        by_adset.setdefault(context["adset_id"], {})[ad_id] = context

    if exact_error:
        return {
            adset_id: {
                "adset_id": adset_id,
                "active_ids": set(),
                "candidate_context": candidate_context,
                "inventory_context": {},
                "state_sha256": "",
                "complete": False,
                "pages_read": 0,
                "error": exact_error,
            }
            for adset_id, candidate_context in by_adset.items()
        }

    return {
        adset_id: _fetch_adset_inventory(adset_id, candidate_context)
        for adset_id, candidate_context in by_adset.items()
    }


def plan_safe_pauses(
    candidates: list[dict],
    inventories: dict[str, AdsetInventory],
    *,
    id_key: str,
) -> tuple[list[dict], list[dict]]:
    """Сохраняет приоритет и разрешает максимум N-1 пауз из N ACTIVE."""
    context_by_id: dict[str, tuple[str, AdLiveContext]] = {}
    remaining_by_adset: dict[str, set[str]] = {}
    for adset_id, inventory in inventories.items():
        remaining_by_adset[adset_id] = set(inventory.get("active_ids") or set())
        for ad_id, context in (inventory.get("candidate_context") or {}).items():
            context_by_id[ad_id] = (adset_id, context)

    allowed: list[dict] = []
    blocked: list[dict] = []
    for candidate in candidates:
        ad_id = str(candidate.get(id_key) or "")
        located = context_by_id.get(ad_id)
        reason: str | None = None
        if not ad_id:
            reason = "missing_candidate_id"
        elif located is None:
            reason = "missing_candidate_context"
        else:
            adset_id, context = located
            inventory = inventories.get(adset_id)
            if not inventory or not inventory.get("complete"):
                reason = (inventory or {}).get("error") or "inventory_incomplete"
            elif context.get("adset_id") != adset_id:
                reason = "candidate_adset_mismatch"
            elif context.get("effective_status") != "ACTIVE":
                reason = "candidate_not_active"
            elif ad_id not in remaining_by_adset.get(adset_id, set()):
                reason = "candidate_missing_from_active_inventory"
            else:
                other_active_ids = remaining_by_adset[adset_id] - {ad_id}
                if not other_active_ids:
                    reason = "last_active_without_replacement"
                else:
                    allowed.append(candidate)
                    remaining_by_adset[adset_id].discard(ad_id)

        if reason is not None:
            blocked_candidate = dict(candidate)
            blocked_candidate["pause_guard_reason"] = reason
            blocked.append(blocked_candidate)

    return allowed, blocked


@contextmanager
def adset_mutation_lock(adset_id: str) -> Iterator[None]:
    """Reentrant lock мутаций одного adset для потока и всех процессов."""
    if not _NUMERIC_ID_RE.fullmatch(adset_id):
        raise ValueError("adset_id должен состоять только из цифр")

    depths = getattr(_LOCK_STATE, "depths", None)
    if depths is None:
        depths = {}
        _LOCK_STATE.depths = depths
    depth = depths.get(adset_id, 0)
    if depth:
        # Вложенный вход того же потока уже защищён внешним flock. Повторный
        # flock на новом file descriptor может заблокировать сам себя.
        depths[adset_id] = depth + 1
        try:
            yield
        finally:
            nested_depth = depths[adset_id] - 1
            if nested_depth:
                depths[adset_id] = nested_depth
            else:
                depths.pop(adset_id, None)
        return

    _LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _LOCKS_DIR / f"adset-{adset_id}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        depths[adset_id] = 1
        try:
            yield
        finally:
            depths.pop(adset_id, None)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def adset_mutation_locks(adset_ids: tuple[str, ...]) -> Iterator[None]:
    """Берёт unique реальные mutation locks в строгом numeric порядке."""

    if not adset_ids or len(adset_ids) != len(set(adset_ids)):
        raise ValueError("adset_ids должны быть непустыми и уникальными")
    if any(_NUMERIC_ID_RE.fullmatch(adset_id) is None for adset_id in adset_ids):
        raise ValueError("adset_ids должны состоять только из цифр")
    ordered = tuple(sorted(adset_ids, key=int))
    held_ids = tuple(
        adset_id
        for adset_id, depth in getattr(_LOCK_STATE, "depths", {}).items()
        if depth
    )
    if held_ids and any(adset_id not in ordered for adset_id in held_ids):
        raise PauseGuardError("nested_adset_lock_order_unknown")
    with ExitStack() as stack:
        for adset_id in ordered:
            stack.enter_context(adset_mutation_lock(adset_id))
        yield


def _tombstone_path(adset_id: str) -> Path:
    return _LOCKS_DIR / f"adset-{adset_id}.paused.json"


def _new_tombstone(
    state: Literal["RESERVED", "PAUSE_CONFIRMED", "AMBIGUOUS"],
    now: datetime,
) -> PauseTombstone:
    now_iso = now.isoformat()
    return {"state": state, "reserved_at": now_iso, "updated_at": now_iso}


def _load_pause_tombstones(
    adset_id: str,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, PauseTombstone], bool]:
    """Читает state и безопасно мигрирует временную схему ``paused_ids``.

    Возвращает ``(entries, needs_write)``. Некорректный файл блокирует PAUSE.
    """
    path = _tombstone_path(adset_id)
    if not path.exists():
        return {}, False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PauseGuardError("pause_tombstones_read_error") from exc
    if not isinstance(payload, dict):
        raise PauseGuardError("pause_tombstones_invalid")

    # Backward-compatible чтение схемы первого safety patch. Миграция получает
    # полный TTL с момента обнаружения: раннее удаление legacy reservation опасно.
    if "paused_ids" in payload:
        paused_ids = payload.get("paused_ids")
        if not isinstance(paused_ids, list) or any(
            not isinstance(ad_id, str) or not ad_id for ad_id in paused_ids
        ):
            raise PauseGuardError("pause_tombstones_invalid")
        migration_now = now or _utc_now()
        return {
            ad_id: _new_tombstone("AMBIGUOUS", migration_now)
            for ad_id in paused_ids
        }, True

    if payload.get("schema_version") != _TOMBSTONE_SCHEMA_VERSION:
        raise PauseGuardError("pause_tombstones_schema_unsupported")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, dict):
        raise PauseGuardError("pause_tombstones_invalid")

    entries: dict[str, PauseTombstone] = {}
    valid_states = {"RESERVED", "PAUSE_CONFIRMED", "AMBIGUOUS"}
    for ad_id, raw_entry in raw_entries.items():
        if not isinstance(ad_id, str) or not ad_id or not isinstance(raw_entry, dict):
            raise PauseGuardError("pause_tombstones_invalid")
        state = raw_entry.get("state")
        reserved_at = raw_entry.get("reserved_at")
        updated_at = raw_entry.get("updated_at")
        if (
            state not in valid_states
            or not isinstance(reserved_at, str)
            or not isinstance(updated_at, str)
        ):
            raise PauseGuardError("pause_tombstones_invalid")
        try:
            _parse_tombstone_time(reserved_at)
            _parse_tombstone_time(updated_at)
        except (TypeError, ValueError) as exc:
            raise PauseGuardError("pause_tombstones_invalid") from exc
        entries[ad_id] = {
            "state": state,
            "reserved_at": reserved_at,
            "updated_at": updated_at,
        }
    return entries, False


def _save_pause_tombstones(
    adset_id: str,
    entries: dict[str, PauseTombstone],
) -> None:
    """Атомарно сохраняет tombstone до FB POST; ошибка блокирует мутацию."""
    path = _tombstone_path(adset_id)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(
            {
                "schema_version": _TOMBSTONE_SCHEMA_VERSION,
                "entries": {ad_id: entries[ad_id] for ad_id in sorted(entries)},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _parse_tombstone_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _reconcile_pause_tombstones(
    adset_id: str,
    entries: dict[str, PauseTombstone],
    inventory: AdsetInventory,
    *,
    now: datetime,
) -> tuple[dict[str, PauseTombstone], bool]:
    """Сверяет tombstones с exact FB status, не доверяя краткому stale ACTIVE.

    * non-ACTIVE означает, что FB уже сошёлся — локальная защита больше не нужна;
    * ACTIVE или отсутствующий exact context сохраняют reservation весь TTL;
    * после TTL reservation удаляется: устойчивый ACTIVE уже считается
      реактивацией/неуспешной PAUSE, а missing context не блокирует adset навсегда;
    * прогон, в котором exact context был неполным, всё равно fail-closed:
      вызывающая сторона блокирует текущую PAUSE и только очищает stale state.
    """
    reconciled = dict(entries)
    changed = False
    contexts = inventory.get("candidate_context") or {}
    for tombstone_ad_id, entry in entries.items():
        age = now - _parse_tombstone_time(entry["updated_at"])
        context = contexts.get(tombstone_ad_id)
        if (
            context is not None
            and context.get("adset_id") == adset_id
            and context.get("effective_status") != "ACTIVE"
        ):
            reconciled.pop(tombstone_ad_id, None)
            changed = True
            continue
        if age >= _TOMBSTONE_TTL:
            reconciled.pop(tombstone_ad_id, None)
            changed = True
    return reconciled, changed


def find_candidate_context(
    ad_id: str,
    inventories: dict[str, AdsetInventory],
) -> tuple[str, AdLiveContext] | None:
    """Находит exact candidate context только в одном inventory bucket."""
    located: tuple[str, AdLiveContext] | None = None
    for adset_id, inventory in inventories.items():
        context = (inventory.get("candidate_context") or {}).get(ad_id)
        if context is not None:
            if located is not None:
                return None
            located = (adset_id, context)
    return located


def _find_candidate_context(
    ad_id: str,
    inventories: dict[str, AdsetInventory],
) -> tuple[str, AdLiveContext] | None:
    """Обратносуместимый alias для существующего guard-кода."""
    return find_candidate_context(ad_id, inventories)


def _assert_action_adset_lock_held(adset_id: str) -> None:
    """Проверяет внешний neutral lock, не захватывая его повторно."""

    from services import action_locks

    expected_key = f"adset:{adset_id}"
    if not any(key == expected_key for _rank, key in action_locks._stack()):
        raise PauseGuardError("adset_lock_required")


def _validate_pause_locked(manifest: PauseManifest) -> LockedPauseValidation:
    """Повторно доказывает безопасную PAUSE под одним внешним adset lock.

    Helper только читает Facebook и возвращает typed результат. Он не берёт
    lock, не вызывает provider mutation и не отправляет Telegram. Будущий
    sealed adapter обязан вызвать его внутри ``action_locks.adset_locks``.
    """

    if not isinstance(manifest, PauseManifest):
        raise TypeError("manifest должен быть PauseManifest")
    _assert_action_adset_lock_held(manifest.adset_id)

    if (
        manifest.expected_before_status != "ACTIVE"
        or manifest.expected_after_status != "PAUSED"
    ):
        return _locked_denial(
            manifest,
            "INVALID_CONTRACT",
            "PAUSE допускает только переход ACTIVE -> PAUSED",
        )

    exact_ids = [manifest.ad_id]
    if manifest.replacement_ad_id is not None:
        exact_ids.append(manifest.replacement_ad_id)
    try:
        inventories = fetch_pause_inventory(exact_ids)
    except Exception as exc:
        logger.warning(
            "adset_pause_guard: locked inventory error %s",
            type(exc).__name__,
        )
        return _locked_denial(
            manifest,
            "INVENTORY_CHANGED",
            "Live inventory недоступен или неполон",
        )

    inventory = inventories.get(manifest.adset_id)
    if inventory is None or not inventory.get("complete"):
        return _locked_denial(
            manifest,
            "INVENTORY_CHANGED",
            "Live inventory недоступен или неполон",
        )

    inventory_sha256 = str(inventory.get("state_sha256") or "") or None
    exact_context = (inventory.get("candidate_context") or {}).get(manifest.ad_id)
    full_context = (inventory.get("inventory_context") or {}).get(manifest.ad_id)
    if (
        exact_context is None
        or full_context is None
        or _canonical_live_context(exact_context) != _canonical_live_context(full_context)
        or exact_context.get("adset_id") != manifest.adset_id
        or exact_context.get("configured_status") != "ACTIVE"
        or exact_context.get("effective_status") != "ACTIVE"
        or manifest.ad_id not in (inventory.get("active_ids") or set())
    ):
        return _locked_denial(
            manifest,
            "PAUSE_TARGET_CHANGED",
            "Target больше не является exact ACTIVE объявлением",
            inventory_sha256=inventory_sha256,
        )

    active_other_ids = tuple(
        sorted((inventory.get("active_ids") or set()) - {manifest.ad_id})
    )
    if set(active_other_ids) != set(manifest.sibling_active_ids):
        return _locked_denial(
            manifest,
            "INVENTORY_CHANGED",
            "Состав ACTIVE siblings изменился",
            active_other_ids=active_other_ids,
            inventory_sha256=inventory_sha256,
        )

    unrelated_sha256, sibling_snapshot = unrelated_inventory_baseline(
        inventory,
        target_ad_id=manifest.ad_id,
        expected_adset_id=manifest.adset_id,
    )
    if (
        unrelated_sha256 != manifest.pre_unrelated_inventory_sha256
        or sibling_snapshot != manifest.sibling_status_snapshot
    ):
        return _locked_denial(
            manifest,
            "INVENTORY_CHANGED",
            "Immutable sibling baseline изменился",
            active_other_ids=active_other_ids,
            inventory_sha256=inventory_sha256,
        )

    if manifest.replacement_ad_id is not None:
        replacement_exact = (inventory.get("candidate_context") or {}).get(
            manifest.replacement_ad_id
        )
        replacement_full = (inventory.get("inventory_context") or {}).get(
            manifest.replacement_ad_id
        )
        if (
            replacement_exact is None
            or replacement_full is None
            or replacement_exact != replacement_full
            or replacement_exact.get("adset_id") != manifest.adset_id
            or replacement_exact.get("configured_status") != "ACTIVE"
            or replacement_exact.get("effective_status") != "ACTIVE"
            or manifest.replacement_ad_id not in active_other_ids
        ):
            return _locked_denial(
                manifest,
                "INVENTORY_CHANGED",
                "Exact replacement больше не подтверждён как ACTIVE",
                active_other_ids=active_other_ids,
                inventory_sha256=inventory_sha256,
            )

    if not active_other_ids:
        return _locked_denial(
            manifest,
            "LAST_EFFECTIVE_ACTIVE",
            "После PAUSE не останется effective ACTIVE объявления",
            inventory_sha256=inventory_sha256,
        )

    return LockedPauseValidation(
        action_result=None,
        check_reason=None,
        ad_id=manifest.ad_id,
        adset_id=manifest.adset_id,
        active_other_ids=active_other_ids,
        inventory_sha256=inventory_sha256,
    )


def safe_pause_ad(ad_id: str, source: str) -> PauseOutcome:
    """Выполняет только live read/guard; provider mutation здесь запрещена."""
    del source
    ad_id = str(ad_id or "")
    if not ad_id:
        outcome = PauseOutcome(False, "missing_candidate_id", ad_id, None)
        return outcome

    try:
        preliminary = fetch_pause_inventory([ad_id])
    except Exception as exc:
        logger.warning(
            "adset_pause_guard: preliminary inventory error %s",
            type(exc).__name__,
        )
        outcome = PauseOutcome(
            False,
            _exception_reason("inventory_error", exc),
            ad_id,
            None,
        )
        return outcome
    located = _find_candidate_context(ad_id, preliminary)
    if located is None:
        outcome = PauseOutcome(False, "missing_candidate_context", ad_id, None)
        return outcome

    adset_id, _ = located
    allowed, blocked = plan_safe_pauses(
        [{"ad_id": ad_id}],
        preliminary,
        id_key="ad_id",
    )
    if not allowed:
        reason = (
            blocked[0].get("pause_guard_reason", "pause_blocked")
            if blocked
            else "pause_blocked"
        )
        return PauseOutcome(False, str(reason), ad_id, adset_id)
    active_other_ids = tuple(
        sorted(preliminary[adset_id]["active_ids"] - {ad_id})
    )
    return PauseOutcome(
        False,
        "owner_approval_required",
        ad_id,
        adset_id,
        active_other_ids,
    )

    """
    try:
        with adset_mutation_lock(adset_id):
            now = _utc_now()
            pause_tombstones, needs_write = _load_pause_tombstones(
                adset_id, now=now
            )
            inventory_ids = [ad_id, *sorted(pause_tombstones)]
            inventories = fetch_pause_inventory(inventory_ids)
            locked_inventory = inventories.get(adset_id)
            if locked_inventory is not None:
                locked_contexts = locked_inventory.get("candidate_context") or {}
                unresolved_tombstones = [
                    tombstone_ad_id
                    for tombstone_ad_id in pause_tombstones
                    if (
                        tombstone_ad_id not in locked_contexts
                        or locked_contexts[tombstone_ad_id].get("adset_id") != adset_id
                    )
                ]
                if unresolved_tombstones:
                    locked_inventory["complete"] = False
                    locked_inventory["error"] = "tombstone_context_incomplete"
                pause_tombstones, reconciled = _reconcile_pause_tombstones(
                    adset_id,
                    pause_tombstones,
                    locked_inventory,
                    now=now,
                )
                if needs_write or reconciled:
                    _save_pause_tombstones(adset_id, pause_tombstones)
                # FB может короткое время возвращать ACTIVE сразу после PAUSE.
                # Локальный tombstone под тем же flock исключает такую рекламу
                # из доказанных замен и работает между процессами.
                locked_inventory["active_ids"] = (
                    set(locked_inventory.get("active_ids") or set())
                    - set(pause_tombstones)
                )
            allowed, blocked = plan_safe_pauses(
                [{"ad_id": ad_id}], inventories, id_key="ad_id"
            )
            if not allowed:
                reason = blocked[0].get("pause_guard_reason", "pause_blocked") if blocked else "pause_blocked"
                outcome = PauseOutcome(False, str(reason), ad_id, adset_id)
                return outcome

            inventory = inventories[adset_id]
            active_other_ids = tuple(sorted(inventory["active_ids"] - {ad_id}))

            legacy mutation boundary removed

            # Резервируем PAUSE до внешней мутации. Если процесс упадёт после
            # POST, следующий worker всё равно не примет stale ACTIVE за замену.
            pause_tombstones[ad_id] = _new_tombstone("RESERVED", now)
            _save_pause_tombstones(adset_id, pause_tombstones)

            try:
                pause_succeeded = False
            except Exception as exc:
                ambiguous_now = _utc_now()
                reservation = pause_tombstones[ad_id]
                pause_tombstones[ad_id] = {
                    **reservation,
                    "state": "AMBIGUOUS",
                    "updated_at": ambiguous_now.isoformat(),
                }
                _save_pause_tombstones(adset_id, pause_tombstones)
                logger.error(
                    "adset_pause_guard: pause request error %s",
                    type(exc).__name__,
                )
                return PauseOutcome(
                    False,
                    _exception_reason("pause_request_error", exc),
                    ad_id,
                    adset_id,
                    active_other_ids,
                )

            if not pause_succeeded:
                pause_tombstones.pop(ad_id, None)
                _save_pause_tombstones(adset_id, pause_tombstones)
                return PauseOutcome(
                    False,
                    "pause_ad_returned_false",
                    ad_id,
                    adset_id,
                    active_other_ids,
                )

            confirmed_now = _utc_now()
            reservation = pause_tombstones[ad_id]
            pause_tombstones[ad_id] = {
                **reservation,
                "state": "PAUSE_CONFIRMED",
                "updated_at": confirmed_now.isoformat(),
            }
            _save_pause_tombstones(adset_id, pause_tombstones)
            return PauseOutcome(True, "paused", ad_id, adset_id, active_other_ids)
    except Exception as exc:
        logger.error(
            "adset_pause_guard: guard error for %s (%s)",
            _redact_sensitive(ad_id),
            type(exc).__name__,
        )
        outcome = PauseOutcome(
            False,
            _exception_reason("guard_error", exc),
            ad_id,
            adset_id,
        )
        return outcome
    """
