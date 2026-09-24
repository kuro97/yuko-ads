#!/usr/bin/env python3
"""Recovery CLI: audit-only по умолчанию, active под точными гейтами."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.launch_recovery import RECOVERY_CUTOFF, audit_missing_launches  # noqa: E402
from services.launch_recovery_apply import (  # noqa: E402
    RecoveryApplyError,
    apply_recovery_case,
    verify_case_action_identity,
)


CONFIRM_PRODUCTION = "APPLY-EXACT-LAUNCH-RECOVERY"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit пропущенных запусков; без --apply внешних mutation нет",
    )
    parser.add_argument("--since", default=RECOVERY_CUTOFF.isoformat())
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--case-id", default="")
    parser.add_argument("--trello-action-id", default="")
    parser.add_argument("--manifest-sha256", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--confirm-production", default="")
    return parser


def _apply_gate(args: argparse.Namespace) -> str | None:
    if not args.case_id:
        return "нужен exact --case-id"
    if not args.trello_action_id:
        return "нужен exact --trello-action-id"
    if not args.manifest_sha256:
        return "нужен exact --manifest-sha256"
    if not args.actor:
        return "нужен --actor"
    if args.confirm_production != CONFIRM_PRODUCTION:
        return f"нужен --confirm-production {CONFIRM_PRODUCTION}"
    return None


def _parse_since(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--since должен быть ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--since должен содержать timezone")
    if parsed < RECOVERY_CUTOFF:
        raise ValueError("--since не может быть раньше recovery cutoff")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.apply:
        try:
            summary = audit_missing_launches(
                _parse_since(args.since),
                tenant_id=args.tenant_id,
            )
        except Exception:
            print(json.dumps({"mode": "audit", "ok": False}, sort_keys=True))
            return 1
        print(
            json.dumps(
                {
                    "mode": "audit",
                    "ok": not summary.errors,
                    "audit_run_id": summary.audit_run_id,
                    "discovered_cards": summary.discovered_cards,
                    "no_action_cases": summary.no_action_cases,
                    "review_required_cases": summary.review_required_cases,
                    "missing_city_plans": summary.missing_city_plans,
                    "error_count": len(summary.errors),
                },
                sort_keys=True,
            )
        )
        return 0 if not summary.errors else 1

    gate_error = _apply_gate(args)
    if gate_error:
        print(json.dumps({"mode": "apply", "ok": False, "reason": gate_error}))
        return 2
    try:
        if not verify_case_action_identity(args.case_id, args.trello_action_id):
            print(
                json.dumps(
                    {"mode": "apply", "ok": False, "reason": "case/action mismatch"}
                )
            )
            return 2
        result = apply_recovery_case(
            args.case_id,
            args.manifest_sha256,
            actor=args.actor,
        )
    except (RecoveryApplyError, ValueError):
        print(json.dumps({"mode": "apply", "ok": False, "reason": "apply blocked"}))
        return 2
    except Exception:
        print(json.dumps({"mode": "apply", "ok": False, "reason": "apply failed"}))
        return 1
    print(
        json.dumps(
            {
                "mode": "apply",
                "ok": not result.errors,
                "case_id": result.case_id,
                "phase": result.phase,
                "applied_plan_ids": list(result.applied_plan_ids),
                "skipped_plan_ids": list(result.skipped_plan_ids),
                "created_count": sum(
                    len(ad_ids) for ad_ids in result.created_ad_ids_by_plan.values()
                ),
                "error_count": len(result.errors),
            },
            sort_keys=True,
        )
    )
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
