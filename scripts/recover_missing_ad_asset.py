#!/usr/bin/env python3
"""CLI точечного восстановления одного отсутствующего FB ad asset."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Прямой запуск `python scripts/...` должен видеть корень проекта.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.ad_asset_recovery import (  # noqa: E402
    DEFAULT_LEDGER_PATH,
    ManifestValidationError,
    RecoveryError,
    RecoveryLedger,
    load_manifest_file,
    recover_sequential,
    result_to_dict,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed CREATE ровно одного missing asset по exact JSON manifest"
        )
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Разрешить CREATE из проверенного source creative без upload. "
            "Без флага выполняется только локальная валидация."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        manifests = load_manifest_file(args.manifest)
    except ManifestValidationError as exc:
        print(json.dumps({"status": "INVALID", "error": str(exc)}, ensure_ascii=False))
        return 2

    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "VALIDATED_ONLY",
                    "count": len(manifests),
                    "manifest_keys": [manifest.key for manifest in manifests],
                    "cities": [manifest.city for manifest in manifests],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if any(not manifest.source_ad_id for manifest in manifests):
        print(
            json.dumps(
                {"status": "BLOCKED", "error": "production_recovery_source_only"},
                ensure_ascii=False,
            )
        )
        return 3

    try:
        results = recover_sequential(
            manifests,
            ledger=RecoveryLedger(args.ledger),
        )
    except (ManifestValidationError, RecoveryError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False))
        return 3
    except Exception as exc:
        # Сырые тексты внешних исключений могут содержать URL/секреты.
        print(
            json.dumps(
                {"status": "BLOCKED", "error_type": type(exc).__name__},
                ensure_ascii=False,
            )
        )
        return 3
    print(
        json.dumps(
            [result_to_dict(result) for result in results],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(result.status == "SUCCEEDED" for result in results) else 3


if __name__ == "__main__":
    sys.exit(main())
