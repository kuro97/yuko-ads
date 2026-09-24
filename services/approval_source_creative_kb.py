"""Read-only evidence из creative_kb для accuracy и product share."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import config
from services.approval_checker_models import (
    EvidenceRecord,
    EvidenceRequest,
    EvidenceState,
    FactCategory,
    Metric,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
)
from services.approval_source_decisions import (
    LocalSqliteEvidenceError,
    _parse_sqlite_datetime,
    _read_only_rows,
)
from services.product_tags import PRODUCTS

_REQUIRED_COLUMNS = (
    "ad_id",
    "status",
    "qual_pct",
    "romi",
    "target_product",
    "synced_at",
    "outcomes_matched_at",
)
_FRESH_FOR = timedelta(hours=3)
_MIN_QUAL_PCT = Decimal("15")
_MIN_ROMI = Decimal("0")
_DECISION_COLUMNS = ("id", "ad_id", "action", "confirmed_by", "created_at")
_CANONICAL_PRODUCTS = frozenset(PRODUCTS)


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise LocalSqliteEvidenceError("CREATIVE_KB_NUMBER_INVALID") from exc
    if not parsed.is_finite():
        raise LocalSqliteEvidenceError("CREATIVE_KB_NUMBER_INVALID")
    return parsed


def _error(now: datetime, exc: Exception) -> SourceEvidence:
    code = str(exc) if isinstance(exc, LocalSqliteEvidenceError) else type(exc).__name__
    state = EvidenceState.INCOMPLETE if isinstance(exc, LocalSqliteEvidenceError) else EvidenceState.ERROR
    return SourceEvidence(
        source=SourceSystem.CREATIVE_KB,
        state=state,
        fetched_at=now,
        data_as_of=None,
        from_cache=False,
        complete=False,
        records=(),
        error_code=code[:120],
    )


def _accuracy_requested(request: EvidenceRequest) -> bool:
    return any(
        subject.kind is SubjectKind.CREATIVE
        and subject.subject_id in {"scale-total", "scale-winners", "scale-accuracy"}
        for subject in request.subjects
    ) or any(
        claim.source is SourceSystem.CREATIVE_KB
        and (
            claim.metric is Metric.ACCURACY_PCT
            or claim.subject.subject_id in {"scale-total", "scale-winners"}
        )
        for claim in request.claims
    )


def load_creative_kb_evidence(request: EvidenceRequest, now: datetime) -> SourceEvidence:
    """Перечитывает текущий KB; missing outcome не превращает ad в проигравший."""

    try:
        snapshot = _read_only_rows(
            config.REPORT_CHECKER_DECISIONS_DB_PATH,
            "creative_kb",
            _REQUIRED_COLUMNS,
            None,
            order_by="ad_id",
        )
        rows_by_ad: dict[str, object] = {}
        latest: datetime | None = None
        for row in snapshot.rows:
            ad_id = str(row["ad_id"] or "").strip()
            if not ad_id or ad_id in rows_by_ad:
                raise LocalSqliteEvidenceError("CREATIVE_KB_DUPLICATE_AD")
            synced_at = _parse_sqlite_datetime(row["synced_at"])
            if synced_at > now or now - synced_at > _FRESH_FOR:
                raise LocalSqliteEvidenceError("CREATIVE_KB_STALE")
            rows_by_ad[ad_id] = row
            latest = synced_at if latest is None else max(latest, synced_at)

        metadata = (f"schema:{snapshot.schema_sha256}", f"data-version:{snapshot.data_version}")
        windows = request.windows or (None,)
        records: list[EvidenceRecord] = []

        requested_ids = sorted(set(request.ad_ids))
        for ad_id in requested_ids:
            row = rows_by_ad.get(ad_id)
            if row is None:
                # Полный snapshot может честно не содержать outcome: это limitation, не false loser.
                continue
            status = str(row["status"] or "").strip().upper()
            matched_raw = row["outcomes_matched_at"]
            matched_at = _parse_sqlite_datetime(matched_raw) if matched_raw not in (None, "") else None
            qual_pct = _decimal_or_none(row["qual_pct"])
            romi = _decimal_or_none(row["romi"])
            for window in windows:
                subject = SubjectRef(SubjectKind.CREATIVE, ad_id)
                records.append(
                    EvidenceRecord(
                        category=FactCategory.ACTION_STATE,
                        subject=subject,
                        metric=Metric.RECORD_STATUS,
                        value=status,
                        source=SourceSystem.CREATIVE_KB,
                        state=EvidenceState.FRESH_COMPLETE,
                        observed_at=now,
                        window=window,
                        currency=None,
                        entity_ids=metadata,
                    )
                )
                if matched_at is None:
                    continue
                if qual_pct is not None:
                    records.append(
                        EvidenceRecord(
                            category=FactCategory.BUSINESS_METRIC,
                            subject=subject,
                            metric=Metric.QUAL_PCT,
                            value=qual_pct,
                            source=SourceSystem.CREATIVE_KB,
                            state=EvidenceState.FRESH_COMPLETE,
                            observed_at=now,
                            window=window,
                            currency=None,
                            entity_ids=(f"matched-at:{matched_at.isoformat()}", *metadata),
                        )
                    )
                if romi is not None:
                    records.append(
                        EvidenceRecord(
                            category=FactCategory.BUSINESS_METRIC,
                            subject=subject,
                            metric=Metric.ROMI_PCT,
                            value=romi,
                            source=SourceSystem.CREATIVE_KB,
                            state=EvidenceState.FRESH_COMPLETE,
                            observed_at=now,
                            window=window,
                            currency=None,
                            entity_ids=(f"matched-at:{matched_at.isoformat()}", *metadata),
                        )
                    )

        active_rows = [row for row in rows_by_ad.values() if str(row["status"] or "").upper() == "ACTIVE"]
        product_counts: dict[str, int] = {}
        untagged = 0
        for row in active_rows:
            product = str(row["target_product"] or "").strip().upper()
            if product not in _CANONICAL_PRODUCTS:
                untagged += 1
                continue
            product_counts[product] = product_counts.get(product, 0) + 1
        tagged_total = sum(product_counts.values())
        for window in windows:
            for product, count in sorted(product_counts.items()):
                share = (
                    Decimal(str(round(count / tagged_total * 100, 1)))
                    if tagged_total
                    else None
                )
                records.append(
                    EvidenceRecord(
                        category=FactCategory.BUSINESS_METRIC,
                        subject=SubjectRef(SubjectKind.PRODUCT, product),
                        metric=Metric.PRODUCT_SHARE_PCT,
                        value=share,
                        source=SourceSystem.CREATIVE_KB,
                        state=EvidenceState.FRESH_COMPLETE,
                        observed_at=now,
                        window=window,
                        currency=None,
                        entity_ids=(
                            f"tagged-count:{count}",
                            f"tagged-denominator:{tagged_total}",
                            f"untagged-count:{untagged}",
                            *metadata,
                        ),
                    )
                )
                records.append(
                    EvidenceRecord(
                        category=FactCategory.BUSINESS_METRIC,
                        subject=SubjectRef(SubjectKind.PRODUCT, f"{product}:count"),
                        metric=Metric.RECORD_STATUS,
                        value=count,
                        source=SourceSystem.CREATIVE_KB,
                        state=EvidenceState.FRESH_COMPLETE,
                        observed_at=now,
                        window=window,
                        currency=None,
                        entity_ids=(f"tagged-denominator:{tagged_total}", *metadata),
                    )
                )
            records.append(
                EvidenceRecord(
                    category=FactCategory.BUSINESS_METRIC,
                    subject=SubjectRef(SubjectKind.PRODUCT, "TAGGED_TOTAL"),
                    metric=Metric.RECORD_STATUS,
                    value=tagged_total,
                    source=SourceSystem.CREATIVE_KB,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=window,
                    currency=None,
                    entity_ids=tuple(
                        f"product:{product}:{count}"
                        for product, count in sorted(product_counts.items())
                    )
                    + metadata,
                )
            )
            records.append(
                EvidenceRecord(
                    category=FactCategory.BUSINESS_METRIC,
                    subject=SubjectRef(SubjectKind.PRODUCT, "UNTAGGED"),
                    metric=Metric.RECORD_STATUS,
                    value=untagged,
                    source=SourceSystem.CREATIVE_KB,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=window,
                    currency=None,
                    entity_ids=(f"tagged-denominator:{tagged_total}", *metadata),
                )
            )

        if _accuracy_requested(request):
            if not request.windows:
                raise LocalSqliteEvidenceError("CREATIVE_KB_ACCURACY_WINDOW_MISSING")
            for window in request.windows:
                decisions = _read_only_rows(
                    config.REPORT_CHECKER_DECISIONS_DB_PATH,
                    "decisions",
                    _DECISION_COLUMNS,
                    window,
                )
                scaled_ids: set[str] = set()
                scaled_decision_ids: list[str] = []
                for decision in decisions.rows:
                    ad_id = str(decision["ad_id"] or "").strip()
                    action = str(decision["action"] or "").strip()
                    confirmed_by = str(decision["confirmed_by"] or "").strip()
                    if not ad_id or not action or not confirmed_by:
                        raise LocalSqliteEvidenceError("CREATIVE_KB_SCALE_DECISION_INVALID")
                    if confirmed_by.casefold().startswith("autopilot") and "scale" in action.casefold():
                        scaled_ids.add(ad_id)
                        scaled_decision_ids.append(str(decision["id"]))

                missing_ids = sorted(scaled_ids - set(rows_by_ad))
                if missing_ids:
                    raise LocalSqliteEvidenceError("CREATIVE_KB_ACCURACY_AD_MISSING")
                winner_ids: list[str] = []
                for ad_id in sorted(scaled_ids):
                    row = rows_by_ad[ad_id]
                    matched_raw = row["outcomes_matched_at"]
                    if matched_raw in (None, ""):
                        raise LocalSqliteEvidenceError("CREATIVE_KB_ACCURACY_OUTCOME_MISSING")
                    matched_at = _parse_sqlite_datetime(matched_raw)
                    if matched_at > now:
                        raise LocalSqliteEvidenceError("CREATIVE_KB_ACCURACY_OUTCOME_INVALID")
                    qual_pct = _decimal_or_none(row["qual_pct"])
                    romi = _decimal_or_none(row["romi"])
                    if qual_pct is None or romi is None:
                        raise LocalSqliteEvidenceError("CREATIVE_KB_ACCURACY_OUTCOME_MISSING")
                    if qual_pct >= _MIN_QUAL_PCT and romi > _MIN_ROMI:
                        winner_ids.append(ad_id)

                checked_ids = tuple(sorted(scaled_ids))
                winner_tuple = tuple(sorted(winner_ids))
                accuracy = (
                    Decimal(
                        str(round(len(winner_tuple) / len(checked_ids) * 100, 1))
                    )
                    if checked_ids
                    else None
                )
                decision_metadata = (
                    f"decisions-schema:{decisions.schema_sha256}",
                    f"decisions-data-version:{decisions.data_version}",
                    *(f"decision:{item}" for item in sorted(scaled_decision_ids)),
                    *metadata,
                )
                for subject_id, value, exact_ids in (
                    ("scale-total", len(checked_ids), checked_ids),
                    ("scale-winners", len(winner_tuple), winner_tuple),
                ):
                    records.append(
                        EvidenceRecord(
                            category=FactCategory.BUSINESS_METRIC,
                            subject=SubjectRef(SubjectKind.CREATIVE, subject_id),
                            metric=Metric.DECISION_COUNT,
                            value=value,
                            source=SourceSystem.CREATIVE_KB,
                            state=EvidenceState.FRESH_COMPLETE,
                            observed_at=now,
                            window=window,
                            currency=None,
                            entity_ids=(
                                *(f"ad:{item}" for item in exact_ids),
                                *decision_metadata,
                            ),
                        )
                    )
                records.append(
                    EvidenceRecord(
                        category=FactCategory.BUSINESS_METRIC,
                        subject=SubjectRef(SubjectKind.CREATIVE, "scale-accuracy"),
                        metric=Metric.ACCURACY_PCT,
                        value=accuracy,
                        source=SourceSystem.CREATIVE_KB,
                        state=EvidenceState.FRESH_COMPLETE,
                        observed_at=now,
                        window=window,
                        currency=None,
                        entity_ids=(
                            f"numerator-winners:{len(winner_tuple)}",
                            f"denominator-checked:{len(checked_ids)}",
                            *(f"winner:{item}" for item in winner_tuple),
                            *(f"checked:{item}" for item in checked_ids),
                            *decision_metadata,
                        ),
                    )
                )
    except Exception as exc:
        return _error(now, exc)

    return SourceEvidence(
        source=SourceSystem.CREATIVE_KB,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=now,
        data_as_of=latest or now,
        from_cache=False,
        complete=True,
        records=tuple(records),
    )
