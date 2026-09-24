"""Read-only аудит пропущенных запусков после Trello completion.

Внешние системы в этом модуле используются только для чтения. Durable case и
планы городов пишутся в локальную SQLite; Facebook CREATE/DELETE/PAUSE и Trello
mutation намеренно не импортируются и не вызываются.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence


AccountKind = Literal["offline", "online"]
RecoveryCasePhase = Literal[
    "DISCOVERED",
    "NO_ACTION",
    "REVIEW_REQUIRED",
    "WAITING_SLOT",
    "APPROVED",
    "LAUNCHING",
    "WAITING_ACTIVE",
    "RECOVERED",
    "BLOCKED",
    "CANCELLED",
]
RecoveryCityPhase = Literal[
    "DISCOVERED",
    "COMPLETE",
    "MISSING",
    "REVIEW_REQUIRED",
    "WAITING_SLOT",
    "APPROVED",
    "LAUNCHING",
    "WAITING_ACTIVE",
    "RECOVERED",
    "BLOCKED",
    "CANCELLED",
]

_TZ_LOCAL = timezone(timedelta(hours=5))
RECOVERY_CUTOFF = datetime.fromisoformat("2026-07-01T00:00:00+05:00")
RECOVERY_CASE_TERMINAL_PHASES: frozenset[RecoveryCasePhase] = frozenset(
    {"NO_ACTION", "RECOVERED", "CANCELLED"}
)
_STANDARD_CITIES = ("CityA", "CityB", "CityC", "CityD", "CityE",
                    "CityF")
_ONLINE_CAMPAIGN_TYPES = frozenset({"mql_online", "prodb_online"})
_CAMPAIGN_LABELS = {"PRODA": "leadgen", "PRODB": "leadgen_prodb"}
_CAMPAIGN_TYPES = frozenset(
    {"leadgen", "leadgen_prodb", "website", "mql_online", "prodb_online"}
)
_HARD_RESERVE_SLOTS = 1
_ID_NAMESPACE = uuid.UUID("b697dce8-b6a9-4cc7-98b1-a2cb02bdcd4c")


class RecoveryAuditError(RuntimeError):
    """Fail-closed ошибка recovery audit без разрешения на CREATE."""


class RecoveryReviewRequired(RecoveryAuditError):
    """Данных достаточно только для ручной проверки."""


@dataclass(frozen=True, slots=True)
class TrelloCompletion:
    action_id: str
    board_id: str
    card_id: str
    card_name: str
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class MediaFileEvidence:
    relative_name: str
    size: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class MediaManifest:
    manifest_sha256: str
    files: tuple[MediaFileEvidence, ...]
    expected_names_by_city: dict[str, tuple[str, ...]]
    # SHA в точности повторяет provider-bound canonical media payload. Старые
    # durable планы не имеют этого поля в evidence и поэтому fail-closed.
    provider_media_sha256: str | None = None
    media_type: str | None = None


@dataclass(frozen=True, slots=True)
class LiveAdsetDiscovery:
    """Проверенный direct FB discovery одного account-контура (kind).

    ``account_id`` — дефолтный кабинет контура (cabinet_a для offline).
    ``accounts`` — кабинет КАЖДОЙ ПАРЫ (город, тип): {city: {type: account}}.
    С маршрутизацией (services/launch_routing.py) оффлайн-контур покрывает
    несколько кабинетов, и расщепление проходит внутри города
    (L2 в cabinet_b, L1/MQL в cabinet_a) — сверяться обязана пара, а не город.
    Пара без записи в ``accounts`` не может быть разрешена (fail-closed).
    """

    account_kind: AccountKind
    account_id: str
    leadgen: dict[str, dict[str, str]]
    mql: dict[str, str]
    accounts: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecoveryCityPlan:
    plan_id: str
    case_id: str
    city: str
    account_kind: AccountKind
    account_id: str
    adset_id: str
    expected_ad_names: tuple[str, ...]
    expected_ad_count: int
    reconcile_from: datetime
    reconcile_until: datetime
    phase: RecoveryCityPhase
    found_ad_ids: tuple[str, ...]
    media_manifest_sha256: str
    launch_attempt_key: str | None
    capacity_available: int | None
    evidence: dict[str, Any]
    approved_by: str | None
    approved_at: datetime | None
    last_rechecked_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @property
    def provider_media_sha256(self) -> str | None:
        """Канонический provider SHA, сохранённый новым recovery audit."""
        value = self.evidence.get("provider_media_sha256")
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            char not in "0123456789abcdef" for char in normalized
        ):
            return None
        return normalized


@dataclass(frozen=True, slots=True)
class LaunchRecoveryPlan:
    case_id: str
    card_id: str
    card_name: str
    source_completed_at: datetime
    phase: RecoveryCasePhase
    media_manifest_sha256: str
    city_plans: tuple[RecoveryCityPlan, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LaunchRecoverySummary:
    audit_run_id: str
    since: datetime
    audited_at: datetime
    discovered_cards: int
    no_action_cases: int
    review_required_cases: int
    missing_city_plans: int
    case_ids: tuple[str, ...]
    errors: tuple[str, ...]


def _aware_local(moment: datetime, field_name: str) -> datetime:
    if (
        not isinstance(moment, datetime)
        or moment.tzinfo is None
        or moment.utcoffset() is None
    ):
        raise ValueError(f"{field_name} должен быть timezone-aware datetime")
    return moment.astimezone(_TZ_LOCAL)


def _parse_aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RecoveryReviewRequired(f"{field_name}_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryReviewRequired(f"{field_name}_invalid") from exc
    return _aware_local(parsed, field_name)


def _case_id(action_id: str) -> str:
    return f"recovery-{uuid.uuid5(_ID_NAMESPACE, action_id).hex}"


def _plan_id(case_id: str, city: str) -> str:
    return f"recovery-city-{uuid.uuid5(_ID_NAMESPACE, f'{case_id}|{city}').hex}"


def _normalize_labels(card: Mapping[str, Any]) -> tuple[str, ...]:
    raw_labels = card.get("labels")
    if not isinstance(raw_labels, Sequence) or isinstance(raw_labels, (str, bytes)):
        return ()
    labels: list[str] = []
    for raw_label in raw_labels:
        if isinstance(raw_label, Mapping):
            label = str(raw_label.get("name") or raw_label.get("color") or "").strip()
        else:
            label = str(raw_label or "").strip()
        if label:
            labels.append(label)
    return tuple(labels)


def _campaign_type(card: Mapping[str, Any]) -> str:
    explicit = str(card.get("campaign_type") or "").strip()
    if explicit:
        if explicit not in _CAMPAIGN_TYPES:
            raise RecoveryReviewRequired("unknown_campaign_type")
        return explicit

    detected: list[str] = []
    for label in _normalize_labels(card):
        upper = label.upper()
        detected.extend(
            value for token, value in _CAMPAIGN_LABELS.items() if token in upper
        )
    unique = tuple(dict.fromkeys(detected))
    if len(unique) != 1:
        raise RecoveryReviewRequired("unknown_or_ambiguous_campaign_labels")
    return unique[0]


def _target_cities(card: Mapping[str, Any], campaign_type: str) -> tuple[str, ...]:
    raw_cities = card.get("cities")
    if isinstance(raw_cities, Sequence) and not isinstance(raw_cities, (str, bytes)):
        cities = tuple(
            dict.fromkeys(
                str(city or "").strip()
                for city in raw_cities
                if str(city or "").strip()
            )
        )
        if cities:
            return cities
    if campaign_type in _ONLINE_CAMPAIGN_TYPES:
        return ("Онлайн",)
    return _STANDARD_CITIES


def scan_completed_cards_since(
    since: datetime,
    *,
    page_limit: int = 1000,
) -> list[TrelloCompletion]:
    """Читает локально доказанные false->true transitions с inclusive cutoff."""
    normalized_since = _aware_local(since, "since")
    if normalized_since < RECOVERY_CUTOFF:
        raise ValueError("since не может быть раньше RECOVERY_CUTOFF")
    if type(page_limit) is not int or not 1 <= page_limit <= 1000:
        raise ValueError("page_limit должен быть от 1 до 1000")

    from config import TRELLO_BOARD_ID
    from integrations.trello import (
        get_board_update_card_actions,
        is_due_complete_transition,
    )

    actions = get_board_update_card_actions(
        TRELLO_BOARD_ID,
        normalized_since,
        page_size=page_limit,
    )
    if not isinstance(actions, list):
        raise RecoveryAuditError("trello_actions_incomplete_or_invalid")

    completions: list[TrelloCompletion] = []
    seen_ids: dict[str, Mapping[str, Any]] = {}
    for action in actions:
        if not isinstance(action, Mapping) or not is_due_complete_transition(
            dict(action)
        ):
            continue
        action_id = str(action.get("id") or "").strip()
        if not action_id:
            raise RecoveryAuditError("trello_action_without_id")
        previous = seen_ids.get(action_id)
        if previous is not None:
            if previous != action:
                raise RecoveryAuditError("trello_action_duplicate_conflict")
            continue
        seen_ids[action_id] = action

        completed_at = _parse_aware(action.get("date"), "trello_action_date")
        if completed_at < normalized_since:
            continue
        data = action.get("data")
        card = data.get("card") if isinstance(data, Mapping) else None
        board = data.get("board") if isinstance(data, Mapping) else None
        card_id = str(card.get("id") if isinstance(card, Mapping) else "").strip()
        if not card_id:
            raise RecoveryAuditError("trello_action_without_card_id")
        board_id = str(board.get("id") if isinstance(board, Mapping) else "").strip()
        if board_id and board_id != TRELLO_BOARD_ID:
            raise RecoveryAuditError("trello_action_board_mismatch")
        completions.append(
            TrelloCompletion(
                action_id=action_id,
                board_id=board_id or TRELLO_BOARD_ID,
                card_id=card_id,
                card_name=str(card.get("name") if isinstance(card, Mapping) else ""),
                completed_at=completed_at,
            )
        )
    return sorted(completions, key=lambda item: (item.completed_at, item.action_id))


def _download_media_for_card(card: Mapping[str, Any]) -> Mapping[str, Any]:
    embedded = card.get("media")
    if isinstance(embedded, Mapping):
        return embedded

    from integrations.gdrive import download_media
    from integrations.trello import get_card_drive_link

    card_id = str(card.get("id") or "").strip()
    drive_url = get_card_drive_link(card_id)
    if not drive_url:
        raise RecoveryReviewRequired("drive_link_missing")
    media = download_media(drive_url)
    if not isinstance(media, Mapping):
        raise RecoveryReviewRequired("media_payload_invalid")
    return media


def _media_files(media: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    media_type = str(media.get("type") or "")
    paths = media.get("paths")
    if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes)):
        raise RecoveryReviewRequired("media_paths_invalid")

    named_paths: list[tuple[str, str]] = []
    if media_type == "placement_pairs":
        normalized_pairs: list[tuple[str, Mapping[str, Any]]] = []
        for pair in paths:
            if not isinstance(pair, Mapping):
                raise RecoveryReviewRequired("placement_pair_invalid")
            label = str(pair.get("label") or "").strip()
            if not label:
                raise RecoveryReviewRequired("placement_pair_incomplete")
            normalized_pairs.append((label, pair))
        for label, pair in sorted(normalized_pairs, key=lambda item: item[0]):
            for placement in ("feed", "story"):
                path = str(pair.get(placement) or "").strip()
                if not path:
                    raise RecoveryReviewRequired("placement_pair_incomplete")
                named_paths.append((f"{label}.{placement}:{Path(path).name}", path))
        singles = media.get("singles", ())
        if not isinstance(singles, Sequence) or isinstance(singles, (str, bytes)):
            raise RecoveryReviewRequired("placement_singles_invalid")
        normalized_singles: list[tuple[str, str]] = []
        for single in singles:
            if not isinstance(single, Mapping):
                raise RecoveryReviewRequired("placement_single_invalid")
            path = str(single.get("path") or "").strip()
            label = str(single.get("label") or "").strip()
            if not path or not label:
                raise RecoveryReviewRequired("placement_single_incomplete")
            normalized_singles.append((label, path))
        for label, path in sorted(normalized_singles, key=lambda item: item[0]):
            named_paths.append((f"{label}.single:{Path(path).name}", path))
    elif media_type in {"video", "image", "carousel"}:
        ordered_paths = sorted(paths, key=lambda value: Path(str(value)).name)
        if media_type == "carousel":
            ordered_paths = ordered_paths[:10]
        for raw_path in ordered_paths:
            path = str(raw_path or "").strip()
            if not path:
                raise RecoveryReviewRequired("media_path_empty")
            named_paths.append((Path(path).name, path))
    else:
        raise RecoveryReviewRequired("media_type_unknown")

    relative_names = [name for name, _path in named_paths]
    if not relative_names or len(relative_names) != len(set(relative_names)):
        raise RecoveryReviewRequired("media_names_empty_or_duplicate")
    return tuple(named_paths)


def _expected_names_for_card(
    card: Mapping[str, Any],
    media: Mapping[str, Any],
) -> dict[str, tuple[str, ...]]:
    from integrations.facebook import _expected_city_ad_names
    from services.product_tags import classify_product

    campaign_type = _campaign_type(card)
    cities = _target_cities(card, campaign_type)
    card_name = str(card.get("name") or "").strip()
    if not card_name:
        raise RecoveryReviewRequired("card_name_missing")
    labels = list(_normalize_labels(card))
    product = classify_product(card_name, str(card.get("desc") or ""), labels)
    media_type = str(media.get("type") or "")

    paths = media.get("paths")
    assert isinstance(paths, Sequence) and not isinstance(paths, (str, bytes))
    video_assets: list[dict[str, str]] = []
    image_assets: list[dict[str, str]] = []
    placement_pairs: list[dict[str, str]] = []
    single_image_assets: list[dict[str, str]] = []
    if media_type == "video":
        video_assets = [
            {"label": Path(str(path)).stem}
            for path in sorted(paths, key=lambda value: Path(str(value)).name)
        ]
    elif media_type == "image":
        image_assets = [
            {"label": Path(str(path)).stem}
            for path in sorted(paths, key=lambda value: Path(str(value)).name)
        ]
    elif media_type == "placement_pairs":
        placement_pairs = sorted(
            ({"label": str(pair.get("label") or "").strip()} for pair in paths),
            key=lambda pair: pair["label"],
        )
        single_image_assets = sorted(
            (
                {"label": str(single.get("label") or "").strip()}
                for single in media.get("singles", ())
            ),
            key=lambda single: single["label"],
        )

    result: dict[str, tuple[str, ...]] = {}
    for city in cities:
        names = tuple(
            _expected_city_ad_names(
                city,
                card_name,
                media_type,
                video_assets,
                image_assets,
                placement_pairs,
                single_image_assets,
                product,
            )
        )
        if not names:
            raise RecoveryReviewRequired(f"expected_names_empty:{city}")
        result[city] = names
    return result


def _provider_media_sha256(
    media_type: str,
    files: Sequence[MediaFileEvidence],
) -> str:
    """Повторяет публичный provider SHA без абсолютных путей и сетевых вызовов."""
    if media_type not in {"video", "image", "carousel", "placement_pairs"}:
        raise RecoveryReviewRequired("media_type_unknown")
    payload = {
        "media_type": media_type,
        "files": [asdict(file) for file in files],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _build_media_manifest_from_media(
    card: Mapping[str, Any],
    media: Mapping[str, Any],
) -> MediaManifest:
    """Строит оба recovery/provider SHA из одного exact media snapshot."""
    if not isinstance(card, Mapping):
        raise TypeError("card должен быть Mapping")
    if not isinstance(media, Mapping):
        raise TypeError("media должен быть Mapping")
    named_paths = _media_files(media)
    files: list[MediaFileEvidence] = []
    for relative_name, raw_path in named_paths:
        path = Path(raw_path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise RecoveryReviewRequired(
                f"media_file_unreadable:{relative_name}"
            ) from exc
        if not content:
            raise RecoveryReviewRequired(f"media_file_empty:{relative_name}")
        files.append(
            MediaFileEvidence(
                relative_name=relative_name,
                size=len(content),
                content_sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    expected_names = _expected_names_for_card(card, media)
    payload = {
        "files": [asdict(file) for file in files],
        "expected_names_by_city": {
            city: list(names) for city, names in sorted(expected_names.items())
        },
    }
    manifest_sha256 = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    media_type = str(media.get("type") or "")
    return MediaManifest(
        manifest_sha256=manifest_sha256,
        files=tuple(files),
        expected_names_by_city=expected_names,
        provider_media_sha256=_provider_media_sha256(media_type, files),
        media_type=media_type,
    )


def build_media_manifest(card: Mapping[str, Any]) -> MediaManifest:
    """Хеширует exact media batch и ordered expected names без внешней mutation."""
    if not isinstance(card, Mapping):
        raise TypeError("card должен быть Mapping")
    media = _download_media_for_card(card)
    return _build_media_manifest_from_media(card, media)


def _resolve_account_id(account_kind: str) -> str:
    from services.fb_token_provider import fb_account, get_fb_account_id

    if account_kind not in {"offline", "online"}:
        raise RecoveryAuditError("account_kind_invalid")
    with fb_account("online" if account_kind == "online" else None):
        account_id = str(get_fb_account_id() or "").removeprefix("act_").strip()
    if not account_id:
        raise RecoveryAuditError(f"account_id_missing:{account_kind}")
    return account_id


def _fetch_account_inventory(
    account_kind: str, account_id: str
) -> list[dict[str, Any]]:
    from integrations import facebook

    fetcher = getattr(facebook, "fetch_complete_account_ad_inventory", None)
    if not callable(fetcher):
        raise RecoveryAuditError("complete_inventory_reader_unavailable")
    rows = fetcher(account_kind, account_id)
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise RecoveryAuditError(f"complete_inventory_invalid:{account_kind}")
    result: list[dict[str, Any]] = []
    for row in rows:
        normalized = dict(row)
        if (
            normalized.get("inventory_complete") is not True
            or normalized.get("account_kind") != account_kind
            or str(normalized.get("account_id") or "").removeprefix("act_")
            != account_id
        ):
            raise RecoveryAuditError(f"complete_inventory_scope_invalid:{account_kind}")
        result.append(normalized)
    return result


def _offline_inventory_accounts() -> tuple[str, ...]:
    """Все оффлайн-кабинеты запуска: дефолтный (cabinet_a) + карта роутинга.

    С маршрутизацией город→кабинет (services/launch_routing.py) оффлайн-контур
    покрывает несколько кабинетов; аудит обязан видеть КАЖДЫЙ из них, иначе
    существующий ad в «другом» кабинете (например CityF в cabinet_b)
    выглядел бы как MISSING и провоцировал бы дубль CREATE. Ошибка чтения
    карты = RecoveryAuditError, а не молчаливое сжатие до cabinet_a (fail-closed).
    """
    accounts: dict[str, None] = dict.fromkeys((_resolve_account_id("offline"),))
    try:
        from services.launch_routing import accounts_to_scan

        accounts.update(dict.fromkeys(accounts_to_scan()))
    except Exception as exc:  # noqa: BLE001 — неполный скан опаснее отказа
        raise RecoveryAuditError(f"launch_routing_unavailable:{type(exc).__name__}") from exc
    return tuple(accounts)


def fetch_complete_launch_inventory(
    account_kinds: Sequence[str],
) -> dict[str, list[dict[str, Any]]]:
    """Читает полный inventory каждого exact FB account scope.

    Для «offline» — конкатенация полных инвентарей ВСЕХ оффлайн-кабинетов
    карты роутинга; каждая строка несёт свой account_id, и классификация
    городов сверяет его с кабинетом плана города.
    """
    normalized = tuple(dict.fromkeys(str(kind or "").strip() for kind in account_kinds))
    if not normalized or any(kind not in {"offline", "online"} for kind in normalized):
        raise ValueError("account_kinds содержит неизвестный или пустой scope")
    result: dict[str, list[dict[str, Any]]] = {}
    for kind in normalized:
        if kind == "offline":
            rows: list[dict[str, Any]] = []
            for account_id in _offline_inventory_accounts():
                rows.extend(_fetch_account_inventory(kind, account_id))
            result[kind] = rows
        else:
            result[kind] = _fetch_account_inventory(kind, _resolve_account_id(kind))
    return result


def _normalize_live_adset_payload(
    account_kind: AccountKind,
    account_id: str,
    payload: object,
) -> LiveAdsetDiscovery:
    if not isinstance(payload, Mapping) or payload.get("source") != "fb_api":
        raise RecoveryReviewRequired(f"adset_discovery_not_live:{account_kind}")
    raw_leadgen = payload.get("leadgen")
    raw_mql = payload.get("mql")
    if not isinstance(raw_leadgen, Mapping) or not isinstance(raw_mql, Mapping):
        raise RecoveryReviewRequired(f"adset_discovery_invalid:{account_kind}")

    leadgen: dict[str, dict[str, str]] = {}
    mql: dict[str, str] = {}
    active_bindings: list[str] = []
    for raw_city, raw_types in raw_leadgen.items():
        city = str(raw_city or "").strip()
        if not city or not isinstance(raw_types, Mapping) or not raw_types:
            raise RecoveryReviewRequired(f"adset_discovery_ambiguous:{account_kind}")
        city_types: dict[str, str] = {}
        for raw_type, raw_adset_id in raw_types.items():
            adset_type = str(raw_type or "").strip()
            adset_id = str(raw_adset_id or "").strip()
            # PRODB — выделенные PRODB-адсеты cabinet_b (карта роутинга);
            # discovery кладёт их в leadgen[city]["PRODB"] рядом с L2/L1.
            if adset_type not in {"L2", "L1", "PRODB"} or not adset_id:
                raise RecoveryReviewRequired(
                    f"adset_discovery_ambiguous:{account_kind}"
                )
            city_types[adset_type] = adset_id
            active_bindings.append(adset_id)
        leadgen[city] = city_types
    for raw_city, raw_adset_id in raw_mql.items():
        city = str(raw_city or "").strip()
        adset_id = str(raw_adset_id or "").strip()
        if not city or not adset_id:
            raise RecoveryReviewRequired(f"adset_discovery_ambiguous:{account_kind}")
        mql[city] = adset_id
        active_bindings.append(adset_id)
    if not active_bindings:
        raise RecoveryReviewRequired(f"adset_discovery_empty:{account_kind}")
    if len(active_bindings) != len(set(active_bindings)):
        raise RecoveryReviewRequired(
            f"adset_discovery_duplicate_binding:{account_kind}"
        )
    # Кабинет каждой ПАРЫ (город, тип): мультикабинетный оффлайн-скан отдаёт его
    # в payload["accounts"] = {city: {type: account}}. Город живёт
    # в двух кабинетах сразу (L2 в cabinet_b, L1/MQL в cabinet_a), поэтому кабинет
    # проверяется по паре. Битая запись = REVIEW; отсутствие карты целиком =
    # весь инвентарь дефолтного кабинета контура (её честность дальше гарантирует
    # сверка с launch_routing в _resolve_city_scope).
    required_pairs = [
        (city, adset_type) for city, types in leadgen.items() for adset_type in types
    ] + [(city, "MQL") for city in mql]
    raw_accounts = payload.get("accounts")
    accounts: dict[str, dict[str, str]] = {}
    normalized_default = str(account_id).removeprefix("act_").strip()
    for city, adset_type in required_pairs:
        if isinstance(raw_accounts, Mapping):
            raw_city_types = raw_accounts.get(city)
            if not isinstance(raw_city_types, Mapping):
                raise RecoveryReviewRequired(
                    f"adset_discovery_account_missing:{account_kind}:{city}:{adset_type}"
                )
            pair_account = str(
                raw_city_types.get(adset_type) or ""
            ).removeprefix("act_").strip()
            if not pair_account:
                raise RecoveryReviewRequired(
                    f"adset_discovery_account_missing:{account_kind}:{city}:{adset_type}"
                )
        else:
            pair_account = normalized_default
        accounts.setdefault(city, {})[adset_type] = pair_account
    return LiveAdsetDiscovery(
        account_kind=account_kind,
        account_id=account_id,
        leadgen=leadgen,
        mql=mql,
        accounts=accounts,
    )


def discover_live_recovery_adsets(
    account_kinds: Sequence[str] = ("offline", "online"),
) -> dict[AccountKind, LiveAdsetDiscovery]:
    """Обходит cache/static fallback и принимает только direct FB discovery."""
    normalized = tuple(str(kind or "").strip() for kind in account_kinds)
    if (
        not normalized
        or len(normalized) != len(set(normalized))
        or any(kind not in {"offline", "online"} for kind in normalized)
    ):
        raise ValueError("account_kinds содержит duplicate/unknown scope")

    from agent.adset_discovery import discover_adsets
    from services.fb_token_provider import fb_account

    discovered: dict[AccountKind, LiveAdsetDiscovery] = {}
    for raw_kind in normalized:
        account_kind: AccountKind = raw_kind  # type: ignore[assignment]
        with fb_account("online" if account_kind == "online" else None):
            account_id = _resolve_account_id(account_kind)
            payload = discover_adsets(force_refresh=True)
        discovered[account_kind] = _normalize_live_adset_payload(
            account_kind,
            account_id,
            payload,
        )
    return discovered


def _resolve_city_scope(
    card: Mapping[str, Any],
    city: str,
    live_adsets: Mapping[AccountKind, LiveAdsetDiscovery] | None = None,
) -> tuple[AccountKind, str, str]:
    """Кабинет и адсет города. Для оффлайна кабинет берётся ПО ПАРЕ (город, тип).

    С маршрутизацией (services/launch_routing.py) оффлайн-инвентарь живёт в
    разных кабинетах, причём расщеплён внутри города: L2 расщеплённых
    городов в cabinet_b, их L1 и MQL в cabinet_a, CityF целиком в cabinet_b.
    Кабинет пары обязан совпасть в двух независимых источниках: живом discovery
    (accounts) и карте роутинга. Любое расхождение = REVIEW (fail-closed) —
    ретрай не имеет права уйти в чужой кабинет.
    """
    from integrations.trello import detect_language
    from services.auto_launch import _resolve_launch_account

    campaign_type = _campaign_type(card)
    account_kind, account_id = _resolve_launch_account(campaign_type)
    normalized_kind: AccountKind = account_kind
    live = live_adsets or discover_live_recovery_adsets(("offline", "online"))
    discovered = live.get(normalized_kind)
    normalized_account_id = str(account_id).removeprefix("act_").strip()
    if discovered is None or discovered.account_id != normalized_account_id:
        raise RecoveryReviewRequired("city_scope_account_mismatch")
    language = detect_language(str(card.get("name") or ""), str(card.get("desc") or ""))
    if campaign_type == "website":
        adset_id = str(discovered.mql.get(city) or "")
        adset_key = "MQL"
    else:
        # Ключ инвентаря = тип маршрута, а не язык карточки: у PRODB (leadgen_prodb)
        # оффлайн-инвентарь — выделенные PRODB-адсеты (тип PRODB), язык выбирает
        # только лид-форму. Тот же контракт, что в facebook._resolve_launch_adsets;
        # раньше recovery брал leadgen[city][language] и для PRODB-карточек
        # находил PRODA-адсет, после чего сверка с картой давала route_mismatch.
        adset_key = language
        if campaign_type == "leadgen_prodb" and normalized_kind != "online":
            adset_key = "PRODB"
        adset_id = str((discovered.leadgen.get(city) or {}).get(adset_key) or "")
    if not adset_id:
        raise RecoveryReviewRequired(
            f"city_scope_missing:{normalized_kind}:{city}:{adset_key}"
        )
    if normalized_kind == "online":
        # Онлайн-контур — один кабинет; города карты роутинга его не касаются.
        return normalized_kind, normalized_account_id, adset_id
    try:
        from services.launch_routing import route_type

        scope_type = route_type(campaign_type, language)
    except Exception as exc:  # noqa: BLE001 — неизвестный тип = REVIEW
        raise RecoveryReviewRequired(
            f"city_scope_route_type_unknown:{campaign_type}:{language}"
        ) from exc
    city_account = str(
        (discovered.accounts.get(city) or {}).get(scope_type) or ""
    ).removeprefix("act_").strip()
    if not city_account:
        raise RecoveryReviewRequired(f"city_scope_account_missing:{city}:{scope_type}")
    try:
        from services.launch_routing import LaunchRoutingError, resolve_account

        routed_account = resolve_account(city, scope_type)
    except LaunchRoutingError:
        raise RecoveryReviewRequired(
            f"city_scope_unrouted:{city}:{scope_type}"
        ) from None
    except Exception as exc:  # noqa: BLE001 — карта нечитаема = REVIEW, не cabinet_a
        raise RecoveryReviewRequired(
            f"city_scope_routing_unavailable:{type(exc).__name__}"
        ) from exc
    if routed_account != city_account:
        raise RecoveryReviewRequired(
            f"city_scope_route_mismatch:{city}:{city_account}!={routed_account}"
        )
    return normalized_kind, city_account, adset_id


def _normalized_expected_names(names: Sequence[str]) -> tuple[str, ...]:
    from services.product_tags import strip_product_tag

    normalized = tuple(strip_product_tag(str(name or "").strip()) for name in names)
    if not normalized or any(not name for name in normalized):
        raise RecoveryReviewRequired("expected_names_empty")
    if len(normalized) != len(set(normalized)):
        raise RecoveryReviewRequired("expected_names_duplicate_after_normalization")
    return normalized


def _ad_time(ad: Mapping[str, Any]) -> datetime | None:
    raw = ad.get("created_time", ad.get("created_at"))
    try:
        return _parse_aware(raw, "ad_created_time")
    except RecoveryReviewRequired:
        return None


def _classify_city_inventory(
    expected_names: Sequence[str],
    account_kind: AccountKind,
    account_id: str,
    adset_id: str,
    reconcile_from: datetime,
    reconcile_until: datetime,
    inventory: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[RecoveryCityPhase, tuple[str, ...], dict[str, Any]]:
    from services.product_tags import strip_product_tag

    normalized_names = _normalized_expected_names(expected_names)
    matches: dict[str, list[str]] = {name: [] for name in normalized_names}
    ambiguous: list[dict[str, Any]] = []
    evidence_matches: list[dict[str, Any]] = []
    for bucket_kind, rows in inventory.items():
        if bucket_kind not in {"offline", "online"}:
            raise RecoveryAuditError("inventory_account_kind_invalid")
        if not isinstance(rows, Sequence):
            raise RecoveryAuditError("inventory_bucket_invalid")
        for ad in rows:
            if not isinstance(ad, Mapping):
                raise RecoveryAuditError("inventory_ad_invalid")
            normalized_name = strip_product_tag(str(ad.get("name") or "").strip())
            if normalized_name not in matches:
                continue
            ad_id = str(ad.get("id") or "").strip()
            actual_kind = str(ad.get("account_kind") or bucket_kind)
            actual_account = str(ad.get("account_id") or "").removeprefix("act_")
            actual_adset = str(ad.get("adset_id") or "")
            created_at = _ad_time(ad)
            exact_scope = (
                actual_kind == account_kind
                and actual_account == account_id
                and actual_adset == adset_id
            )
            in_window = (
                created_at is not None
                and reconcile_from <= created_at <= reconcile_until
            )
            safe_evidence = {
                "ad_id": ad_id,
                "name": str(ad.get("name") or ""),
                "account_kind": actual_kind,
                "account_id": actual_account,
                "adset_id": actual_adset,
                "created_at": created_at.isoformat() if created_at else None,
            }
            evidence_matches.append(safe_evidence)
            if ad_id and exact_scope and in_window:
                matches[normalized_name].append(ad_id)
            else:
                ambiguous.append(safe_evidence)

    counts = {name: len(ad_ids) for name, ad_ids in matches.items()}
    evidence: dict[str, Any] = {
        "normalized_expected_names": list(normalized_names),
        "match_counts": counts,
        "matching_ads": evidence_matches,
        "ambiguous_ads": ambiguous,
        "window_inclusive": True,
    }
    if ambiguous or any(count > 1 for count in counts.values()):
        return "REVIEW_REQUIRED", (), evidence
    if all(count == 1 for count in counts.values()):
        return (
            "COMPLETE",
            tuple(matches[name][0] for name in normalized_names),
            evidence,
        )
    if all(count == 0 for count in counts.values()):
        return "MISSING", (), evidence
    return "REVIEW_REQUIRED", (), evidence


def _capacity_for_scope(
    inventory: Mapping[str, Sequence[Mapping[str, Any]]],
    account_kind: AccountKind,
    account_id: str,
    adset_id: str,
) -> int:
    from integrations.facebook import MAX_ADS_PER_ADSET

    ad_ids: list[str] = []
    for bucket_kind, rows in inventory.items():
        for ad in rows:
            actual_kind = str(ad.get("account_kind") or bucket_kind)
            actual_account = str(ad.get("account_id") or "").removeprefix("act_")
            actual_adset = str(ad.get("adset_id") or "")
            if (
                actual_kind == account_kind
                and actual_account == account_id
                and actual_adset == adset_id
            ):
                ad_id = str(ad.get("id") or "").strip()
                if not ad_id:
                    raise RecoveryReviewRequired("capacity_ad_id_missing")
                ad_ids.append(ad_id)
    if len(ad_ids) != len(set(ad_ids)) or len(ad_ids) > MAX_ADS_PER_ADSET:
        raise RecoveryReviewRequired("capacity_inventory_ambiguous")
    return MAX_ADS_PER_ADSET - len(ad_ids)


def build_recovery_city_plans(
    completion: TrelloCompletion,
    manifest: MediaManifest,
    inventory: Mapping[str, Sequence[Mapping[str, Any]]],
    audited_at: datetime,
    *,
    card: Mapping[str, Any] | None = None,
    live_adsets: Mapping[AccountKind, LiveAdsetDiscovery] | None = None,
) -> tuple[RecoveryCityPlan, ...]:
    """Строит отдельный immutable typed plan для каждого target city."""
    completed_at = _aware_local(completion.completed_at, "completed_at")
    audited_at = _aware_local(audited_at, "audited_at")
    reconcile_from = completed_at - timedelta(hours=24)
    if audited_at < reconcile_from:
        raise RecoveryReviewRequired("reconcile_window_invalid")
    if card is None:
        from integrations.trello import get_card

        card = get_card(completion.card_id)
    case_id = _case_id(completion.action_id)
    plans: list[RecoveryCityPlan] = []
    for city, expected_names in manifest.expected_names_by_city.items():
        created_at = audited_at
        phase: RecoveryCityPhase = "REVIEW_REQUIRED"
        found_ids: tuple[str, ...] = ()
        evidence: dict[str, Any] = {}
        last_error: str | None = None
        account_kind: AccountKind = "offline"
        account_id = ""
        adset_id = ""
        capacity_available: int | None = None
        try:
            if live_adsets is None:
                account_kind, account_id, adset_id = _resolve_city_scope(card, city)
            else:
                account_kind, account_id, adset_id = _resolve_city_scope(
                    card,
                    city,
                    live_adsets,
                )
            normalized_names = _normalized_expected_names(expected_names)
            if len(normalized_names) != len(expected_names):
                raise RecoveryReviewRequired("expected_count_mismatch")
            if not account_id or not adset_id:
                raise RecoveryReviewRequired("city_scope_incomplete")
            phase, found_ids, evidence = _classify_city_inventory(
                expected_names,
                account_kind,
                account_id,
                adset_id,
                reconcile_from,
                audited_at,
                inventory,
            )
            capacity_available = _capacity_for_scope(
                inventory,
                account_kind,
                account_id,
                adset_id,
            )
            evidence["capacity_available"] = capacity_available
            evidence["hard_reserve_slots"] = _HARD_RESERVE_SLOTS
            if manifest.provider_media_sha256 is not None:
                evidence["provider_media_sha256"] = manifest.provider_media_sha256
                evidence["provider_media_type"] = manifest.media_type
            if (
                phase == "MISSING"
                and capacity_available - len(expected_names) < _HARD_RESERVE_SLOTS
            ):
                phase = "WAITING_SLOT"
        except RecoveryReviewRequired as exc:
            last_error = str(exc)
            evidence = {"reason": last_error}
        plans.append(
            RecoveryCityPlan(
                plan_id=_plan_id(case_id, city),
                case_id=case_id,
                city=city,
                account_kind=account_kind,
                account_id=account_id,
                adset_id=adset_id,
                expected_ad_names=tuple(expected_names),
                expected_ad_count=len(expected_names),
                reconcile_from=reconcile_from,
                reconcile_until=audited_at,
                phase=phase,
                found_ad_ids=found_ids,
                media_manifest_sha256=manifest.manifest_sha256,
                launch_attempt_key=None,
                capacity_available=capacity_available,
                evidence=evidence,
                approved_by=None,
                approved_at=None,
                last_rechecked_at=None,
                last_error=last_error,
                created_at=created_at,
                updated_at=created_at,
                completed_at=created_at if phase == "COMPLETE" else None,
            )
        )
    return tuple(plans)


def classify_recovery_case(
    completion: TrelloCompletion,
    manifest: MediaManifest,
    city_plans: Sequence[RecoveryCityPlan],
) -> LaunchRecoveryPlan:
    """Поднимает любую city ambiguity до REVIEW_REQUIRED всей case."""
    plans = tuple(city_plans)
    reasons: list[str] = []
    if not plans:
        phase: RecoveryCasePhase = "REVIEW_REQUIRED"
        reasons.append("city_plans_empty")
    elif any(plan.phase == "REVIEW_REQUIRED" for plan in plans):
        phase = "REVIEW_REQUIRED"
        reasons.extend(
            f"{plan.city}:{plan.last_error or 'ambiguous_inventory'}"
            for plan in plans
            if plan.phase == "REVIEW_REQUIRED"
        )
    elif all(plan.phase == "COMPLETE" for plan in plans):
        phase = "NO_ACTION"
        reasons.append("all_city_batches_complete")
    elif any(plan.phase == "WAITING_SLOT" for plan in plans) and all(
        plan.phase in {"COMPLETE", "MISSING", "WAITING_SLOT"} for plan in plans
    ):
        phase = "WAITING_SLOT"
        reasons.append("missing_city_batch_has_insufficient_capacity")
    elif all(plan.phase in {"COMPLETE", "MISSING"} for plan in plans):
        phase = "DISCOVERED"
        reasons.append("missing_city_batches_found")
    else:
        phase = "REVIEW_REQUIRED"
        reasons.append("city_phase_not_auditable")
    return LaunchRecoveryPlan(
        case_id=_case_id(completion.action_id),
        card_id=completion.card_id,
        card_name=completion.card_name,
        source_completed_at=completion.completed_at,
        phase=phase,
        media_manifest_sha256=manifest.manifest_sha256,
        city_plans=plans,
        reasons=tuple(reasons),
    )


def _connection():
    from services.cleanup_repository import _get_connection

    return _get_connection()


def _json_dumps(value: Mapping[str, Any] | Sequence[Any]) -> str:
    from services.cleanup_repository import _json_dumps

    return _json_dumps(value)


def _safe_error(value: object) -> str:
    from services.cleanup_repository import sanitize_text

    return sanitize_text(value)[:1000]


def _existing_case(action_id: str) -> dict[str, Any] | None:
    conn = _connection()
    try:
        row = conn.execute(
            "SELECT * FROM launch_recovery_cases WHERE trello_action_id = ?",
            (action_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _persist_discovered(
    completion: TrelloCompletion,
    since: datetime,
    audited_at: datetime,
    tenant_id: str,
) -> Literal["created", "already_exists", "blocked_by_open_case"]:
    conn = _connection()
    case_id = _case_id(completion.action_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        open_cases = conn.execute(
            """
            SELECT *
            FROM launch_recovery_cases
            WHERE card_id = ?
              AND trello_action_id <> ?
              AND phase NOT IN ('NO_ACTION','RECOVERED','CANCELLED')
            ORDER BY
              CASE WHEN phase IN ('DISCOVERED','WAITING_SLOT','APPROVED',
                                  'LAUNCHING','WAITING_ACTIVE') THEN 0 ELSE 1 END,
              source_completed_at DESC
            LIMIT 1
            """,
            (completion.card_id, completion.action_id),
        ).fetchall()
        if open_cases:
            # Старую approved/in-flight case не меняем. Новый action получает
            # отдельный REVIEW_REQUIRED ledger с не-runnable city plans.
            old_case = open_cases[0]
            conflict_error = _safe_error(
                f"blocked_by_nonterminal_case:{old_case['case_id']}"
            )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO launch_recovery_cases (
                    case_id, tenant_id, trello_action_id, card_id, card_name,
                    source_completed_at, scan_since, campaign_type, account_kind,
                    account_id, phase, target_cities_json, found_ads_json,
                    missing_cities_json, expected_names_json, media_manifest_json,
                    media_manifest_sha256, fb_evidence_json, last_error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'REVIEW_REQUIRED', ?, '{}',
                          ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    tenant_id,
                    completion.action_id,
                    completion.card_id,
                    completion.card_name,
                    completion.completed_at.isoformat(),
                    since.isoformat(),
                    old_case["campaign_type"],
                    old_case["account_kind"],
                    old_case["account_id"],
                    old_case["target_cities_json"],
                    old_case["target_cities_json"],
                    old_case["expected_names_json"],
                    old_case["media_manifest_json"],
                    old_case["media_manifest_sha256"],
                    _json_dumps(
                        {
                            "classification": "blocked_by_nonterminal_case",
                            "blocking_case_id": str(old_case["case_id"]),
                            "blocking_action_id": str(old_case["trello_action_id"]),
                        }
                    ),
                    conflict_error,
                    audited_at.isoformat(),
                    audited_at.isoformat(),
                ),
            )
            if cursor.rowcount == 1:
                old_city_rows = conn.execute(
                    """
                    SELECT * FROM launch_recovery_city_plans
                    WHERE case_id = ? ORDER BY city, plan_id
                    """,
                    (old_case["case_id"],),
                ).fetchall()
                for old_city in old_city_rows:
                    city = str(old_city["city"])
                    conn.execute(
                        """
                        INSERT INTO launch_recovery_city_plans (
                            plan_id, case_id, city, account_kind, account_id,
                            adset_id, expected_ad_names_json, expected_ad_count,
                            reconcile_from, reconcile_until, phase,
                            found_ad_ids_json, media_manifest_sha256,
                            evidence_json, last_error, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'REVIEW_REQUIRED', '[]', ?, ?, ?, ?, ?)
                        """,
                        (
                            _plan_id(case_id, city),
                            case_id,
                            city,
                            old_city["account_kind"],
                            old_city["account_id"],
                            old_city["adset_id"],
                            old_city["expected_ad_names_json"],
                            old_city["expected_ad_count"],
                            completion.completed_at.isoformat(),
                            audited_at.isoformat(),
                            old_city["media_manifest_sha256"],
                            _json_dumps(
                                {
                                    "classification": "blocked_by_nonterminal_case",
                                    "blocking_case_id": str(old_case["case_id"]),
                                }
                            ),
                            conflict_error,
                            audited_at.isoformat(),
                            audited_at.isoformat(),
                        ),
                    )
            conn.commit()
            return "blocked_by_open_case"
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO launch_recovery_cases (
                case_id, tenant_id, trello_action_id, card_id, card_name,
                source_completed_at, scan_since, phase, last_error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'DISCOVERED', 'audit_in_progress', ?, ?)
            """,
            (
                case_id,
                tenant_id,
                completion.action_id,
                completion.card_id,
                completion.card_name,
                completion.completed_at.isoformat(),
                since.isoformat(),
                audited_at.isoformat(),
                audited_at.isoformat(),
            ),
        )
        conn.commit()
        return "created" if cursor.rowcount == 1 else "already_exists"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _persist_failure(
    case_id: str, phase: RecoveryCasePhase, error: str, now: datetime
) -> None:
    conn = _connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE launch_recovery_cases
            SET phase = ?, last_error = ?, updated_at = ?
            WHERE case_id = ?
            """,
            (phase, _safe_error(error), now.isoformat(), case_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _persist_plan(
    completion: TrelloCompletion,
    card: Mapping[str, Any],
    manifest: MediaManifest,
    plan: LaunchRecoveryPlan,
    since: datetime,
    audited_at: datetime,
    tenant_id: str,
) -> None:
    campaign_type = _campaign_type(card)
    missing_cities = [
        city.city
        for city in plan.city_plans
        if city.phase in {"MISSING", "WAITING_SLOT"}
    ]
    found_ads = {city.city: list(city.found_ad_ids) for city in plan.city_plans}
    expected_names = {
        city.city: list(city.expected_ad_names) for city in plan.city_plans
    }
    target_cities = [city.city for city in plan.city_plans]
    manifest_json = {
        "files": [asdict(file) for file in manifest.files],
        "provider_media_sha256": manifest.provider_media_sha256,
        "media_type": manifest.media_type,
    }
    case_account_kinds = {
        city.account_kind for city in plan.city_plans if city.account_id
    }
    case_account_ids = {city.account_id for city in plan.city_plans if city.account_id}
    conn = _connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE launch_recovery_cases
            SET tenant_id = ?, card_name = ?, source_completed_at = ?, scan_since = ?,
                campaign_type = ?, account_kind = ?, account_id = ?, phase = ?,
                target_cities_json = ?, found_ads_json = ?, missing_cities_json = ?,
                expected_names_json = ?, media_manifest_json = ?,
                media_manifest_sha256 = ?, fb_evidence_json = ?, last_error = NULL,
                updated_at = ?, completed_at = ?
            WHERE case_id = ? AND trello_action_id = ? AND card_id = ?
            """,
            (
                tenant_id,
                str(card.get("name") or completion.card_name),
                completion.completed_at.isoformat(),
                since.isoformat(),
                campaign_type,
                next(iter(case_account_kinds))
                if len(case_account_kinds) == 1
                else None,
                next(iter(case_account_ids)) if len(case_account_ids) == 1 else None,
                plan.phase,
                _json_dumps(target_cities),
                _json_dumps(found_ads),
                _json_dumps(missing_cities),
                _json_dumps(expected_names),
                _json_dumps(manifest_json),
                manifest.manifest_sha256,
                _json_dumps(
                    {"city_plans": [city.evidence for city in plan.city_plans]}
                ),
                audited_at.isoformat(),
                audited_at.isoformat() if plan.phase == "NO_ACTION" else None,
                plan.case_id,
                completion.action_id,
                completion.card_id,
            ),
        )
        for city in plan.city_plans:
            conn.execute(
                """
                INSERT INTO launch_recovery_city_plans (
                    plan_id, case_id, city, account_kind, account_id, adset_id,
                    expected_ad_names_json, expected_ad_count, reconcile_from,
                    reconcile_until, phase, found_ad_ids_json,
                    media_manifest_sha256, capacity_available, evidence_json,
                    last_error, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    phase = excluded.phase,
                    found_ad_ids_json = excluded.found_ad_ids_json,
                    capacity_available = excluded.capacity_available,
                    evidence_json = excluded.evidence_json,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at,
                    completed_at = excluded.completed_at
                """,
                (
                    city.plan_id,
                    city.case_id,
                    city.city,
                    city.account_kind,
                    city.account_id,
                    city.adset_id,
                    _json_dumps(list(city.expected_ad_names)),
                    city.expected_ad_count,
                    city.reconcile_from.isoformat(),
                    city.reconcile_until.isoformat(),
                    city.phase,
                    _json_dumps(list(city.found_ad_ids)),
                    city.media_manifest_sha256,
                    city.capacity_available,
                    _json_dumps(city.evidence),
                    city.last_error,
                    city.created_at.isoformat(),
                    city.updated_at.isoformat(),
                    city.completed_at.isoformat() if city.completed_at else None,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def audit_missing_launches(
    since: datetime = RECOVERY_CUTOFF,
    tenant_id: str = "default",
) -> LaunchRecoverySummary:
    """Выполняет только audit и сохраняет durable classification."""
    normalized_since = _aware_local(since, "since")
    if normalized_since < RECOVERY_CUTOFF:
        raise ValueError("since не может быть раньше RECOVERY_CUTOFF")
    tenant_id = str(tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id не должен быть пустым")
    audited_at = datetime.now(_TZ_LOCAL)
    audit_run_id = f"recovery-audit-{uuid.uuid4().hex}"
    completions = scan_completed_cards_since(normalized_since)
    case_ids: list[str] = []
    errors: list[str] = []
    no_action_cases = 0
    review_required_cases = 0
    missing_city_plans = 0

    for completion in completions:
        case_id = _case_id(completion.action_id)
        existing = _existing_case(completion.action_id)
        if existing is not None and not (
            existing.get("phase") in {"DISCOVERED", "BLOCKED"}
            and existing.get("last_error")
        ):
            case_ids.append(case_id)
            no_action_cases += int(existing.get("phase") == "NO_ACTION")
            review_required_cases += int(existing.get("phase") == "REVIEW_REQUIRED")
            continue
        try:
            if existing is None:
                persist_outcome = _persist_discovered(
                    completion,
                    normalized_since,
                    audited_at,
                    tenant_id,
                )
                if persist_outcome == "blocked_by_open_case":
                    review_required_cases += 1
                    case_ids.append(case_id)
                    continue
                if persist_outcome == "already_exists":
                    case_ids.append(case_id)
                    continue
            from integrations.trello import get_card

            card = get_card(completion.card_id)
            if str(card.get("id") or "") != completion.card_id:
                raise RecoveryReviewRequired("card_identity_mismatch")
            if card.get("dueComplete") is not True or card.get("closed") is True:
                raise RecoveryReviewRequired("card_no_longer_completed_or_closed")
            current_card_name = str(card.get("name") or "").strip()
            if completion.card_name and current_card_name != completion.card_name:
                raise RecoveryReviewRequired("card_name_changed_after_completion")
            manifest = build_media_manifest(card)
            _campaign_type(card)
            # Оба кабинета обязательны: exact name в другом scope означает
            # ambiguity, а не безопасное отсутствие batch в целевом кабинете.
            live_adsets = discover_live_recovery_adsets(("offline", "online"))
            inventory = fetch_complete_launch_inventory(("offline", "online"))
            city_plans = build_recovery_city_plans(
                completion,
                manifest,
                inventory,
                audited_at,
                card=card,
                live_adsets=live_adsets,
            )
            recovery_plan = classify_recovery_case(completion, manifest, city_plans)
            _persist_plan(
                completion,
                card,
                manifest,
                recovery_plan,
                normalized_since,
                audited_at,
                tenant_id,
            )
            no_action_cases += int(recovery_plan.phase == "NO_ACTION")
            review_required_cases += int(recovery_plan.phase == "REVIEW_REQUIRED")
            missing_city_plans += sum(
                city.phase in {"MISSING", "WAITING_SLOT"}
                for city in recovery_plan.city_plans
            )
        except RecoveryReviewRequired as exc:
            review_required_cases += 1
            _persist_failure(case_id, "REVIEW_REQUIRED", str(exc), audited_at)
        except Exception as exc:
            safe_error = _safe_error(exc)
            errors.append(f"{case_id}:{safe_error}")
            _persist_failure(case_id, "BLOCKED", safe_error, audited_at)
        case_ids.append(case_id)

    return LaunchRecoverySummary(
        audit_run_id=audit_run_id,
        since=normalized_since,
        audited_at=audited_at,
        discovered_cards=len(completions),
        no_action_cases=no_action_cases,
        review_required_cases=review_required_cases,
        missing_city_plans=missing_city_plans,
        case_ids=tuple(case_ids),
        errors=tuple(errors),
    )


def _optional_time(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    return _parse_aware(value, "durable_time")


def _city_plan_from_row(row: Mapping[str, Any]) -> RecoveryCityPlan:
    return RecoveryCityPlan(
        plan_id=str(row["plan_id"]),
        case_id=str(row["case_id"]),
        city=str(row["city"]),
        account_kind=str(row["account_kind"]),  # type: ignore[arg-type]
        account_id=str(row["account_id"]),
        adset_id=str(row["adset_id"]),
        expected_ad_names=tuple(json.loads(str(row["expected_ad_names_json"]))),
        expected_ad_count=int(row["expected_ad_count"]),
        reconcile_from=_parse_aware(row["reconcile_from"], "reconcile_from"),
        reconcile_until=_parse_aware(row["reconcile_until"], "reconcile_until"),
        phase=str(row["phase"]),  # type: ignore[arg-type]
        found_ad_ids=tuple(json.loads(str(row["found_ad_ids_json"]))),
        media_manifest_sha256=str(row["media_manifest_sha256"]),
        launch_attempt_key=row["launch_attempt_key"],
        capacity_available=row["capacity_available"],
        evidence=dict(json.loads(str(row["evidence_json"]))),
        approved_by=row["approved_by"],
        approved_at=_optional_time(row["approved_at"]),
        last_rechecked_at=_optional_time(row["last_rechecked_at"]),
        last_error=row["last_error"],
        created_at=_parse_aware(row["created_at"], "created_at"),
        updated_at=_parse_aware(row["updated_at"], "updated_at"),
        completed_at=_optional_time(row["completed_at"]),
    )


def get_recovery_status(
    tenant_id: str = "default",
    limit: int = 50,
) -> list[LaunchRecoveryPlan]:
    """Возвращает durable cases без сетевых вызовов и внешних mutation."""
    tenant_id = str(tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id не должен быть пустым")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit должен быть от 1 до 500")
    conn = _connection()
    try:
        case_rows = conn.execute(
            """
            SELECT * FROM launch_recovery_cases
            WHERE tenant_id = ?
            ORDER BY CASE WHEN phase IN ('NO_ACTION','RECOVERED','CANCELLED')
                          THEN 1 ELSE 0 END,
                     updated_at DESC, case_id
            LIMIT ?
            """,
            (tenant_id, limit),
        ).fetchall()
        result: list[LaunchRecoveryPlan] = []
        for case in case_rows:
            city_rows = conn.execute(
                """
                SELECT * FROM launch_recovery_city_plans
                WHERE case_id = ? ORDER BY city, plan_id
                """,
                (case["case_id"],),
            ).fetchall()
            plans = tuple(_city_plan_from_row(dict(row)) for row in city_rows)
            reasons = (str(case["last_error"]),) if case["last_error"] else ()
            result.append(
                LaunchRecoveryPlan(
                    case_id=str(case["case_id"]),
                    card_id=str(case["card_id"]),
                    card_name=str(case["card_name"]),
                    source_completed_at=_parse_aware(
                        case["source_completed_at"], "source_completed_at"
                    ),
                    phase=str(case["phase"]),  # type: ignore[arg-type]
                    media_manifest_sha256=str(case["media_manifest_sha256"] or ""),
                    city_plans=plans,
                    reasons=reasons,
                )
            )
        return result
    finally:
        conn.close()
